#!/usr/bin/env python3
"""
Agent Loop - 流式输出 + 规划模式 + 卡死检测 + Subagent 隔离 + 预分析计划 + 待办强制完成 + 技能按需加载（增强路径查找）
兼容模型：GLM、DeepSeek、Qwen（OpenAI 兼容 API）
环境变量：
    LLM_API_BASE   - API基础URL（必需）
    LLM_API_KEY    - API密钥（必需）
    LLM_MODEL      - 模型名称（必需）
    SKILLS_DIR     - 可选，指定技能目录路径
"""

import os
import subprocess
import sys
import json
import platform
import re
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

# 尝试导入 yaml（用于解析技能 frontmatter）
try:
    import yaml
except ImportError:
    print("警告: 未安装 PyYAML，技能加载功能将不可用。请运行: pip install pyyaml")
    yaml = None

# ========== 配置常量 ==========
DEFAULT_TIMEOUT = 120
MAX_OUTPUT_SIZE = 50000
WORKDIR = Path.cwd()


# 智能查找技能目录
def find_skills_dir() -> Path:
    """多级查找技能目录：环境变量 -> 当前工作目录/skills -> 脚本所在目录/skills"""
    # 1. 环境变量
    env_dir = os.getenv("SKILLS_DIR")
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        if p.exists():
            return p
    # 2. 当前工作目录下的 skills
    cwd_skills = WORKDIR / "skills"
    if cwd_skills.exists():
        return cwd_skills
    # 3. 脚本所在目录下的 skills
    script_dir = Path(__file__).parent.resolve()
    script_skills = script_dir / "skills"
    if script_skills.exists():
        return script_skills
    # 4. 返回一个默认路径（即使不存在，后续会提示）
    return WORKDIR / "skills"


SKILLS_DIR = find_skills_dir()

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


# ========== 技能加载器 ==========
class SkillLoader:
    """扫描技能目录，解析 SKILL.md 的 frontmatter 和正文，提供按需加载"""

    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills = {}  # name -> {"meta": {...}, "body": str, "path": str}
        self._load_all()
        if not self.skills:
            print(
                f"\033[33m[警告] 未找到任何技能，请将技能目录放置在以下位置之一：\n"
                f"   - 环境变量 SKILLS_DIR 指定的路径\n"
                f"   - 当前工作目录下的 skills/ 目录 ({WORKDIR / 'skills'})\n"
                f"   - 脚本所在目录下的 skills/ 目录 ({Path(__file__).parent / 'skills'})\033[0m"
            )

    def _load_all(self):
        if not self.skills_dir.exists():
            return
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            try:
                text = f.read_text(encoding="utf-8")
                meta, body = self._parse_frontmatter(text)
                name = meta.get("name", f.parent.name)
                self.skills[name] = {"meta": meta, "body": body, "path": str(f)}
                print(f"[技能加载] 已加载: {name} 来自 {f.parent.name}")
            except Exception as e:
                print(f"警告: 加载技能文件 {f} 失败: {e}")

    def _parse_frontmatter(self, text: str) -> tuple:
        """解析 YAML frontmatter，返回 (meta_dict, body_str)"""
        if yaml is None:
            return {}, text
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        return meta, match.group(2).strip()

    def get_descriptions(self) -> str:
        """Layer 1: 返回技能简短描述，用于注入系统提示"""
        if not self.skills:
            return "(无可用技能)"
        lines = ["可用技能（使用 load_skill 工具加载详细内容）："]
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "无描述")
            tags = skill["meta"].get("tags", "")
            line = f"  - {name}: {desc}"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)

    def get_content(self, name: str) -> str:
        """Layer 2: 返回技能的完整正文"""
        skill = self.skills.get(name)
        if not skill:
            available = ", ".join(self.skills.keys())
            return (
                f"错误: 未知技能 '{name}'。可用技能: {available if available else '无'}"
            )
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"


# 初始化全局技能加载器
SKILL_LOADER = SkillLoader(SKILLS_DIR)

# ========== 系统提示（含技能列表 Layer 1） ==========
PARENT_SYSTEM_PROMPT = f"""你是一个智能助手，当前工作目录: {WORKDIR}。
你可以使用以下工具：
- bash, read_file, write_file, edit_file：常规文件操作
- todo：创建任务列表，规划多步骤工作
- task：将子任务委托给一个干净的 subagent（拥有全新上下文，但共享文件系统）。subagent 完成后会返回摘要。
- load_skill：按需加载专项技能知识（如 git、测试、PDF处理等）。当遇到不熟悉的领域时，先调用此工具获取指导。

{SKILL_LOADER.get_descriptions()}

原则：
- 对于需要独立探索、大量工具调用但不想污染主对话历史的任务，使用 task 工具。
- 优先使用 todo 规划复杂任务。如果任务需要多于2个步骤，请首先调用 todo 创建计划。
- 委托子任务（task）后，如果子代理返回的结果已经足够，请直接使用该结果回答用户，不要重复委托相同的子任务。
- 如果子代理多次返回相同的不完整结果，你应该改变策略（例如换个角度提问、直接向用户请求澄清或使用其他工具），而不是反复调用 task。
- 如果连续尝试相同命令多次没有进展，改变策略。
- 【重要】使用 todo 列表管理多步骤任务时：
   * 每个任务完成后，必须立即调用 todo 工具将其状态改为 completed。
   * 如果某个任务需要委托给子代理（task），请在子代理返回结果后，将该任务标记为 completed，然后立即开始处理下一个 pending 任务。
   * 只有 todo 列表中所有任务的状态都为 completed 时，你才能输出最终答案。
   * 绝对不能在所有任务完成之前就输出最终答案或结束对话。
- 对于不熟悉的任务领域，先调用 load_skill 加载相关技能知识，再执行具体操作。
- 完成后给出清晰最终答案。"""

SUBAGENT_SYSTEM_PROMPT = f"""你是一个子代理，当前工作目录: {WORKDIR}。
你的任务是完成用户给出的子任务，并返回一个简洁的摘要。
你可以使用 bash, read_file, write_file, edit_file, todo, load_skill 工具，但不能再次生成子代理。
完成后，请只输出任务结果或摘要，不要额外解释。"""


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


def run_load_skill(name: str) -> Tuple[str, Optional[str]]:
    """加载技能知识（按需注入）"""
    content = SKILL_LOADER.get_content(name)
    if content.startswith("错误"):
        return "", content
    return content, None


# ========== Subagent 实现 ==========
def run_subagent(prompt: str) -> Tuple[str, Optional[str]]:
    """子代理：独立上下文，非流式执行但输出详细过程，与父代理区分。"""
    try:
        child_client = MultiModelClient()
    except ValueError as e:
        return "", f"子代理初始化失败: {e}"

    # 子代理不可使用 task，避免递归和额外复杂度
    child_tools = [t for t in TOOLS if t["function"]["name"] not in ("task",)]

    sub_messages = [
        {"role": "system", "content": SUBAGENT_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    print("\033[36m" + "=" * 40 + " 启动子代理 " + "=" * 40 + "\033[0m")
    print(f"\033[36m[子代理] 任务: {prompt[:200]}\033[0m")

    max_sub_iterations = 30
    sub_iter = 0
    final_answer = ""

    while sub_iter < max_sub_iterations:
        sub_iter += 1
        resp = child_client._chat_no_stream(sub_messages, child_tools)
        if resp.get("error"):
            error_msg = f"子代理模型错误: {resp['error']}"
            print(f"\033[36m[子代理] \033[31m错误: {error_msg}\033[0m")
            return "", error_msg

        if resp["content"]:
            print(f"\033[36m[子代理] 模型:\033[0m {resp['content']}")

        assistant_msg = {"role": "assistant", "content": resp["content"]}
        if resp["tool_calls"]:
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": tc["type"], "function": tc["function"]}
                for tc in resp["tool_calls"]
            ]
        sub_messages.append(assistant_msg)

        if not resp["tool_calls"]:
            final_answer = resp["content"]
            print(f"\033[36m[子代理] 完成，返回摘要:\033[0m {final_answer[:200]}")
            print("\033[36m" + "=" * 90 + "\033[0m")
            return final_answer, None

        for tc in resp["tool_calls"]:
            tool_name = tc["function"]["name"]
            try:
                args = (
                    json.loads(tc["function"]["arguments"])
                    if tc["function"]["arguments"]
                    else {}
                )
            except json.JSONDecodeError as e:
                output = f"参数解析错误: {e}"
                error = output
            else:
                handler = TOOL_HANDLERS.get(tool_name)
                if handler is None:
                    output, error = "", f"未知工具: {tool_name}"
                else:
                    try:
                        output, error = handler(**args)
                    except Exception as e:
                        output, error = "", f"执行异常: {e}"

            tool_result_content = output if not error else f"错误: {error}"
            sub_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result_content,
                }
            )

            if tool_name == "todo":
                print(f"\n\033[36m[子代理] 🔧 {tool_name} (任务列表)\033[0m")
                print(f"\033[36m{chr(9472)} {tool_result_content}\033[0m")
            else:
                args_display = {
                    k: (v[:50] + "..." if isinstance(v, str) and len(v) > 50 else v)
                    for k, v in args.items()
                }
                print(f"\n\033[36m[子代理] 🔧 {tool_name}\033[0m {args_display}")
                preview = tool_result_content[:200] + (
                    "..." if len(tool_result_content) > 200 else ""
                )
                print(f"\033[36m  └─ {preview}\033[0m")

    print(f"\033[36m[子代理] 达到最大迭代次数 ({max_sub_iterations})\033[0m")
    print("\033[36m" + "=" * 90 + "\033[0m")
    return "子代理达到最大迭代次数，可能未完成", None


# ========== 工具调度矩阵（父代理） ==========
TOOL_HANDLERS: Dict[str, Callable] = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo": lambda **kw: run_todo(kw["items"]),
    "task": lambda **kw: run_subagent(kw["prompt"]),
    "load_skill": lambda **kw: run_load_skill(kw["name"]),
}

# 工具定义 (OpenAI 格式)
BASE_TOOLS_DEF = [
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
]

TODO_TOOL_DEF = {
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
}

TASK_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "task",
        "description": "启动一个子代理，拥有全新上下文（但共享文件系统）。子代理会独立完成任务并返回摘要。",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "要委托给子代理的任务描述"}
            },
            "required": ["prompt"],
        },
    },
}

LOAD_SKILL_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "按需加载专项技能知识（如 git、测试、PDF处理等）。当遇到不熟悉的领域时，先调用此工具获取指导。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "技能名称，例如 'git', 'code-review', 'pdf'",
                }
            },
            "required": ["name"],
        },
    },
}

PARENT_TOOLS = BASE_TOOLS_DEF + [TODO_TOOL_DEF, TASK_TOOL_DEF, LOAD_SKILL_TOOL_DEF]
TOOLS = PARENT_TOOLS


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

        print()
        formatted_tool_calls = []
        for tc in collected_tool_calls:
            if tc["function"]["name"]:
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


# ========== 主控循环（含预分析计划、规划提醒、卡死检测、待办强制完成） ==========
def agent_loop(initial_prompt: str, max_iterations: int = 30) -> AgentResult:
    try:
        llm = MultiModelClient()
    except ValueError as e:
        return AgentResult(final_answer="", error=str(e))

    # ========== 预分析：判断是否复杂任务并生成初始 todo ==========
    print("\033[90m[预分析...]\033[0m")
    analysis_messages = [
        {
            "role": "system",
            "content": "你是一个任务分析器。分析用户请求，如果完成任务需要超过2个步骤，请输出一个 todo 列表（JSON数组，每个元素含 id, text, status='pending'）；否则只输出 'SIMPLE'。",
        },
        {"role": "user", "content": initial_prompt},
    ]
    analysis_resp = llm._chat_no_stream(analysis_messages, [])
    analysis_text = analysis_resp.get("content", "").strip()

    messages = [
        {"role": "system", "content": PARENT_SYSTEM_PROMPT},
        {"role": "user", "content": initial_prompt},
    ]
    tool_records: List[ToolCallRecord] = []

    # 如果是复杂任务，解析并注入 todo 计划
    if analysis_text != "SIMPLE" and (
        analysis_text.startswith("[") or analysis_text.startswith("{")
    ):
        try:
            todo_items = json.loads(analysis_text)
            if isinstance(todo_items, list):
                todo_output, _ = run_todo(todo_items)
                plan_msg = {
                    "role": "assistant",
                    "content": "根据任务复杂度，我已为您创建以下任务计划：",
                }
                tool_result_msg = {
                    "role": "tool",
                    "tool_call_id": "preanalysis",
                    "content": todo_output,
                }
                messages.append(plan_msg)
                messages.append(tool_result_msg)
                tool_records.append(
                    ToolCallRecord(
                        tool_name="todo",
                        arguments={"items": todo_items},
                        output=todo_output,
                        error=None,
                    )
                )
                print(f"\033[32m[自动计划] 已生成任务列表:\n{todo_output}\033[0m")
        except json.JSONDecodeError:
            print("[分析] JSON 解析失败，不注入计划")
    else:
        print("[分析] 任务较简单，无需预规划")

    iteration = 0
    rounds_since_todo = 0
    STUCK_THRESHOLD = 2
    call_signatures = []
    consecutive_identical = 0

    while iteration < max_iterations:
        iteration += 1

        # ========== 强制待办提醒：若存在未完成任务，在调用模型前注入 ==========
        todo_state = TODO.render()
        has_incomplete = (
            any(item["status"] != "completed" for item in TODO.items)
            if TODO.items
            else False
        )
        if has_incomplete:
            progress_msg = {
                "role": "user",
                "content": f"【当前待办进度】\n{todo_state}\n\n请继续执行下一个未完成的任务。不要输出最终答案。",
            }
            messages.append(progress_msg)

        # 提醒 todo (连续未使用)
        if rounds_since_todo >= 3:
            reminder_msg = {
                "role": "user",
                "content": "<reminder>你已连续多轮未更新任务列表(todo)，请使用 todo 工具规划或更新当前进度。</reminder>",
            }
            messages.append(reminder_msg)
            rounds_since_todo = 0

        # 提醒卡死
        if consecutive_identical >= STUCK_THRESHOLD:
            last_sig = call_signatures[-1] if call_signatures else ""
            if "task:" in last_sig:
                stuck_msg = {
                    "role": "user",
                    "content": "<system-reminder>⚠️ 你已经连续多次调用 task 工具委托相同的子任务，但未获得进展。请立即停止重复委托。如果子任务已完成，请直接给出最终答案；如果任务描述不清晰，请向用户请求更多信息。不要再调用 task 工具。</system-reminder>",
                }
            else:
                stuck_msg = {
                    "role": "user",
                    "content": "<system-reminder>⚠️ 检测到你在连续多轮中重复调用相同的工具且参数相同，似乎没有取得进展。请尝试不同的命令、工具或方法，或检查是否已完成任务。如果已经完成，请给出最终答案。</system-reminder>",
                }
            messages.append(stuck_msg)
            consecutive_identical = 0
            call_signatures = []

        # 调用模型（流式）
        print("\033[90m[模型思考]\033[0m ", end="", flush=True)
        resp = llm.chat(messages, TOOLS, stream=True)
        print()

        if resp.get("error"):
            return AgentResult(
                final_answer="", tool_calls=tool_records, error=resp["error"]
            )

        assistant_msg = {"role": "assistant", "content": resp["content"]}
        if resp["tool_calls"]:
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": tc["type"], "function": tc["function"]}
                for tc in resp["tool_calls"]
            ]
        messages.append(assistant_msg)

        # ==== 若无工具调用，检查待办是否全部完成 ====
        if not resp["tool_calls"]:
            todo_state = TODO.render()
            has_incomplete = (
                any(item["status"] != "completed" for item in TODO.items)
                if TODO.items
                else False
            )
            if has_incomplete:
                reminder = {
                    "role": "user",
                    "content": f"【尚未完成】你尝试结束，但以下任务仍待完成：\n{todo_state}\n请继续执行未完成的任务。",
                }
                messages.append(reminder)
                continue
            else:
                return AgentResult(
                    final_answer=resp["content"], tool_calls=tool_records, error=None
                )

        # 生成调用签名（task 特殊处理）
        current_sigs = []
        for tc in resp["tool_calls"]:
            tool_name = tc["function"]["name"]
            args_str_raw = tc["function"]["arguments"]
            try:
                args = json.loads(args_str_raw) if args_str_raw else {}
            except json.JSONDecodeError:
                args = {}

            if tool_name == "task":
                prompt = args.get("prompt", "")
                prompt_key = (
                    prompt.strip()[:200]
                    if isinstance(prompt, str)
                    else str(prompt)[:200]
                )
                sig_value = f"task:prompt={prompt_key}"
            else:
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
                sig_value = f"{tool_name}:{args_signature}"
            current_sigs.append(sig_value)
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
                print(f"\n\033[33m🔧 {tool_name}\033[0m 参数解析失败")
                print(f"   └─ {error_msg}")
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

            # 显示工具调用（特殊处理 todo, task, load_skill）
            if tool_name == "todo":
                print(f"\n\033[33m🔧 {tool_name} (任务列表更新)\033[0m")
                print(tool_result_content)
            elif tool_name == "task":
                print(f"\n\033[33m🔧 {tool_name}\033[0m 委托子任务")
                preview = tool_result_content[:200] + (
                    "..." if len(tool_result_content) > 200 else ""
                )
                print(f"   └─ 子代理返回: {preview}")
            elif tool_name == "load_skill":
                print(
                    f"\n\033[33m🔧 {tool_name}\033[0m 加载技能: {args.get('name', '')}"
                )
                preview = tool_result_content[:200] + (
                    "..." if len(tool_result_content) > 200 else ""
                )
                print(f"   └─ {preview}")
            else:
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
    print(
        "\n=== 多模型Agent循环 (流式 + 规划 + 卡死检测 + Subagent + 待办强制完成 + 技能按需加载) ==="
    )
    print("环境变量: LLM_API_BASE, LLM_API_KEY, LLM_MODEL")
    print(
        f"技能目录查找顺序: 环境变量 SKILLS_DIR -> 当前工作目录/skills -> 脚本所在目录/skills"
    )
    print(f"当前技能目录: {SKILLS_DIR}")
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
            if not result.tool_calls and result.final_answer:
                print(f"\n\033[32m【最终答案】\033[0m {result.final_answer}")

        print("\n" + "-" * 50)

    print("退出。")


if __name__ == "__main__":
    main()
