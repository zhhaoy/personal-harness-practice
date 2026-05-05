#!/usr/bin/env python3
"""
Agent Loop - 支持多模型的工具调用循环（bash + 文件操作）
兼容模型：GLM、DeepSeek、Qwen（通过OpenAI兼容API）
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

# 尝试导入OpenAI库
try:
    from openai import OpenAI, APIError
except ImportError:
    print("错误: 请安装 openai 库: pip install openai")
    sys.exit(1)

# 加载环境变量（如果存在.env文件）
try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

# ========== 配置常量 ==========
DEFAULT_TIMEOUT = 120  # 命令超时（秒）
MAX_OUTPUT_SIZE = 50000  # 工具输出最大字符数
WORKDIR = Path.cwd()  # 工作目录根路径

# 危险命令黑名单（bash）
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

请遵循以下原则：
- 优先使用工具完成任务，每一步操作后分析输出，决定下一步。
- 确保命令安全，不要执行破坏性操作。
- 文件操作会自动限制在工作目录内，无法访问外部路径。
- 完成任务后，给出清晰的最终答案。"""


# ========== 辅助函数 ==========
def safe_path(p: str) -> Path:
    """路径沙箱化：确保访问路径不逃逸工作目录"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径逃逸工作目录: {p}")
    return path


# ========== 工具实现 ==========
def run_bash(command: str, timeout: int = DEFAULT_TIMEOUT) -> Tuple[str, Optional[str]]:
    """执行 bash 命令，返回 (输出, 错误)"""
    cmd_lower = command.lower().strip()
    for blocked in BLOCKED_COMMANDS:
        if blocked in cmd_lower:
            return "", f"危险命令被阻止: {command}"

    # 跨平台 shell 处理
    if platform.system() == "Windows":
        # Windows 使用 cmd.exe
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
            output = (
                output[:MAX_OUTPUT_SIZE]
                + f"\n...[输出已截断，超出{MAX_OUTPUT_SIZE}字符]"
            )
        return output, None
    except subprocess.TimeoutExpired:
        return "", f"命令执行超时 ({timeout}秒)"
    except FileNotFoundError:
        return "", f"命令不存在或 Shell 路径错误: {command}"
    except Exception as e:
        return "", f"执行异常: {str(e)}"


def run_read(path: str, limit: Optional[int] = None) -> Tuple[str, Optional[str]]:
    """读取文件，返回 (内容, 错误)"""
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
    """写入文件，返回 (成功消息, 错误)"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"已写入 {len(content)} 字节到 {path}", None
    except Exception as e:
        return "", f"写入文件失败: {str(e)}"


def run_edit(path: str, old_text: str, new_text: str) -> Tuple[str, Optional[str]]:
    """编辑文件（精确替换第一次出现），返回 (成功消息, 错误)"""
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


# ========== 工具调度矩阵 ==========
TOOL_HANDLERS: Dict[str, Callable] = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

# OpenAI 格式的工具定义
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行一条Shell命令。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的Shell命令"}
                },
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
                    "path": {
                        "type": "string",
                        "description": "文件路径（相对于工作目录）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多显示的行数（可选）",
                    },
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
                    "path": {
                        "type": "string",
                        "description": "文件路径（相对于工作目录）",
                    },
                    "content": {"type": "string", "description": "要写入的完整内容"},
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
                    "path": {
                        "type": "string",
                        "description": "文件路径（相对于工作目录）",
                    },
                    "old_text": {"type": "string", "description": "要查找的原始文本"},
                    "new_text": {"type": "string", "description": "替换后的新文本"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
    },
]


# ========== 数据结构 ==========
@dataclass
class ToolCallRecord:
    """记录单次工具调用"""

    tool_name: str  # 工具名称
    arguments: Dict[str, Any]  # 调用参数
    output: str  # 输出内容（成功时）
    error: Optional[str] = None  # 错误信息（失败时）


@dataclass
class AgentResult:
    """Agent执行结果"""

    final_answer: str
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    error: Optional[str] = None


# ========== 多模型客户端 ==========
class MultiModelClient:
    """统一的多模型客户端（基于OpenAI兼容API）"""

    def __init__(self):
        api_base = os.getenv("LLM_API_BASE")
        api_key = os.getenv("LLM_API_KEY")
        self.model = os.getenv("LLM_MODEL")

        if not all([api_base, api_key, self.model]):
            raise ValueError("请设置环境变量: LLM_API_BASE, LLM_API_KEY, LLM_MODEL")

        self.client = OpenAI(
            base_url=api_base, api_key=api_key, timeout=60.0, max_retries=2
        )

    def chat(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        """调用模型，返回响应字典"""
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


# ========== 主控循环 ==========
def agent_loop(initial_prompt: str, max_iterations: int = 20) -> AgentResult:
    """
    主控循环：执行工具调用直到模型不再请求工具
    使用工具调度矩阵 TOOL_HANDLERS 处理不同工具
    """
    # 初始化客户端
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

    while iteration < max_iterations:
        iteration += 1

        # 调用大模型
        resp = llm.chat(messages, TOOLS)
        if resp.get("error"):
            return AgentResult(
                final_answer="", tool_calls=tool_records, error=resp["error"]
            )

        # 将 assistant 回复加入消息历史
        assistant_msg = {"role": "assistant", "content": resp["content"]}
        if resp["tool_calls"]:
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": tc["type"], "function": tc["function"]}
                for tc in resp["tool_calls"]
            ]
        messages.append(assistant_msg)

        # 如果没有工具调用，循环结束
        if not resp["tool_calls"]:
            return AgentResult(
                final_answer=resp["content"], tool_calls=tool_records, error=None
            )

        # 处理每个工具调用
        for tc in resp["tool_calls"]:
            tool_name = tc["function"]["name"]
            # 解析参数
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError as e:
                args = {}
                error_msg = f"参数 JSON 解析失败: {e}"
                output = ""
                error = error_msg
                # 仍然记录一次失败的调用
                tool_records.append(
                    ToolCallRecord(
                        tool_name=tool_name, arguments={}, output=output, error=error
                    )
                )
                # 向模型返回错误
                tool_result_content = f"错误: {error}"
                tool_result_msg = {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result_content,
                }
                messages.append(tool_result_msg)
                continue

            # 查找处理器
            handler = TOOL_HANDLERS.get(tool_name)
            if handler is None:
                output = ""
                error = f"未知工具: {tool_name}"
                tool_records.append(
                    ToolCallRecord(
                        tool_name=tool_name, arguments=args, output=output, error=error
                    )
                )
                tool_result_content = f"错误: {error}"
            else:
                # 执行工具
                try:
                    output, error = handler(**args)
                except Exception as e:
                    output = ""
                    error = f"工具执行异常: {str(e)}"

                # 记录调用
                tool_records.append(
                    ToolCallRecord(
                        tool_name=tool_name, arguments=args, output=output, error=error
                    )
                )
                tool_result_content = output if not error else f"错误: {error}"

            # 构建工具结果消息（OpenAI 格式）
            tool_result_msg = {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": tool_result_content,
            }
            messages.append(tool_result_msg)

    # 达到最大迭代次数
    return AgentResult(
        final_answer="达到最大循环次数，可能未完成任务。",
        tool_calls=tool_records,
        error="循环超限",
    )


# ========== 交互式入口 ==========
def main():
    print("\n=== 多模型Agent循环 (bash + 文件工具) ===")
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

        # 输出中间工具调用信息
        if result.tool_calls:
            print("\n\033[33m【工具调用记录】\033[0m")
            for idx, tc in enumerate(result.tool_calls, 1):
                # 格式化参数显示
                args_str = ", ".join(
                    f"{k}={repr(v)[:50]}" for k, v in tc.arguments.items()
                )
                print(f"{idx}. {tc.tool_name}({args_str})")
                if tc.error:
                    print(f"   错误: {tc.error}")
                else:
                    preview = tc.output[:200] + ("..." if len(tc.output) > 200 else "")
                    print(f"   输出: {preview}")

        # 输出最终答案
        if result.error:
            print(f"\033[31m【错误】\033[0m {result.error}")
        else:
            print("\n\033[32m【最终答案】\033[0m")
            print(result.final_answer)

        print("\n" + "-" * 50)

    print("退出。")


if __name__ == "__main__":
    main()
