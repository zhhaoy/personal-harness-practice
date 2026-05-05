#!/usr/bin/env python3
"""
Agent Loop - 流式输出 + 规划模式 + 卡死检测（稳定版）
兼容模型：GLM、DeepSeek、Qwen（OpenAI 兼容 API）
环境变量：
    LLM_API_BASE   - API基础URL（必需）
    LLM_API_KEY    - API密钥（必需）
    LLM_MODEL      - 模型名称（必需）
"""

import os
import subprocess
import sys
import json
import platform
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Callable
from dataclasses import dataclass, field

try:
    from openai import OpenAI, APIError
except ImportError:
    print("错误: 请安装 openai 库: pip install openai")
    sys.exit(1)

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

# ========== 配置常量 ==========
DEFAULT_TIMEOUT = 120
MAX_OUTPUT_SIZE = 50000
WORKDIR = Path.cwd()

BLOCKED_COMMANDS = [
    "rm -rf /",
    "sudo",
    "shutdown",
    "reboot",
    "mkfs",
    "dd if=",
    "> /dev/",
    "chmod 777",
]

# ========== 系统提示 ==========
SYSTEM_PROMPT = f"""你是一个智能编码助手，当前工作目录: {WORKDIR}。
你可以使用以下工具解决问题：
- bash: 执行 Shell 命令
- read_file: 读取文件内容
- write_file: 写入或覆盖文件
- edit_file: 替换文件中的文本（先查找 old_text，再替换为 new_text）
- todo: 创建或更新任务列表，规划多步骤工作

请遵循以下原则：
- 对于复杂或多步骤任务，首先使用 todo 工具创建计划，将大任务拆解为子任务。
- 每个子任务使用唯一 id（如 "1", "2"），状态为 pending/in_progress/completed。
- 同一时间只能有一个任务处于 in_progress 状态，完成后再开始下一个。
- 如果连续尝试相同命令多次没有进展，请改变策略或使用不同参数。
- 完成任务后，给出清晰的最终答案。"""


# ========== TodoManager ==========
class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        if len(items) > 20:
            raise ValueError("最多允许 20 个任务")
        validated = []
        in_progress_count = 0
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            if not text:
                raise ValueError(f"任务 {item_id}: 缺少文本内容")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"任务 {item_id}: 无效状态 '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})
        if in_progress_count > 1:
            raise ValueError("同时只能有一个任务处于进行中 (in_progress)")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "暂无待办任务。"
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(
                item["status"], "[?]"
            )
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} 任务完成)")
        return "\n".join(lines)


TODO = TodoManager()


# ========== 辅助函数 ==========
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径逃逸工作目录: {p}")
    return path


def clean_ellipsis(obj):
    """递归地将 Ellipsis 对象转换为字符串 '...'，避免 JSON 序列化错误"""
    if obj is Ellipsis:
        return "..."
    if isinstance(obj, dict):
        return {k: clean_ellipsis(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_ellipsis(i) for i in obj]
    return obj


# ========== 工具实现 ==========
def run_bash(command: str, timeout: int = DEFAULT_TIMEOUT) -> Tuple[str, Optional[str]]:
    cmd_lower = command.lower().strip()
    for blocked in BLOCKED_COMMANDS:
        if blocked in cmd_lower:
            return "", f"危险命令被阻止: {command}"

    if platform.system() == "Windows":
        shell_cmd = ["cmd.exe", "/c", command]
        use_shell = False
    else:
        shell_cmd = command
        use_shell = True

    try:
        if use_shell:
            result = subprocess.run(
                shell_cmd,
                shell=True,
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        else:
            result = subprocess.run(
                shell_cmd,
                shell=False,
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        output = (result.stdout + result.stderr).strip()
        if not output:
            output = "(无输出)"
        if len(output) > MAX_OUTPUT_SIZE:
            output = output[:MAX_OUTPUT_SIZE] + f"\n...[输出已截断]"
        return output, None
    except subprocess.TimeoutExpired:
        return "", f"命令执行超时 ({timeout}秒)"
    except FileNotFoundError:
        return "", f"命令不存在或 Shell 路径错误: {command}"
    except Exception as e:
        return "", f"执行异常: {str(e)}"


def run_read(path: str, limit: Optional[int] = None) -> Tuple[str, Optional[str]]:
    try:
        fp = safe_path(path)
        if not fp.exists():
            return "", f"文件不存在: {path}"
        text = fp.read_text()
        lines = text.splitlines()
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"... (共 {len(lines)} 行，仅显示前 {limit} 行)"]
        output = "\n".join(lines)
        if len(output) > MAX_OUTPUT_SIZE:
            output = output[:MAX_OUTPUT_SIZE] + f"\n...[输出已截断]"
        return output, None
    except Exception as e:
        return "", f"读取文件失败: {str(e)}"


def run_write(path: str, content: str) -> Tuple[str, Optional[str]]:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"已写入 {len(content)} 字节到 {path}", None
    except Exception as e:
        return "", f"写入文件失败: {str(e)}"


def run_edit(path: str, old_text: str, new_text: str) -> Tuple[str, Optional[str]]:
    try:
        fp = safe_path(path)
        if not fp.exists():
            return "", f"文件不存在: {path}"
        content = fp.read_text()
        if old_text not in content:
            return "", f"在文件 {path} 中未找到要替换的文本"
        new_content = content.replace(old_text, new_text, 1)
        fp.write_text(new_content)
        return f"已编辑文件 {path}（替换了一处匹配）", None
    except Exception as e:
        return "", f"编辑文件失败: {str(e)}"


def run_todo(items: List[Dict]) -> Tuple[str, Optional[str]]:
    try:
        result = TODO.update(items)
        return result, None
    except Exception as e:
        return "", f"更新任务列表失败: {str(e)}"


# ========== 工具调度矩阵 ==========
TOOL_HANDLERS: Dict[str, Callable] = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo": lambda **kw: run_todo(kw["items"]),
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行一条Shell命令。",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "将内容写入文件（会覆盖已有内容）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "编辑文件，将 old_text 替换为 new_text（仅替换第一次出现）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo",
            "description": "创建或更新任务列表。用于多步骤任务的规划与进度跟踪。",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "text": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                            },
                            "required": ["id", "text", "status"],
                        },
                    }
                },
                "required": ["items"],
            },
        },
    },
]


# ========== 数据结构 ==========
@dataclass
class ToolCallRecord:
    tool_name: str
    arguments: Dict[str, Any]
    output: str
    error: Optional[str] = None


@dataclass
class AgentResult:
    final_answer: str
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    error: Optional[str] = None


# ========== 多模型客户端（流式 + 自动回退） ==========
class MultiModelClient:
    def __init__(self):
        api_base = os.getenv("LLM_API_BASE")
        api_key = os.getenv("LLM_API_KEY")
        self.model = os.getenv("LLM_MODEL")
        if not all([api_base, api_key, self.model]):
            raise ValueError("请设置环境变量: LLM_API_BASE, LLM_API_KEY, LLM_MODEL")
        self.client = OpenAI(
            base_url=api_base, api_key=api_key, timeout=60.0, max_retries=2
        )

    def chat(
        self, messages: List[Dict], tools: List[Dict], stream: bool = False
    ) -> Dict:
        if not stream:
            return self._chat_no_stream(messages, tools)
        else:
            # 先清理 messages 和 tools 中的 Ellipsis
            clean_messages = clean_ellipsis(messages)
            clean_tools = clean_ellipsis(tools)
            try:
                return self._chat_stream(clean_messages, clean_tools)
            except Exception as e:
                print(f"\n[流式调用失败，切换到非流式] {e}")
                return self._chat_no_stream(clean_messages, clean_tools)

    def _chat_no_stream(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.7,
                max_tokens=4096,
            )
            choice = response.choices[0]
            message = choice.message
            tool_calls = []
            if message.tool_calls:
                for tc in message.tool_calls:
                    tool_calls.append(
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                    )
            return {
                "content": message.content or "",
                "tool_calls": tool_calls,
                "finish_reason": choice.finish_reason,
            }
        except APIError as e:
            return {
                "content": "",
                "tool_calls": [],
                "finish_reason": "error",
                "error": f"API错误: {e}",
            }
        except Exception as e:
            return {
                "content": "",
                "tool_calls": [],
                "finish_reason": "error",
                "error": f"调用失败: {str(e)}",
            }

    def _chat_stream(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.7,
            max_tokens=4096,
            stream=True,
        )
        collected_content = []
        collected_tool_calls = []
        tool_call_index_map = {}
        last_finish_reason = None

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            finish_reason = chunk.choices[0].finish_reason
            if finish_reason:
                last_finish_reason = finish_reason

            if delta.content:
                print(delta.content, end="", flush=True)
                collected_content.append(delta.content)

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_call_index_map:
                        new_tc = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                        tool_call_index_map[idx] = len(collected_tool_calls)
                        collected_tool_calls.append(new_tc)
                    pos = tool_call_index_map[idx]
                    if tc_delta.id:
                        collected_tool_calls[pos]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            collected_tool_calls[pos]["function"][
                                "name"
                            ] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            collected_tool_calls[pos]["function"][
                                "arguments"
                            ] += tc_delta.function.arguments

        print()  # 换行
        formatted_tool_calls = []
        for tc in collected_tool_calls:
            if tc["function"]["name"]:  # 只返回完整的工具调用
                # 确保 arguments 是字符串
                args_str = tc["function"]["arguments"]
                if not isinstance(args_str, str):
                    args_str = str(args_str)
                formatted_tool_calls.append(
                    {
                        "id": tc["id"],
                        "type": tc["type"],
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": args_str,
                        },
                    }
                )
        return {
            "content": "".join(collected_content),
            "tool_calls": formatted_tool_calls,
            "finish_reason": last_finish_reason or "stop",
        }


# ========== 主控循环（含规划提醒 + 卡死检测） ==========
def agent_loop(initial_prompt: str, max_iterations: int = 30) -> AgentResult:
    try:
        llm = MultiModelClient()
    except ValueError as e:
        return AgentResult(final_answer="", error=str(e))

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": initial_prompt},
    ]
    tool_records: List[ToolCallRecord] = []
    iteration = 0
    rounds_since_todo = 0

    STUCK_THRESHOLD = 3
    call_signatures = []
    consecutive_identical = 0

    while iteration < max_iterations:
        iteration += 1

        # 提醒 todo
        if rounds_since_todo >= 3:
            reminder_msg = {
                "role": "user",
                "content": "<reminder>你已连续多轮未更新任务列表(todo)，请使用 todo 工具规划或更新当前进度。</reminder>",
            }
            messages.append(reminder_msg)
            rounds_since_todo = 0

        # 提醒卡死
        if consecutive_identical >= STUCK_THRESHOLD:
            stuck_msg = {
                "role": "user",
                "content": "<system-reminder>⚠️ 检测到你在连续多轮中重复调用相同的工具且参数相同，似乎没有取得进展。请尝试不同的命令、工具或方法，或检查是否已完成任务。如果已经完成，请给出最终答案。</system-reminder>",
            }
            messages.append(stuck_msg)
            consecutive_identical = 0
            call_signatures = []

        # 调用模型（启用流式）
        print("\033[90m[模型思考]\033[0m ", end="", flush=True)
        resp = llm.chat(messages, TOOLS, stream=True)
        print()  # 换行

        if resp.get("error"):
            return AgentResult(
                final_answer="", tool_calls=tool_records, error=resp["error"]
            )

        # 添加 assistant 消息
        assistant_msg = {"role": "assistant", "content": resp["content"]}
        if resp["tool_calls"]:
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": tc["type"], "function": tc["function"]}
                for tc in resp["tool_calls"]
            ]
        messages.append(assistant_msg)

        if not resp["tool_calls"]:
            return AgentResult(
                final_answer=resp["content"], tool_calls=tool_records, error=None
            )

        # 生成调用签名（用于卡死检测，增强异常处理）
        current_sigs = []
        for tc in resp["tool_calls"]:
            tool_name = tc["function"]["name"]
            args_str_raw = tc["function"]["arguments"]
            try:
                args = json.loads(args_str_raw) if args_str_raw else {}
            except json.JSONDecodeError:
                args = {}
            # 简化参数值（避免长字符串导致误判）
            safe_args = {}
            for k, v in args.items():
                if isinstance(v, str) and len(v) > 200:
                    safe_args[k] = v[:200] + "..."
                else:
                    safe_args[k] = v
            try:
                args_signature = json.dumps(safe_args, sort_keys=True)
            except Exception:
                args_signature = ""
            current_sigs.append(f"{tool_name}:{args_signature}")
        current_sig = "|".join(current_sigs)

        if call_signatures and current_sig == call_signatures[-1]:
            consecutive_identical += 1
        else:
            consecutive_identical = 1
        call_signatures.append(current_sig)
        call_signatures = call_signatures[-STUCK_THRESHOLD:]

        # 执行工具调用
        used_todo = False
        for tc in resp["tool_calls"]:
            tool_name = tc["function"]["name"]
            try:
                args = (
                    json.loads(tc["function"]["arguments"])
                    if tc["function"]["arguments"]
                    else {}
                )
            except json.JSONDecodeError as e:
                args = {}
                error_msg = f"参数 JSON 解析失败: {e}"
                tool_records.append(
                    ToolCallRecord(
                        tool_name=tool_name, arguments={}, output="", error=error_msg
                    )
                )
                tool_result_content = f"错误: {error_msg}"
                tool_result_msg = {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result_content,
                }
                messages.append(tool_result_msg)
                continue

            handler = TOOL_HANDLERS.get(tool_name)
            if handler is None:
                output, error = "", f"未知工具: {tool_name}"
            else:
                try:
                    output, error = handler(**args)
                except Exception as e:
                    output, error = "", f"工具执行异常: {str(e)}"

            tool_records.append(
                ToolCallRecord(
                    tool_name=tool_name, arguments=args, output=output, error=error
                )
            )
            tool_result_content = output if not error else f"错误: {error}"
            tool_result_msg = {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": tool_result_content,
            }
            messages.append(tool_result_msg)

            # 实时显示工具调用及结果
            print(f"\n\033[33m🔧 {tool_name}\033[0m {args}")
            preview = tool_result_content[:200] + (
                "..." if len(tool_result_content) > 200 else ""
            )
            print(f"   └─ {preview}")

            if tool_name == "todo":
                used_todo = True

        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1

    return AgentResult(
        final_answer="达到最大循环次数，可能未完成任务。",
        tool_calls=tool_records,
        error="循环超限",
    )


# ========== 交互式入口 ==========
def main():
    print("\n=== 多模型Agent循环 (流式输出 + 规划模式 + 卡死检测) ===")
    print("环境变量: LLM_API_BASE, LLM_API_KEY, LLM_MODEL")
    print("输入任务后，Agent将循环调用工具直至完成。输入 'exit' 退出。\n")

    while True:
        try:
            user_input = input("\033[36m用户 >> \033[0m")
        except EOFError, KeyboardInterrupt:
            break

        if user_input.strip().lower() in ("exit", "q", ""):
            break

        print("\n--- 执行中 ---")
        result = agent_loop(user_input)

        if result.error:
            print(f"\n\033[31m【错误】\033[0m {result.error}")
        elif result.final_answer:
            # 避免重复打印最终答案（流式输出时可能已经打印过）
            if not result.tool_calls:
                print(f"\n\033[32m【最终答案】\033[0m {result.final_answer}")

        print("\n" + "-" * 50)

    print("退出。")


if __name__ == "__main__":
    main()
