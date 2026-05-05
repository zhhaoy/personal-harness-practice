#!/usr/bin/env python3
"""
Agent Loop - 流式输出 + 规划模式 + 卡死检测 + Subagent 隔离 + 预分析计划 + 待办强制完成 + 技能按需加载 + 上下文压缩 + I/O并行处理（Background Tasks）
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
import time
import threading
import uuid
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

# 上下文压缩配置
THRESHOLD = 50000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
KEEP_RECENT = 3
PRESERVE_RESULT_TOOLS = {"read_file"}


# 智能查找技能目录
def find_skills_dir() -> Path:
    env_dir = os.getenv("SKILLS_DIR")
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        if p.exists():
            return p
    cwd_skills = WORKDIR / "skills"
    if cwd_skills.exists():
        return cwd_skills
    script_dir = Path(__file__).parent.resolve()
    script_skills = script_dir / "skills"
    if script_skills.exists():
        return script_skills
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

OS_INFO = f"{platform.system()} {platform.release()}"
IS_WINDOWS = platform.system() == "Windows"


# ========== BackgroundManager: I/O并行处理 ==========
class BackgroundManager:
    """管理后台运行的长时间命令，使用线程池和通知队列"""

    def __init__(self):
        self.tasks: Dict[str, Dict] = {}
        self._notification_queue: List[Dict] = []
        self._lock = threading.Lock()

    def run(self, command: str, timeout: int = 300) -> str:
        """启动后台任务，立即返回 task_id"""
        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = {
            "status": "running",
            "result": None,
            "command": command[:200],
        }
        thread = threading.Thread(
            target=self._execute, args=(task_id, command, timeout), daemon=True
        )
        thread.start()
        return f"Background task {task_id} started: {command[:80]}"

    def _execute(self, task_id: str, command: str, timeout: int):
        """线程目标：执行子进程并收集输出"""
        try:
            # Windows 使用 cmd.exe，Unix 使用默认 shell
            if IS_WINDOWS:
                proc = subprocess.run(
                    ["cmd.exe", "/c", command],
                    cwd=WORKDIR,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            else:
                proc = subprocess.run(
                    command,
                    shell=True,
                    cwd=WORKDIR,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            output = (proc.stdout + proc.stderr).strip()
            if not output:
                output = "(无输出)"
            status = "completed"
        except subprocess.TimeoutExpired:
            output = f"错误: 任务超时 ({timeout}秒)"
            status = "timeout"
        except Exception as e:
            output = f"错误: {str(e)}"
            status = "error"
        # 截断输出到合理长度
        if len(output) > 50000:
            output = output[:50000] + "\n...[输出已截断]"
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output
        with self._lock:
            self._notification_queue.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "command": command[:80],
                    "result": output[:500],  # 通知中只保留前500字符
                }
            )

    def check(self, task_id: Optional[str] = None) -> str:
        """查询任务状态，若不提供 task_id 则列出所有"""
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return f"错误: 未知任务 {task_id}"
            result_preview = t.get("result", "")[:200] if t["result"] else "(运行中)"
            return f"[{t['status']}] {t['command']}\n{result_preview}"
        if not self.tasks:
            return "无后台任务"
        lines = []
        for tid, t in self.tasks.items():
            lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")
        return "\n".join(lines)

    def drain_notifications(self) -> List[Dict]:
        """获取并清空所有待处理的通知"""
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs


BG = BackgroundManager()


# ========== 上下文压缩辅助函数 ==========
def estimate_tokens(messages: list) -> int:
    return len(json.dumps(messages, default=str)) // 4


def micro_compact(messages: list) -> list:
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            for part_idx, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))
    if len(tool_results) <= KEEP_RECENT:
        return messages
    tool_name_map = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            if "tool_calls" in msg and msg["tool_calls"]:
                for tc in msg["tool_calls"]:
                    if "id" in tc and tc["function"]["name"]:
                        tool_name_map[tc["id"]] = tc["function"]["name"]
            elif isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name
    to_clear = tool_results[:-KEEP_RECENT]
    for _, _, result in to_clear:
        if not isinstance(result.get("content"), str) or len(result["content"]) <= 100:
            continue
        tool_id = result.get("tool_use_id", "")
        tool_name = tool_name_map.get(tool_id, "unknown")
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue
        result["content"] = f"[Previous: used {tool_name}]"
    return messages


def auto_compact(messages: list, llm_client) -> list:
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    timestamp = int(time.time())
    transcript_path = TRANSCRIPT_DIR / f"transcript_{timestamp}.jsonl"
    with open(transcript_path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    print(f"\033[90m[transcript saved: {transcript_path}]\033[0m")
    conversation_text = json.dumps(messages, default=str, ensure_ascii=False)[-80000:]
    summary_prompt = (
        "Summarize this conversation for continuity. Include:\n"
        "1) What was accomplished,\n"
        "2) Current state,\n"
        "3) Key decisions made.\n"
        "Be concise but preserve critical details.\n\n" + conversation_text
    )
    resp = llm_client._chat_no_stream([{"role": "user", "content": summary_prompt}], [])
    summary = resp.get("content", "")
    if not summary:
        summary = "No summary generated."
    compressed_content = (
        f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"
    )
    return [{"role": "user", "content": compressed_content}]


# ========== 技能加载器 ==========
class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills = {}
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
        skill = self.skills.get(name)
        if not skill:
            available = ", ".join(self.skills.keys())
            return (
                f"错误: 未知技能 '{name}'。可用技能: {available if available else '无'}"
            )
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"


SKILL_LOADER = SkillLoader(SKILLS_DIR)

# ========== 系统提示（含后台任务说明） ==========
PARENT_SYSTEM_PROMPT = f"""你是一个智能助手，当前工作目录: {WORKDIR}，操作系统: {OS_INFO}。
Windows 系统下请注意：
- 使用 `dir` 代替 `ls`，使用 `type` 代替 `cat`。
- 递归查找文件：`dir /s /b *.py`（注意路径不要加多余反引号或双反斜杠）。
- 推荐使用 read_file 工具读取文件，该工具已自动处理编码问题（UTF-8 及 GBK 兼容）。

可用工具：
- bash: 同步执行命令（阻塞）
- background_run: 后台执行长时间命令，立即返回任务ID，结果稍后可用
- check_background: 查询后台任务状态，可指定任务ID或列出所有
- read_file, write_file, edit_file: 文件操作
- todo: 创建任务列表
- task: 委托子代理
- load_skill: 加载专项技能
- compact: 手动压缩上下文

{SKILL_LOADER.get_descriptions()}

重要原则：
- 对于可能耗时较长的命令（如 `pip install`、`npm install`、`pytest`、`docker build`），请使用 `background_run`，这样你可以同时做其他工作。
- 后台任务的结果会在下次模型调用前自动注入为 <background-results> 消息。
- 你可以使用 `check_background` 主动查询任务状态。
- 其他原则详见之前的说明。
- 完成后给出清晰最终答案。"""

SUBAGENT_SYSTEM_PROMPT = f"""你是一个子代理，当前工作目录: {WORKDIR}，操作系统: {OS_INFO}。
你的任务是完成用户给出的子任务，并返回一个简洁的摘要。
你可以使用 bash, read_file, write_file, edit_file, todo, load_skill 工具，但不能再次生成子代理，也不能使用 background_run 或 check_background。
Windows 下请注意命令兼容性。使用 read_file 读取文件更可靠。
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
    if IS_WINDOWS:
        while "\\\\" in command:
            command = command.replace("\\\\", "\\")
        command = command.replace('\\"', '"')
        if command.startswith("dir") and command.endswith("\\"):
            command = command.rstrip("\\")
    if IS_WINDOWS:
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
        if "python" in command.lower():
            return (
                "",
                "错误: Python 命令未找到，请确保 Python 已安装并添加到 PATH 环境变量。",
            )
        return "", f"命令不存在或 Shell 路径错误: {command}"
    except Exception as e:
        return "", f"执行异常: {str(e)}"


def run_read(path: str, limit: Optional[int] = None) -> Tuple[str, Optional[str]]:
    try:
        fp = safe_path(path)
        if not fp.exists():
            return "", f"文件不存在: {path}"
        try:
            text = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = fp.read_text(encoding="gbk", errors="replace")
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
        fp.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字节到 {path}", None
    except Exception as e:
        return "", f"写入文件失败: {str(e)}"


def run_edit(path: str, old_text: str, new_text: str) -> Tuple[str, Optional[str]]:
    try:
        fp = safe_path(path)
        if not fp.exists():
            return "", f"文件不存在: {path}"
        content = fp.read_text(encoding="utf-8", errors="replace")
        if old_text not in content:
            return "", f"在文件 {path} 中未找到要替换的文本"
        new_content = content.replace(old_text, new_text, 1)
        fp.write_text(new_content, encoding="utf-8")
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
    content = SKILL_LOADER.get_content(name)
    if content.startswith("错误"):
        return "", content
    return content, None


def run_compact() -> Tuple[str, Optional[str]]:
    return (
        "Manual compression requested. The conversation will be summarized in the next turn.",
        None,
    )


def run_background_run(command: str) -> Tuple[str, Optional[str]]:
    """启动后台任务，立即返回任务ID"""
    result = BG.run(command)
    return result, None


def run_check_background(task_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """查询后台任务状态"""
    return BG.check(task_id), None


# ========== Subagent 实现 ==========
def run_subagent(prompt: str) -> Tuple[str, Optional[str]]:
    try:
        child_client = MultiModelClient()
    except ValueError as e:
        return "", f"子代理初始化失败: {e}"
    # 子代理不可使用 task, background_run, check_background
    forbidden = {"task", "background_run", "check_background"}
    child_tools = [t for t in TOOLS if t["function"]["name"] not in forbidden]
    sub_messages = [
        {"role": "system", "content": SUBAGENT_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    print("\033[36m" + "=" * 40 + " 启动子代理 " + "=" * 40 + "\033[0m")
    print(f"\033[36m[子代理] 任务: {prompt[:200]}\033[0m")
    max_sub_iterations = 30
    sub_iter = 0
    final_answer = ""
    sub_call_signatures = []
    sub_consecutive_identical = 0
    SUB_STUCK_THRESHOLD = 3
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
        # 卡死检测
        current_sigs = []
        for tc in resp["tool_calls"]:
            tool_name = tc["function"]["name"]
            args_str_raw = tc["function"]["arguments"]
            try:
                args = json.loads(args_str_raw) if args_str_raw else {}
            except json.JSONDecodeError:
                args = {}
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
        if sub_call_signatures and current_sig == sub_call_signatures[-1]:
            sub_consecutive_identical += 1
        else:
            sub_consecutive_identical = 1
        sub_call_signatures.append(current_sig)
        sub_call_signatures = sub_call_signatures[-SUB_STUCK_THRESHOLD:]
        if sub_consecutive_identical >= SUB_STUCK_THRESHOLD:
            print(f"\033[36m[子代理] 检测到重复调用，注入提醒\033[0m")
            reminder_msg = {
                "role": "user",
                "content": "<subagent-reminder>⚠️ 你已经重复执行相同的命令多次但都失败了。请立即改变策略：\n"
                "1. 不要再使用 bash 来运行 'dir' 或 'python' 等命令，因为似乎存在系统配置问题。\n"
                "2. 直接使用 read_file 工具来读取已知路径的文件。\n"
                "3. 如果还不行，返回一个错误摘要并结束任务。</subagent-reminder>",
            }
            sub_messages.append(reminder_msg)
            sub_consecutive_identical = 0
            sub_call_signatures = []
            continue
        # 执行工具调用
        for tc in resp["tool_calls"]:
            tool_name = tc["function"]["name"]
            try:
                args = (
                    json.loads(tc["function"]["arguments"])
                    if tc["function"]["arguments"]
                    else {}
                )
            except json.JSONDecodeError:
                args = {}
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
    "compact": lambda **kw: run_compact(),
    "background_run": lambda **kw: run_background_run(kw["command"]),
    "check_background": lambda **kw: run_check_background(kw.get("task_id")),
}

# 工具定义 (OpenAI 格式)
BASE_TOOLS_DEF = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行一条Shell命令（阻塞）。Windows下请使用 `dir` 代替 `ls`，使用 `type` 代替 `cat`。",
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
            "description": "读取文件内容（自动处理 UTF-8/GBK 编码）。",
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
        "description": "按需加载专项技能知识（如 git、测试、PDF处理等）。",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "技能名称"}},
            "required": ["name"],
        },
    },
}

COMPACT_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "compact",
        "description": "手动触发上下文压缩。当对话历史过长影响效率时调用。",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

BACKGROUND_RUN_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "background_run",
        "description": "在后台运行长时间命令（如 pip install, pytest）。立即返回任务ID，结果稍后自动注入对话。",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}

CHECK_BACKGROUND_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "check_background",
        "description": "检查后台任务状态。若不提供 task_id，则列出所有后台任务。",
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": [],
        },
    },
}

PARENT_TOOLS = BASE_TOOLS_DEF + [
    TODO_TOOL_DEF,
    TASK_TOOL_DEF,
    LOAD_SKILL_TOOL_DEF,
    COMPACT_TOOL_DEF,
    BACKGROUND_RUN_TOOL_DEF,
    CHECK_BACKGROUND_TOOL_DEF,
]
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


# ========== 多模型客户端 ==========
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
                tools=tools if tools else None,
                tool_choice="auto" if tools else None,
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


# ========== 主控循环（含上下文压缩和子代理卡死检测） ==========
def agent_loop(initial_prompt: str, max_iterations: int = 30) -> AgentResult:
    try:
        llm = MultiModelClient()
    except ValueError as e:
        return AgentResult(final_answer="", error=str(e))

    # 预分析
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

        # Layer 1: micro_compact
        micro_compact(messages)

        # Layer 2: auto_compact
        if estimate_tokens(messages) > THRESHOLD:
            print("\033[90m[auto_compact triggered]\033[0m")
            messages = auto_compact(messages, llm)
            call_signatures = []
            consecutive_identical = 0
            rounds_since_todo = 0

        # 注入后台任务通知（在每个 LLM 调用前）
        notifs = BG.drain_notifications()
        if notifs:
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )
            messages.append(
                {
                    "role": "user",
                    "content": f"<background-results>\n{notif_text}\n</background-results>",
                }
            )

        # 强制待办提醒
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

        if rounds_since_todo >= 3:
            reminder_msg = {
                "role": "user",
                "content": "<reminder>你已连续多轮未更新任务列表(todo)，请使用 todo 工具规划或更新当前进度。</reminder>",
            }
            messages.append(reminder_msg)
            rounds_since_todo = 0

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

        # 调用模型
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

        # 生成签名用于卡死检测
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
        manual_compact = False
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

            # 显示
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
            elif tool_name == "compact":
                print(f"\n\033[33m🔧 {tool_name}\033[0m 手动压缩请求")
                print(f"   └─ {tool_result_content}")
                manual_compact = True
            elif tool_name == "background_run":
                print(
                    f"\n\033[33m🔧 {tool_name}\033[0m {args.get('command', '')[:100]}"
                )
                print(f"   └─ {tool_result_content}")
            elif tool_name == "check_background":
                print(f"\n\033[33m🔧 {tool_name}\033[0m {args.get('task_id', 'all')}")
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

        if manual_compact:
            print("\033[90m[manual compact triggered]\033[0m")
            messages = auto_compact(messages, llm)
            call_signatures = []
            consecutive_identical = 0
            rounds_since_todo = 0

    return AgentResult(
        final_answer="达到最大循环次数，可能未完成任务。",
        tool_calls=tool_records,
        error="循环超限",
    )


# ========== 交互式入口 ==========
def main():
    print(
        "\n=== 多模型Agent循环 (流式 + 规划 + 卡死检测 + Subagent + 待办强制完成 + 技能按需加载 + 上下文压缩 + I/O并行) ==="
    )
    print("环境变量: LLM_API_BASE, LLM_API_KEY, LLM_MODEL")
    print(
        f"技能目录查找顺序: 环境变量 SKILLS_DIR -> 当前工作目录/skills -> 脚本所在目录/skills"
    )
    print(f"当前技能目录: {SKILLS_DIR}")
    print(f"上下文压缩阈值: {THRESHOLD} tokens, 自动保存转录本到: {TRANSCRIPT_DIR}")
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
