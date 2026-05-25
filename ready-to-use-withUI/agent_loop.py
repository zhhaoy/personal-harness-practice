#!/usr/bin/env python3
"""
Agent Loop with Task Isolation (Worktree + Task Binding) + Pluggable Tools
- 每个任务拥有独立的 git worktree 目录，并行执行永不冲突
- 任务状态持久化在 .tasks/ 目录，工作树状态记录在 .worktrees/index.json
- 队友可自动认领任务、创建工作树、在隔离环境中运行命令，完成后清理
- 队友可被激活（从 shutdown 恢复），继承身份和收件箱历史
- 可插拔工具系统：支持动态注册/启用/禁用工具，内置工具不可编辑，自定义工具由 Agent 生成并验证
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
import inspect
import traceback
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Callable
from dataclasses import dataclass, field, asdict
import importlib.util
import ast

# 第三方库导入（带 graceful fallback）
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.lexers import PygmentsLexer
    from pygments.lexers import PythonLexer
    from prompt_toolkit.key_binding import KeyBindings
except ImportError:
    print("警告: prompt_toolkit 未安装，请运行: pip install prompt_toolkit pygments")
    PromptSession = None

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

try:
    import yaml
except ImportError:
    print("警告: 未安装 PyYAML，技能加载功能将不可用。请运行: pip install pyyaml")
    yaml = None

# 导入元操作总管家
try:
    from meta_dispatcher import (
        run_meta_dispatch, run_meta_step, run_meta_status, run_meta_list,
        get_dispatcher, init_dispatcher, MetaDispatcherCore, ParadigmType
    )
    META_DISPATCHER_AVAILABLE = True
except ImportError as e:
    print(f"警告: meta_dispatcher 模块未找到: {e}")
    META_DISPATCHER_AVAILABLE = False

# ========== 配置常量 ==========
DEFAULT_TIMEOUT = 120
MAX_OUTPUT_SIZE = 50000
WORKDIR = Path.cwd()

THRESHOLD = 50000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
KEEP_RECENT = 3
PRESERVE_RESULT_TOOLS = {"read_file"}

TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
POLL_INTERVAL = 5           # 空闲轮询间隔（秒）
IDLE_TIMEOUT = 60           # 空闲超时时间（秒），超后自动关机
MAX_TOOL_CALLS_PER_PHASE = 30
MAX_TOTAL_STEPS = 200
STUCK_SIMILAR_THRESHOLD = 2

# 自定义工具存储目录
CUSTOM_TOOLS_DIR = WORKDIR / ".tools"
CUSTOM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
CUSTOM_TOOLS_META_PATH = CUSTOM_TOOLS_DIR / "custom_tools.json"

# ========== 队友消息回调（供 Web UI 使用） ==========
TEAMMATE_CALLBACK = None

def set_teammate_callback(callback):
    """设置队友消息回调函数，参数为 (teammate_name, subtype, data)"""
    global TEAMMATE_CALLBACK
    TEAMMATE_CALLBACK = callback
# ==========

def run_builtin_command(cmd: str) -> bool:
    """处理内置命令，返回 True 表示已处理，不需要调用 agent_loop"""
    if cmd == "/help":
        print("\033[36m可用命令：\033[0m")
        print("  /help       显示此帮助")
        print("  /tasks      列出所有任务")
        print("  /worktrees  列出所有工作树")
        print("  /todo       显示当前待办")
        print("  /teammates  显示队友状态")
        print("  /exit       退出程序")
        print("\n其他输入将直接发送给 Agent 处理。")
        return True
    if cmd == "/tasks":
        print(TASKS.list_all())
        return True
    if cmd == "/worktrees":
        print(WORKTREES.list_all())
        return True
    if cmd == "/todo":
        print(TODO.render())
        return True
    if cmd == "/teammates":
        print(TEAM.list_all())
        return True
    if cmd in ("/exit", "/quit"):
        print("再见！")
        sys.exit(0)
    return False

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
    "rm -rf /", "sudo", "shutdown", "reboot",
    "mkfs", "dd if=", "> /dev/", "chmod 777"
]

OS_INFO = f"{platform.system()} {platform.release()}"
IS_WINDOWS = platform.system() == "Windows"

_shutdown_requests: Dict[str, Dict] = {}
_plan_requests: Dict[str, Dict] = {}
_tracker_lock = threading.Lock()
_claim_lock = threading.Lock()

# ========== Git 仓库根目录检测 ==========
def detect_repo_root(cwd: Path) -> Path | None:
    """Return git repo root if cwd is inside a repo, else None."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            return None
        root = Path(r.stdout.strip())
        return root if root.exists() else None
    except Exception:
        return None

REPO_ROOT = detect_repo_root(WORKDIR) or WORKDIR

FORBIDDEN_FUNCS = {"open", "eval", "exec", "__import__", "compile", "globals",
            "locals", "vars", "dir", "help", "input", "raw_input"}
FORBIDDEN_ATTR = {"system", "popen", "remove", "unlink", "rmdir", "rename",
                "chdir", "getenv", "putenv", "environ", "run", "Popen"}

def validate_tool_code_safety(code: str) -> Tuple[bool, str]:
    """返回 (是否安全, 错误信息)"""
    # 语法检查
    try:
        ast.parse(code)
    except SyntaxError as e:
        return False, f"语法错误: {e}"

    # 遍历 AST
    tree = ast.parse(code)
    for node in ast.walk(tree):
        # 禁止调用危险函数
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in FORBIDDEN_FUNCS:
                    return False, f"禁止调用函数: {node.func.id}"
            elif isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_ATTR:
                    return False, f"禁止调用属性: {node.func.attr}"
                # 检查是否是 os.subprocess 等危险对象的方法
                if isinstance(node.func.value, ast.Name):
                    if node.func.value.id in {"os", "subprocess", "shutil", "sys"}:
                        return False, f"禁止通过 {node.func.value.id} 调用 {node.func.attr}"
    return True, ""

# ========== EventBus: 生命周期事件记录 ==========
class EventBus:
    def __init__(self, event_log_path: Path):
        self.path = event_log_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("")

    def emit(
        self,
        event: str,
        task: dict | None = None,
        worktree: dict | None = None,
        error: str | None = None,
    ):
        payload = {
            "event": event,
            "ts": time.time(),
            "task": task or {},
            "worktree": worktree or {},
        }
        if error:
            payload["error"] = error
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

    def list_recent(self, limit: int = 20) -> str:
        n = max(1, min(int(limit or 20), 200))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        recent = lines[-n:]
        items = []
        for line in recent:
            try:
                items.append(json.loads(line))
            except Exception:
                items.append({"event": "parse_error", "raw": line})
        return json.dumps(items, indent=2)

# ========== TaskManager: 持久任务板，支持 worktree 绑定 ==========
class TaskManager:
    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        ids = []
        for f in self.dir.glob("task_*.json"):
            try:
                ids.append(int(f.stem.split("_")[1]))
            except Exception:
                pass
        return max(ids) if ids else 0

    def _path(self, task_id: int) -> Path:
        return self.dir / f"task_{task_id}.json"

    def _load(self, task_id: int) -> dict:
        path = self._path(task_id)
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text())

    def _save(self, task: dict):
        self._path(task["id"]).write_text(json.dumps(task, indent=2))

    def create(self, subject: str, description: str = "") -> str:
        task = {
            "id": self._next_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "owner": "",
            "worktree": "",
            "blockedBy": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2)

    def get(self, task_id: int) -> str:
        return json.dumps(self._load(task_id), indent=2)

    def exists(self, task_id: int) -> bool:
        return self._path(task_id).exists()

    def update(self, task_id: int, status: str = None, owner: str = None) -> str:
        task = self._load(task_id)
        if status:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status
        if owner is not None:
            task["owner"] = owner
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2)

    def bind_worktree(self, task_id: int, worktree: str, owner: str = "") -> str:
        task = self._load(task_id)
        task["worktree"] = worktree
        if owner:
            task["owner"] = owner
        if task["status"] == "pending":
            task["status"] = "in_progress"
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2)

    def unbind_worktree(self, task_id: int) -> str:
        task = self._load(task_id)
        task["worktree"] = ""
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2)

    def list_all(self) -> str:
        tasks = []
        for f in sorted(self.dir.glob("task_*.json")):
            tasks.append(json.loads(f.read_text()))
        if not tasks:
            return "No tasks."
        lines = []
        for t in tasks:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }.get(t["status"], "[?]")
            owner = f" owner={t['owner']}" if t.get("owner") else ""
            wt = f" wt={t['worktree']}" if t.get("worktree") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{owner}{wt}")
        return "\n".join(lines)

    def find_pending_unclaimed(self) -> list:
        """返回所有状态为 pending 且无 owner 的任务列表"""
        pending = []
        for f in self.dir.glob("task_*.json"):
            try:
                task = json.loads(f.read_text())
                if task.get("status") == "pending" and not task.get("owner"):
                    pending.append(task)
            except Exception:
                continue
        return pending

TASKS = TaskManager(REPO_ROOT / ".tasks")
EVENTS = EventBus(REPO_ROOT / ".worktrees" / "events.jsonl")

# ========== WorktreeManager: Git worktree 生命周期 + 索引 ==========
class WorktreeManager:
    def __init__(self, repo_root: Path, tasks: TaskManager, events: EventBus):
        self.repo_root = repo_root
        self.tasks = tasks
        self.events = events
        self.dir = repo_root / ".worktrees"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        if not self.index_path.exists():
            self.index_path.write_text(json.dumps({"worktrees": []}, indent=2))
        self.git_available = self._is_git_repo()

    def _is_git_repo(self) -> bool:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _run_git(self, args: list[str]) -> str:
        if not self.git_available:
            raise RuntimeError("Not in a git repository. worktree tools require git.")
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=120,
            )
            if r.returncode != 0:
                msg = (r.stdout + r.stderr).strip()
                raise RuntimeError(msg or f"git {' '.join(args)} failed")
            out = (r.stdout + r.stderr).strip()
            return out if out else "(no output)"
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Command timed out: git {' '.join(args)}")

    def _load_index(self) -> dict:
        return json.loads(self.index_path.read_text())

    def _save_index(self, data: dict):
        self.index_path.write_text(json.dumps(data, indent=2))

    def _find(self, name: str) -> dict | None:
        idx = self._load_index()
        for wt in idx.get("worktrees", []):
            if wt.get("name") == name:
                return wt
        return None

    def _validate_name(self, name: str):
        if not name or len(name) > 100:
            raise ValueError(f"Worktree name must be 1-100 characters, got '{name}'")
        invalid_chars = set(r'\/:*?"<>|')
        if any(c in invalid_chars for c in name):
            raise ValueError(f"Worktree name contains invalid characters: {name}. Allowed: most characters except {''.join(invalid_chars)}")

    def create(self, name: str, task_id: int = None, base_ref: str = "HEAD") -> str:
        self._validate_name(name)
        if self._find(name):
            raise ValueError(f"Worktree '{name}' already exists in index")
        if task_id is not None and not self.tasks.exists(task_id):
            raise ValueError(f"Task {task_id} not found")

        path = self.dir / name
        branch = f"wt/{name}"

        # 检查分支是否已经存在
        branch_exists = False
        try:
            self._run_git(["show-ref", "--verify", f"refs/heads/{branch}"])
            branch_exists = True
        except RuntimeError:
            branch_exists = False

        self.events.emit(
            "worktree.create.before",
            task={"id": task_id} if task_id is not None else {},
            worktree={"name": name, "base_ref": base_ref},
        )
        try:
            if branch_exists:
                # 分支已存在：直接使用该分支创建 worktree，不再创建新分支
                self._run_git(["worktree", "add", str(path), branch])
            else:
                # 分支不存在：创建新分支
                self._run_git(["worktree", "add", "-b", branch, str(path), base_ref])

            entry = {
                "name": name,
                "path": str(path),
                "branch": branch,
                "task_id": task_id,
                "status": "active",
                "created_at": time.time(),
            }
            idx = self._load_index()
            idx["worktrees"].append(entry)
            self._save_index(idx)

            if task_id is not None:
                self.tasks.bind_worktree(task_id, name)

            self.events.emit(
                "worktree.create.after",
                task={"id": task_id} if task_id is not None else {},
                worktree={
                    "name": name,
                    "path": str(path),
                    "branch": branch,
                    "status": "active",
                },
            )
            return json.dumps(entry, indent=2)
        except Exception as e:
            self.events.emit(
                "worktree.create.failed",
                task={"id": task_id} if task_id is not None else {},
                worktree={"name": name, "base_ref": base_ref},
                error=str(e),
            )
            raise

    def list_all(self) -> str:
        idx = self._load_index()
        wts = idx.get("worktrees", [])
        if not wts:
            return "No worktrees in index."
        lines = []
        for wt in wts:
            suffix = f" task={wt['task_id']}" if wt.get("task_id") else ""
            lines.append(
                f"[{wt.get('status', 'unknown')}] {wt['name']} -> "
                f"{wt['path']} ({wt.get('branch', '-')}){suffix}"
            )
        return "\n".join(lines)

    def status(self, name: str) -> str:
        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"
        path = Path(wt["path"])
        if not path.exists():
            return f"Error: Worktree path missing: {path}"
        r = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        text = (r.stdout + r.stderr).strip()
        return text or "Clean worktree"

    def run(self, name: str, command: str, timeout: int = 600) -> str:
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(d in command for d in dangerous):
            return "Error: Dangerous command blocked"

        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"
        path = Path(wt["path"])
        if not path.exists():
            return f"Error: Worktree path missing: {path}"

        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
            )
            output_lines = []
            timer = threading.Timer(timeout, process.terminate)
            timer.start()
            try:
                for line in process.stdout:
                    print(f"\033[36m[worktree {name}] {line.rstrip()}\033[0m")
                    output_lines.append(line)
                    if len(''.join(output_lines)) > MAX_OUTPUT_SIZE:
                        output_lines.append("...[输出过长已截断]...\n")
                        break
            finally:
                timer.cancel()
            process.wait(timeout=1)
            out = ''.join(output_lines).strip()
            if not out:
                out = "(no output)"
            return out[:MAX_OUTPUT_SIZE] if out else "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {timeout} seconds"
        except Exception as e:
            return f"Error: {str(e)}"

    def remove(self, name: str, force: bool = False, complete_task: bool = False) -> str:
        wt = self._find(name)
        if not wt:
            path_candidate = self.dir / name
            if path_candidate.exists():
                try:
                    self._run_git(["worktree", "remove", "--force", str(path_candidate)])
                    idx = self._load_index()
                    idx["worktrees"] = [w for w in idx.get("worktrees", []) if w.get("name") != name]
                    self._save_index(idx)
                    return f"Removed orphaned worktree '{name}' (not in index)"
                except Exception as e:
                    return f"Error removing orphaned worktree '{name}': {e}"
            else:
                return f"Error: Unknown worktree '{name}'"

        self.events.emit(
            "worktree.remove.before",
            task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
            worktree={"name": name, "path": wt.get("path")},
        )
        try:
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(wt["path"])
            self._run_git(args)

            if complete_task and wt.get("task_id") is not None:
                task_id = wt["task_id"]
                before = json.loads(self.tasks.get(task_id))
                self.tasks.update(task_id, status="completed")
                self.tasks.unbind_worktree(task_id)
                self.events.emit(
                    "task.completed",
                    task={
                        "id": task_id,
                        "subject": before.get("subject", ""),
                        "status": "completed",
                    },
                    worktree={"name": name},
                )

            idx = self._load_index()
            for item in idx.get("worktrees", []):
                if item.get("name") == name:
                    item["status"] = "removed"
                    item["removed_at"] = time.time()
            self._save_index(idx)

            self.events.emit(
                "worktree.remove.after",
                task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
                worktree={"name": name, "path": wt.get("path"), "status": "removed"},
            )
            return f"Removed worktree '{name}'"
        except Exception as e:
            self.events.emit(
                "worktree.remove.failed",
                task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
                worktree={"name": name, "path": wt.get("path")},
                error=str(e),
            )
            raise

    def keep(self, name: str) -> str:
        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"

        idx = self._load_index()
        kept = None
        for item in idx.get("worktrees", []):
            if item.get("name") == name:
                item["status"] = "kept"
                item["kept_at"] = time.time()
                kept = item
        self._save_index(idx)

        self.events.emit(
            "worktree.keep",
            task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
            worktree={
                "name": name,
                "path": wt.get("path"),
                "status": "kept",
            },
        )
        return json.dumps(kept, indent=2) if kept else f"Error: Unknown worktree '{name}'"

WORKTREES = WorktreeManager(REPO_ROOT, TASKS, EVENTS)

# ========== BackgroundManager ==========
class BackgroundManager:
    def __init__(self):
        self.tasks: Dict[str, Dict] = {}
        self._notification_queue: List[Dict] = []
        self._lock = threading.Lock()

    def run(self, command: str, timeout: int = 300) -> str:
        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = {"status": "running", "result": None, "command": command[:200]}
        thread = threading.Thread(target=self._execute, args=(task_id, command, timeout), daemon=True)
        thread.start()
        return f"Background task {task_id} started: {command[:80]}"

    def _execute(self, task_id: str, command: str, timeout: int):
        try:
            if IS_WINDOWS:
                proc = subprocess.run(["cmd.exe", "/c", command], cwd=WORKDIR, capture_output=True, text=True, timeout=timeout)
            else:
                proc = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=timeout)
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
        if len(output) > 50000:
            output = output[:50000] + "\n...[输出已截断]"
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output
        with self._lock:
            self._notification_queue.append({
                "task_id": task_id, "status": status, "command": command[:80], "result": output[:500]
            })

    def check(self, task_id: Optional[str] = None) -> str:
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return f"错误: 未知后台任务 {task_id}"
            result_preview = t.get("result", "")[:200] if t["result"] else "(运行中)"
            return f"[{t['status']}] {t['command']}\n{result_preview}"
        if not self.tasks:
            return "无后台任务"
        lines = [f"{tid}: [{t['status']}] {t['command'][:60]}" for tid, t in self.tasks.items()]
        return "\n".join(lines)

    def drain_notifications(self) -> List[Dict]:
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs

BG = BackgroundManager()

# ========== 上下文压缩 ==========
def estimate_tokens(messages: list) -> int:
    return len(json.dumps(messages, default=str)) // 4

def micro_compact(messages: list) -> list:
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            for part_idx, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))
    if len(tool_results) <= KEEP_RECENT:
        return messages
    tool_name_map = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
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
    dict_messages = [msg for msg in messages if isinstance(msg, dict)]
    with open(transcript_path, "w", encoding="utf-8") as f:
        for msg in dict_messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    print(f"\033[90m[transcript saved: {transcript_path}]\033[0m")
    conversation_text = json.dumps(dict_messages, default=str, ensure_ascii=False)[-80000:]
    summary_prompt = ( "Summarize this conversation for continuity. Include:\n"
        "1) What was accomplished,\n"
        "2) Current state,\n"
        "3) Key decisions made.\n"
        "Be concise but preserve critical details.\n\n" + conversation_text )
    resp = llm_client._chat_no_stream([{"role": "user", "content": summary_prompt}], [])
    summary = resp.get("content", "")
    if not summary:
        summary = "No summary generated."
    compressed_content = f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"
    return [{"role": "user", "content": compressed_content}]

# ========== 技能加载器 ==========
class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills = {}
        self._load_all()

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
            return f"错误: 未知技能 '{name}'。可用技能: {available if available else '无'}"
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"

SKILL_LOADER = SkillLoader(SKILLS_DIR)

# ========== MessageBus ==========
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str, msg_type: str = "message", extra: dict = None) -> str:
        valid_types = {"message", "broadcast", "shutdown_request", "shutdown_response", "plan_approval_response"}
        if msg_type not in valid_types:
            return f"错误: 无效类型 '{msg_type}'。有效类型: {valid_types}"
        msg = {"type": msg_type, "from": sender, "content": content, "timestamp": time.time()}
        if extra:
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return f"已发送 {msg_type} 给 {to}"

    def read_inbox(self, name: str) -> list:
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        with open(inbox_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    messages.append(json.loads(line))
        # 注意：不清空文件，以便激活时能读取历史
        return messages

    def clear_inbox(self, name: str):
        """清空收件箱文件（用于读取后清理）"""
        inbox_path = self.dir / f"{name}.jsonl"
        if inbox_path.exists():
            inbox_path.write_text("", encoding="utf-8")

BUS = MessageBus(INBOX_DIR)

# ========== TeammateManager (队友线程，使用任务+工作树机制) ==========
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        (self.dir / "inbox").mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self._reset_all_to_shutdown()
        self.threads: Dict[str, threading.Thread] = {}

    def _reset_all_to_shutdown(self):
        changed = False
        for member in self.config.get("members", []):
            if member.get("status") != "shutdown":
                member["status"] = "shutdown"
                changed = True
        if changed:
            self._save_config()

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8")

    def _find_member(self, name: str) -> Optional[dict]:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        member = self._find_member(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"错误: '{name}' 当前状态为 {member['status']}"
            member["status"] = "working"
            member["role"] = role
            member["prompt"] = prompt
        else:
            member = {"name": name, "role": role, "status": "working", "prompt": prompt}
            self.config["members"].append(member)
        self._save_config()
        if name in self.threads and self.threads[name].is_alive():
            return f"错误: 队友 '{name}' 的旧线程仍在运行"
        thread = threading.Thread(target=self._autonomous_loop, args=(name, role, prompt, []), daemon=True)
        self.threads[name] = thread
        thread.start()
        return f"已生成自主队友 '{name}' (角色: {role})"

    def activate(self, name: str) -> str:
        member = self._find_member(name)
        if not member:
            return f"错误: 未知队友 '{name}'，请先使用 spawn_teammate 创建。"
        if member["status"] != "shutdown":
            return f"错误: 队友 '{name}' 状态为 {member['status']}，无法激活（只有 shutdown 状态可激活）。"
        if name in self.threads and self.threads[name].is_alive():
            return f"错误: 队友 '{name}' 的线程仍在运行，请等待其自然停止或手动清理。"
        history = BUS.read_inbox(name)
        role = member.get("role", "worker")
        prompt = member.get("prompt", "请继续你的工作。")
        member["status"] = "working"
        self._save_config()
        thread = threading.Thread(target=self._autonomous_loop, args=(name, role, prompt, history), daemon=True)
        self.threads[name] = thread
        thread.start()
        return f"已激活队友 '{name}' (角色: {role})，已加载 {len(history)} 条历史收件消息。"

    def _autonomous_loop(self, name: str, role: str, prompt: str, initial_inbox_history: List[Dict]):
        team_name = self.config["team_name"]
        sys_prompt = (
            f"你是队员 '{name}',角色: {role}，团队: '{team_name}', 你的 inbox 地址 '{name}.jsonl', 团队 lead 的 inbox 地址: 'lead.jsonl', 工作目录: {WORKDIR}，操作系统: {OS_INFO}。！！需使用适配该操作系统的bash命令！！ \n"
            f"你可以使用以下工具：\n"
            f"  - 基础文件: read_file, write_file, edit_file, bash\n"
            f"  - 任务管理: task_create, task_list, task_get, task_update, task_bind_worktree\n"
            f"  - 工作树隔离: worktree_create, worktree_list, worktree_status, worktree_run, worktree_keep, worktree_remove, worktree_events\n"
            f"  - 通信工具: send_message, read_inbox, shutdown_response, plan_approval, idle\n"
            f"工作流程建议：\n"
            f"1. 从任务板认领任务：使用 task_list 查看 pending 任务，然后 task_update 设置 owner 为自己，状态为 in_progress。\n"
            f"2. 使用 send_message 工具给团队 lead 发送'任务已被领取，正在执行'。\n"
            f"3. 为任务创建工作树：worktree_create name=<任务名> task_id=<任务ID> base_ref=HEAD\n"
            f"4. 在工作树中执行修改：worktree_run name=<任务名> command=\"...\"\n"
            f"5. 任务完成后，使用 worktree_remove name=<任务名> complete_task=true 自动清理并标记完成。\n"
            f"6. 使用 send_message 工具给团队 lead 发送 '任务已完成' 的信息。 \n"            
            f"7. 使用 read_inbox 工具查看 lead 或 其它队员发送的信息，并及时回复，如果问已完成任务信息，只需回复任务执行情况，无需重复执行任务。\n"
            f"8. 当没有更多工作时，调用 idle 工具进入空闲状态。空闲时会自动轮询收件箱和任务板。\n"
            f"当收到 shutdown_request 时，请调用 shutdown_response 工具响应（approve 表示同意关机）。\n"
            f"对于重大更改，请先使用 plan_approval 工具提交计划给 lead，等待批准。\n"
            f"每个工作阶段最多调用 {MAX_TOOL_CALLS_PER_PHASE} 次工具，超过将强制进入 idle 。"
            f"最后，检查是否已经使用 send_message 工具给团队 lead 发送任务已完成的信息！"
        )
        teammate_tools_def = [
            {"type": "function", "function": {"name": "bash", "description": "执行Shell命令",
             "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
            {"type": "function", "function": {"name": "read_file", "description": "读取文件",
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}}},
            {"type": "function", "function": {"name": "write_file", "description": "写入文件",
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
            {"type": "function", "function": {"name": "edit_file", "description": "编辑文件",
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
            {"type": "function", "function": {"name": "send_message", "description": "发送消息给队友或lead",
             "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": ["message", "broadcast", "shutdown_request", "shutdown_response", "plan_approval_response"]}}, "required": ["to", "content"]}}},
            {"type": "function", "function": {"name": "read_inbox", "description": "读取并清空自己的收件箱",
             "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "shutdown_response", "description": "响应 shutdown 请求",
             "parameters": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]}}},
            {"type": "function", "function": {"name": "plan_approval", "description": "向 lead 提交需要批准的计划",
             "parameters": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}}},
            {"type": "function", "function": {"name": "idle", "description": "通知系统进入空闲状态",
             "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "task_create", "description": "创建新任务",
             "parameters": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}}},
            {"type": "function", "function": {"name": "task_list", "description": "列出所有任务",
             "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "task_get", "description": "获取任务详情",
             "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}},
            {"type": "function", "function": {"name": "task_update", "description": "更新任务状态或所有者",
             "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "owner": {"type": "string"}}, "required": ["task_id"]}}},
            {"type": "function", "function": {"name": "task_bind_worktree", "description": "绑定任务到工作树",
             "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}, "worktree": {"type": "string"}, "owner": {"type": "string"}}, "required": ["task_id", "worktree"]}}},
            {"type": "function", "function": {"name": "worktree_create", "description": "创建 git worktree 并可选绑定任务",
             "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "task_id": {"type": "integer"}, "base_ref": {"type": "string"}}, "required": ["name"]}}},
            {"type": "function", "function": {"name": "worktree_list", "description": "列出所有工作树",
             "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "worktree_status", "description": "显示工作树 git 状态",
             "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
            {"type": "function", "function": {"name": "worktree_run", "description": "在工作树中运行命令",
             "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "command": {"type": "string"}}, "required": ["name", "command"]}}},
            {"type": "function", "function": {"name": "worktree_keep", "description": "将工作树标记为保留",
             "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
            {"type": "function", "function": {"name": "worktree_remove", "description": "删除工作树并可选完成任务",
             "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "force": {"type": "boolean"}, "complete_task": {"type": "boolean"}}, "required": ["name"]}}},
            {"type": "function", "function": {"name": "worktree_events", "description": "查看生命周期事件", "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}}}},
        ]

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt}
        ]
        if initial_inbox_history:
            for msg in initial_inbox_history:
                inbox_block = f"<inbox>\n{json.dumps(msg, indent=2, ensure_ascii=False)}\n</inbox>"
                messages.append({"role": "user", "content": inbox_block})
            print(f"\033[36m[队友 {name}] 已加载 {len(initial_inbox_history)} 条历史收件消息\033[0m")

        client = MultiModelClient()
        global_step = 0
        total_tool_calls = 0
        pending_plan_request_id = None
        error_count = 0
        last_tool_signature = ""
        similar_count = 0

        while True:
            global_step += 1            
            phase_tool_calls = 0
            idle_requested = False

            for _ in range(50):
                inbox = BUS.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "plan_approval_response":
                            req_id = msg.get("request_id")
                            approve = msg.get("approve")
                            feedback = msg.get("feedback", "")
                            if approve is True:
                                pending_plan_request_id = None
                                messages.append({
                                    "role": "user",
                                    "content": f"<system-approval>\n你的计划已被 lead 批准！反馈: {feedback}\n请立即执行计划中的具体操作。\n</system-approval>"
                                })
                            else:
                                pending_plan_request_id = None
                                messages.append({
                                    "role": "user",
                                    "content": f"<system-rejection>\n你的计划被 lead 拒绝。反馈: {feedback}\n请修改计划或停止。\n</system-rejection>"
                                })
                        else:
                            messages.append({"role": "user", "content": f"<inbox>\n{json.dumps(msg, indent=2, ensure_ascii=False)}\n</inbox>"})
                    BUS.clear_inbox(name)

                resp = client._chat_no_stream(messages, teammate_tools_def)
                if resp.get("error"):
                    print(f"\033[36m[队友 {name}] 模型错误: {resp['error']}\033[0m")
                    error_count += 1
                    if error_count >= 3:
                        print(f"\033[36m[队友 {name}] 连续错误过多，强制退出\033[0m")
                        self._set_status(name, "shutdown")
                        return
                    time.sleep(5)
                    continue
                error_count = 0

                assistant_msg = {"role": "assistant", "content": resp["content"]}
                if resp["tool_calls"]:
                    assistant_msg["tool_calls"] = [
                        {"id": tc["id"], "type": tc["type"], "function": tc["function"]}
                        for tc in resp["tool_calls"]
                    ]
                messages.append(assistant_msg)

                if not resp["tool_calls"]:
                    if resp["content"]:
                        print(f"\033[36m[队友 {name}] 说: {resp['content'][:200]}\033[0m")
                        if TEAMMATE_CALLBACK:
                            TEAMMATE_CALLBACK(name, "assistant", {"content": resp["content"]})
                    idle_requested = True
                    break

                for tc in resp["tool_calls"]:
                    tool_name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {}
                    sig = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
                    if sig == last_tool_signature:
                        similar_count += 1
                        if similar_count >= STUCK_SIMILAR_THRESHOLD:
                            print(f"\033[36m[队友 {name}] 重复操作，强制进入空闲\033[0m")
                            idle_requested = True
                            break
                    else:
                        similar_count = 0
                        last_tool_signature = sig

                    if tool_name == "plan_approval":
                        if pending_plan_request_id is not None:
                            output = f"错误: 已有等待批准的计划，请稍后"
                            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": output})
                            continue
                        output = self._execute_teammate_tool(name, tool_name, args)
                        match = re.search(r"request_id=([a-f0-9]+)", output)
                        if match:
                            pending_plan_request_id = match.group(1)
                    else:
                        output = self._execute_teammate_tool(name, tool_name, args)

                    print(f"\033[36m[队友 {name}] 🔧 {tool_name}\033[0m {str(output)[:100]}")
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(output)})
                    
                    if TEAMMATE_CALLBACK:
                        TEAMMATE_CALLBACK(name, "tool", {
                            "tool_name": tool_name,
                            "arguments": args,
                            "output": output
                        })

                    phase_tool_calls += 1
                    total_tool_calls += 1

                    if tool_name == "idle":
                        idle_requested = True
                    elif tool_name == "shutdown_response" and args.get("approve"):
                        self._set_status(name, "shutdown")
                        return

                    if phase_tool_calls >= MAX_TOOL_CALLS_PER_PHASE:
                        print(f"\033[36m[队友 {name}] 单阶段工具调用上限，强制进入空闲\033[0m")
                        idle_requested = True
                        break
                    if total_tool_calls >= MAX_TOTAL_STEPS:
                        print(f"\033[36m[队友 {name}] 总调用次数上限，自动关机\033[0m")
                        self._set_status(name, "shutdown")
                        return

                if idle_requested:
                    break

            self._set_status(name, "idle")
            resume = self._idle_poll(name, messages, role, team_name)
            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")

    def _idle_poll(self, name: str, messages: list, role: str, team_name: str) -> bool:
        polls = IDLE_TIMEOUT // POLL_INTERVAL
        for _ in range(polls):
            time.sleep(POLL_INTERVAL)
            inbox = BUS.read_inbox(name)
            if inbox:
                for msg in inbox:
                    messages.append({"role": "user", "content": f"<inbox>\n{json.dumps(msg, indent=2, ensure_ascii=False)}\n</inbox>"})
                BUS.clear_inbox(name)
                return True
            unclaimed = TASKS.find_pending_unclaimed()
            if unclaimed:
                task = unclaimed[0]
                try:
                    TASKS.update(task["id"], status="in_progress", owner=name)
                except Exception as e:
                    print(f"\033[36m[队友 {name}] 自动认领任务 #{task['id']} 失败: {e}\033[0m")
                    continue
                task_prompt = (
                    f"<auto-claimed>任务 #{task['id']}: {task['subject']}\n"
                    f"{task.get('description', '')}\n"
                    f"建议: 使用 worktree_create name=task-{task['id']} task_id={task['id']} 创建工作树，然后在该工作树中完成任务。</auto-claimed>"
                )                    
                messages.append({"role": "user", "content": task_prompt})
                messages.append({"role": "assistant", "content": f"已认领任务 #{task['id']}，现在开始工作。"})
                BUS.send(name, "lead", f"我已认领任务 #{task['id']}: {task['subject']}", "message")
                if TEAMMATE_CALLBACK:
                    TEAMMATE_CALLBACK(name, "info", {
                        "content": f"已自动认领任务 #{task['id']}: {task['subject']}"
                    })
                return True
        print(f"\033[36m[队友 {name}] 空闲超时，自动关机\033[0m")
        return False

    def _execute_teammate_tool(self, sender: str, tool_name: str, args: dict) -> str:
        if tool_name == "bash":
            out, _ = run_bash(args["command"])
            return out
        if tool_name == "read_file":
            out, _ = run_read(args["path"], args.get("limit"))
            return out
        if tool_name == "write_file":
            out, _ = run_write(args["path"], args["content"])
            return out
        if tool_name == "edit_file":
            out, _ = run_edit(args["path"], args["old_text"], args["new_text"])
            return out
        if tool_name == "send_message":
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            msgs = BUS.read_inbox(sender)
            BUS.clear_inbox(sender)
            return json.dumps(msgs, indent=2, ensure_ascii=False)
        if tool_name == "shutdown_response":
            req_id = args["request_id"]
            approve = args["approve"]
            with _tracker_lock:
                if req_id in _shutdown_requests:
                    _shutdown_requests[req_id]["status"] = "approved" if approve else "rejected"
            BUS.send(sender, "lead", args.get("reason", ""),
                     "shutdown_response", {"request_id": req_id, "approve": approve})
            return f"Shutdown {'approved' if approve else 'rejected'}"
        if tool_name == "plan_approval":
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]
            with _tracker_lock:
                _plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
            BUS.send(sender, "lead", plan_text, "plan_approval_response",
                     {"request_id": req_id, "plan": plan_text})
            return f"Plan submitted (request_id={req_id})"
        if tool_name == "idle":
            return "进入空闲轮询模式"
        # 任务/工作树工具
        if tool_name == "task_create":
            try:
                return TASKS.create(args["subject"], args.get("description", ""))
            except Exception as e:
                return f"错误: 创建任务失败 - {str(e)}"
        if tool_name == "task_list":
            try:
                return TASKS.list_all()
            except Exception as e:
                return f"错误: 列出任务失败 - {str(e)}"
        if tool_name == "task_get":
            try:
                return TASKS.get(args["task_id"])
            except Exception as e:
                return f"错误: 获取任务失败 - {str(e)}"
        if tool_name == "task_update":
            try:
                return TASKS.update(args["task_id"], args.get("status"), args.get("owner"))
            except Exception as e:
                return f"错误: 更新任务失败 - {str(e)}"
        if tool_name == "task_bind_worktree":
            try:
                return TASKS.bind_worktree(args["task_id"], args["worktree"], args.get("owner", ""))
            except Exception as e:
                return f"错误: 绑定工作树失败 - {str(e)}"
        if tool_name == "worktree_create":
            try:
                return WORKTREES.create(args["name"], args.get("task_id"), args.get("base_ref", "HEAD"))
            except Exception as e:
                return f"错误: 创建工作树失败 - {str(e)}"
        if tool_name == "worktree_list":
            try:
                return WORKTREES.list_all()
            except Exception as e:
                return f"错误: 列出工作树失败 - {str(e)}"
        if tool_name == "worktree_status":
            try:
                return WORKTREES.status(args["name"])
            except Exception as e:
                return f"错误: 获取工作树状态失败 - {str(e)}"
        if tool_name == "worktree_run":
            try:
                return WORKTREES.run(args["name"], args["command"])
            except Exception as e:
                return f"错误: 在工作树中运行命令失败 - {str(e)}"
        if tool_name == "worktree_keep":
            try:
                return WORKTREES.keep(args["name"])
            except Exception as e:
                return f"错误: 保留工作树失败 - {str(e)}"
        if tool_name == "worktree_remove":
            try:
                return WORKTREES.remove(args["name"], args.get("force", False), args.get("complete_task", False))
            except Exception as e:
                return f"错误: 删除工作树失败 - {str(e)}"
        if tool_name == "worktree_events":
            try:
                return EVENTS.list_recent(args.get("limit", 20))
            except Exception as e:
                return f"错误: 获取事件失败 - {str(e)}"
        return f"未知工具: {tool_name}"

    def _set_status(self, name: str, status: str):
        member = self._find_member(name)
        if member:
            member["status"] = status
            self._save_config()

    def list_all(self) -> str:
        if not self.config["members"]:
            return "暂无队友"
        lines = [f"团队: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> List[str]:
        return [m["name"] for m in self.config["members"]]

    def has_working_members(self) -> bool:
        for m in self.config["members"]:
            if m["status"] == "working":
                return True
        return False

# 创建全局队友管理器实例
TEAM = TeammateManager(TEAM_DIR)

@dataclass
class WorkflowState:
    session_id: str                # 唯一标识（可关联 task_id 或 conversation_id）
    task_id: Optional[int]         # 可选绑定任务板 ID
    phase: str                     # 当前阶段: ARCH, REQ, DESIGN, CONFIRM, EXEC, VERIFY, REFINE, DONE
    artifacts: Dict[str, str]      # 各阶段产出: {"ARCH": "...", "REQ": "...", ...}
    pending_plan: str              # 待固化的计划（DESIGN 产出）
    error_count: int
    created_at: float
    updated_at: float

class WorkflowManager:
    """管理多个会话的工作流状态，持久化到 .workflows/ 目录"""
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir / ".workflows"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._states: Dict[str, WorkflowState] = {}
        self._load_all()

    def _load_all(self):
        for f in self.base_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                state = WorkflowState(**data)
                self._states[state.session_id] = state
            except Exception as e:
                print(f"加载工作流状态失败 {f}: {e}")

    def _save(self, state: WorkflowState):
        path = self.base_dir / f"{state.session_id}.json"
        path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")

    def get_or_create(self, session_id: str, task_id: Optional[int] = None) -> WorkflowState:
        if session_id in self._states:
            return self._states[session_id]
        state = WorkflowState(
            session_id=session_id,
            task_id=task_id,
            phase="ARCH",
            artifacts={},
            pending_plan="",
            error_count=0,
            created_at=time.time(),
            updated_at=time.time(),
        )
        self._states[session_id] = state
        self._save(state)
        return state

    def get(self, session_id: str) -> Optional[WorkflowState]:
        return self._states.get(session_id)

    def update(self, state: WorkflowState):
        state.updated_at = time.time()
        self._states[state.session_id] = state
        self._save(state)

    def delete(self, session_id: str):
        if session_id in self._states:
            del self._states[session_id]
            (self.base_dir / f"{session_id}.json").unlink(missing_ok=True)

    def transition(self, session_id: str, event: str, artifact: str = "") -> Tuple[str, str]:
        """
        执行状态转移，返回 (new_phase, instruction)
        event: "confirm" (用户确认固化), "execute_done", "verify_pass", "verify_fail", "refine_done", "abort"
        artifact: 当前阶段生成的产出文本
        """
        state = self.get_or_create(session_id)
        old_phase = state.phase

        # 保存当前阶段的产出
        if artifact and old_phase in ("ARCH", "REQ", "DESIGN", "EXEC", "REFINE"):
            state.artifacts[old_phase] = artifact

        # 状态转移逻辑
        if old_phase == "ARCH" and event == "confirm":
            state.phase = "REQ"
            instr = "请输出需求分析（验收标准、功能列表、模糊点），然后询问用户确认。"
        elif old_phase == "REQ" and event == "confirm":
            state.phase = "DESIGN"
            instr = "请输出详细设计（可执行的步骤、接口、数据结构），完成后询问用户固化（回复'固化'）。"
        elif old_phase == "DESIGN" and event == "confirm":
            state.phase = "CONFIRM"
            state.pending_plan = artifact
            instr = "请等待用户输入'固化'以锁定计划，否则返回修改设计。"
        elif old_phase == "CONFIRM" and event == "confirm":
            state.phase = "EXEC"
            instr = "计划已固化。请严格按照设计逐步执行，每完成一步调用 workflow_step 并传入 'execute_done' 事件。"
        elif old_phase == "EXEC" and event == "execute_done":
            state.phase = "VERIFY"
            instr = "请验证执行结果是否符合需求验收标准，输出验证报告，然后调用 workflow_step 选择 'verify_pass' 或 'verify_fail'。"
        elif old_phase == "VERIFY" and event == "verify_pass":
            state.phase = "DONE"
            instr = "验证通过，工作流完成。你可以输出最终答案。"
        elif old_phase == "VERIFY" and event == "verify_fail":
            state.phase = "REFINE"
            instr = "验证失败，请修正问题，然后调用 workflow_step 传入 'refine_done'。"
        elif old_phase == "REFINE" and event == "refine_done":
            state.phase = "VERIFY"
            instr = "修正完成，请重新验证。"
        else:
            raise ValueError(f"非法转移: {old_phase} + {event}")

        self.update(state)
        return state.phase, instr

WORKFLOWS = WorkflowManager(REPO_ROOT)

# ========== Tool Registry (可插拔工具核心) ==========
class ToolRegistry:
    """管理所有工具：注册、启用/禁用、获取工具定义和处理器"""
    def __init__(self):
        self._tools: Dict[str, Dict] = {}   # name -> {description, parameters, handler, enabled, builtin, editable}
        self._lock = threading.RLock()
        self._load_custom_tools()

    def register(self, name: str, description: str, parameters: dict, handler: Callable,
             enabled: bool = True, builtin: bool = True, editable: bool = False):
        # 注意：此方法应由已持有 self._lock 的调用者调用
        self._tools[name] = {
            "description": description,
            "parameters": parameters,
            "handler": handler,
            "enabled": enabled,
            "builtin": builtin,
            "editable": editable,
        }

    def enable(self, name: str) -> bool:
        with self._lock:
            if name in self._tools:
                self._tools[name]["enabled"] = True
                return True
            return False

    def disable(self, name: str) -> bool:
        with self._lock:
            if name in self._tools:
                self._tools[name]["enabled"] = False
                return True
            return False

    def is_enabled(self, name: str) -> bool:
        with self._lock:
            return self._tools.get(name, {}).get("enabled", False)

    def get_tools_def(self) -> List[Dict]:
        """返回 OpenAI 格式的工具列表，仅包含启用的工具"""
        with self._lock:
            tools_def = []
            for name, info in self._tools.items():
                if info["enabled"]:
                    tools_def.append({
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": info["description"],
                            "parameters": info["parameters"],
                        }
                    })
            return tools_def

    def get_handler(self, name: str) -> Optional[Callable]:
        with self._lock:
            info = self._tools.get(name)
            if info and info["enabled"]:
                return info["handler"]
            return None

    def list_tools(self) -> List[Dict]:
        """返回所有工具的元信息（包括启用状态）"""
        with self._lock:
            return [
                {
                    "name": name,
                    "description": info["description"],
                    "enabled": info["enabled"],
                    "builtin": info["builtin"],
                    "editable": info["editable"],
                }
                for name, info in self._tools.items()
            ]

    def get_custom_tools(self) -> List[Dict]:
        """返回可编辑的自定义工具"""
        with self._lock:
            return [
                {"name": name, "enabled": info["enabled"], "code": self._get_custom_code(name)}
                for name, info in self._tools.items() if not info["builtin"] and info["editable"]
            ]

    def add_custom_tool(self, name: str, description: str, parameters: dict, code: str,
                    enabled: bool = True) -> Tuple[bool, str]:
        valid, err_msg = self._validate_custom_code(code)
        if not valid:
            return False, err_msg
        try:
            handler = self._make_handler_from_code(name, code)
        except Exception as e:
            return False, f"创建 handler 失败: {e}"
        
        # 准备保存的数据（在锁外）
        tool_data = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "code": code,
            "enabled": enabled,
        }
        
        # 先写入文件，确保能持久化再注册到内存（避免不一致）
        try:
            self._save_custom_tool(name, description, parameters, code, enabled)
        except Exception as e:
            return False, f"保存文件失败: {e}"
        
        # 注册到内存，只持有锁一小会儿
        with self._lock:
            self.register(name, description, parameters, handler,
                        enabled=enabled, builtin=False, editable=True)
            self._tools[name]["code"] = code
        return True, ""

    def update_custom_tool(self, name: str, description: str = None, parameters: dict = None,
                           code: str = None, enabled: bool = None) -> bool:
        with self._lock:
            if name not in self._tools or self._tools[name]["builtin"]:
                return False
        if code is not None:
            if not self._validate_custom_code(code):
                return False
            handler = self._make_handler_from_code(name, code)
            with self._lock:
                self._tools[name]["handler"] = handler
                self._tools[name]["code"] = code
        if description is not None:
            with self._lock:
                self._tools[name]["description"] = description
        if parameters is not None:
            with self._lock:
                self._tools[name]["parameters"] = parameters
        if enabled is not None:
            with self._lock:
                self._tools[name]["enabled"] = enabled
        # 重新持久化（简单删除再添加）
        with self._lock:
            info = self._tools[name]
            self._save_custom_tool(name, info["description"], info["parameters"],
                                   info.get("code", ""), info["enabled"])
        return True

    def delete_custom_tool(self, name: str) -> bool:
        with self._lock:
            if name not in self._tools or self._tools[name]["builtin"]:
                return False
            del self._tools[name]
        self._remove_custom_tool_file(name)
        return True

    # ---------- 内部辅助方法 ----------
    def _load_custom_tools(self):
        """从 CUSTOM_TOOLS_META_PATH 加载自定义工具"""
        if not CUSTOM_TOOLS_META_PATH.exists():
            return
        try:
            with open(CUSTOM_TOOLS_META_PATH, "r", encoding="utf-8") as f:
                tools_data = json.load(f)
            for tool in tools_data:
                name = tool["name"]
                description = tool["description"]
                parameters = tool["parameters"]
                code = tool["code"]
                enabled = tool.get("enabled", True)
                valid, _ = self._validate_custom_code(code)
                if valid:
                    handler = self._make_handler_from_code(name, code)
                    self.register(name, description, parameters, handler,
                                  enabled=enabled, builtin=False, editable=True)
                    # 缓存代码
                    with self._lock:
                        self._tools[name]["code"] = code
                else:
                    print(f"警告: 自定义工具 {name} 代码验证失败，跳过加载")
        except Exception as e:
            print(f"加载自定义工具失败: {e}")

    def _save_custom_tool(self, name: str, description: str, parameters: dict, code: str, enabled: bool):
        # 确保目录存在
        CUSTOM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        meta_path = CUSTOM_TOOLS_DIR / "custom_tools.json"
        # 读取现有数据
        tools_data = []
        if meta_path.exists():
            with open(meta_path, "r", encoding="utf-8") as f:
                tools_data = json.load(f)
        # 移除旧的同名条目
        tools_data = [t for t in tools_data if t.get("name") != name]
        tools_data.append({
            "name": name,
            "description": description,
            "parameters": parameters,
            "code": code,
            "enabled": enabled,
        })
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(tools_data, f, indent=2, ensure_ascii=False)

    def _remove_custom_tool_file(self, name: str):
        if not CUSTOM_TOOLS_META_PATH.exists():
            return
        with open(CUSTOM_TOOLS_META_PATH, "r", encoding="utf-8") as f:
            tools_data = json.load(f)
        tools_data = [t for t in tools_data if t.get("name") != name]
        with open(CUSTOM_TOOLS_META_PATH, "w", encoding="utf-8") as f:
            json.dump(tools_data, f, indent=2, ensure_ascii=False)
        
    def _validate_custom_code(self, code: str) -> Tuple[bool, str]:        
        ok, msg = validate_tool_code_safety(code)
        if not ok:
            return False, msg
        # 额外检查必须包含 execute 函数（已在其他地方检查，但这里也做）
        if "def execute" not in code:
            return False, "代码必须定义 execute(args: dict) -> str 函数"
        return True, ""

    def _make_handler_from_code(self, name: str, code: str) -> Callable:
        """从代码字符串创建可调用的 handler 函数，要求代码中定义了一个名为 execute 的函数，接收一个字典参数，返回字符串"""
        namespace = {}
        exec(code, namespace)
        if "execute" not in namespace:
            raise ValueError("自定义工具代码必须定义 execute(args: dict) -> str 函数")
        executor = namespace["execute"]
        # 包装为标准 handler 签名: (args: dict) -> Tuple[str, Optional[str]]
        def handler(**kwargs) -> Tuple[str, Optional[str]]:
            try:
                result = executor(kwargs)
                if not isinstance(result, str):
                    result = str(result)
                return result, None
            except Exception as e:
                return "", f"自定义工具执行失败: {str(e)}\n{traceback.format_exc()}"
        return handler

    def _get_custom_code(self, name: str) -> str:
        with self._lock:
            return self._tools.get(name, {}).get("code", "")


# ========== 内置工具定义和处理器 ==========
def _make_base_tools():
    """注册所有内置工具到注册表"""
    # 基础文件工具
    registry.register("bash", "执行Shell命令",
        {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        lambda **kw: run_bash(kw["command"]), builtin=True, editable=False)
    registry.register("read_file", "读取文件",
        {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]},
        lambda **kw: run_read(kw["path"], kw.get("limit")), builtin=True, editable=False)
    registry.register("write_file", "写入文件",
        {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
        lambda **kw: run_write(kw["path"], kw["content"]), builtin=True, editable=False)
    registry.register("edit_file", "编辑文件",
        {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]},
        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]), builtin=True, editable=False)
    # 待办工具
    registry.register("todo", "创建或更新任务列表",
        {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object",
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
            "required": ["id", "text", "status"]}}}, "required": ["items"]},
        lambda **kw: run_todo(kw["items"]), builtin=True, editable=False)
    # 子代理工具
    registry.register("task", "启动一次性子代理",
        {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
        lambda **kw: run_subagent(kw["prompt"]), builtin=True, editable=False)
    # 技能加载
    registry.register("load_skill", "加载技能",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        lambda **kw: run_load_skill(kw["name"]), builtin=True, editable=False)
    # 上下文压缩
    registry.register("compact", "手动压缩上下文",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda **kw: run_compact(), builtin=True, editable=False)
    # 后台命令
    registry.register("background_run", "后台运行命令（返回 task_id）",
        {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        lambda **kw: run_background_run(kw["command"]), builtin=True, editable=False)
    registry.register("check_background", "检查后台任务状态（仅用于 background_run 返回的 task_id）",
        {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": []},
        lambda **kw: run_check_background(kw.get("task_id")), builtin=True, editable=False)
    # 队友管理工具
    registry.register("spawn_teammate", "生成持久化自主队友",
        {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]},
        lambda **kw: run_spawn_teammate(kw["name"], kw["role"], kw["prompt"]), builtin=True, editable=False)
    registry.register("activate_teammate", "激活已 shutdown 的队友",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        lambda **kw: run_activate_teammate(kw["name"]), builtin=True, editable=False)
    registry.register("list_teammates", "列出所有队友",
        {"type": "object", "properties": {}},
        lambda **kw: run_list_teammates(), builtin=True, editable=False)
    registry.register("send_message", "给队友发送消息",
        {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": ["message", "broadcast", "shutdown_request", "shutdown_response", "plan_approval_response"]}}, "required": ["to", "content"]},
        lambda **kw: run_send_message(kw["to"], kw["content"], kw.get("msg_type", "message")), builtin=True, editable=False)
    registry.register("read_inbox", "读取收件箱",
        {"type": "object", "properties": {}},
        lambda **kw: run_read_inbox(), builtin=True, editable=False)
    registry.register("broadcast", "广播消息",
        {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
        lambda **kw: run_broadcast(kw["content"]), builtin=True, editable=False)
    # 协议工具
    registry.register("shutdown_request", "请求队友关机",
        {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]},
        lambda **kw: handle_shutdown_request(kw["teammate"]), builtin=True, editable=False)
    registry.register("shutdown_response", "查询关机请求状态",
        {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]},
        lambda **kw: handle_shutdown_status(kw["request_id"]), builtin=True, editable=False)
    registry.register("plan_approval", "批准或拒绝计划",
        {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]},
        lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")), builtin=True, editable=False)
    # 任务/工作树工具
    registry.register("task_create", "创建新任务",
        {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]},
        lambda **kw: run_task_create(kw["subject"], kw.get("description", "")), builtin=True, editable=False)
    registry.register("task_list", "列出所有任务",
        {"type": "object", "properties": {}},
        lambda **kw: run_task_list(), builtin=True, editable=False)
    registry.register("task_get", "获取任务详情",
        {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]},
        lambda **kw: run_task_get(kw["task_id"]), builtin=True, editable=False)
    registry.register("task_update", "更新任务状态或所有者",
        {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "owner": {"type": "string"}}, "required": ["task_id"]},
        lambda **kw: run_task_update(kw["task_id"], kw.get("status"), kw.get("owner")), builtin=True, editable=False)
    registry.register("task_bind_worktree", "将任务绑定到工作树",
        {"type": "object", "properties": {"task_id": {"type": "integer"}, "worktree": {"type": "string"}, "owner": {"type": "string"}}, "required": ["task_id", "worktree"]},
        lambda **kw: run_task_bind_worktree(kw["task_id"], kw["worktree"], kw.get("owner", "")), builtin=True, editable=False)
    registry.register("worktree_create", "创建 Git worktree，可选绑定任务",
        {"type": "object", "properties": {"name": {"type": "string"}, "task_id": {"type": "integer"}, "base_ref": {"type": "string"}}, "required": ["name"]},
        lambda **kw: run_worktree_create(kw["name"], kw.get("task_id"), kw.get("base_ref", "HEAD")), builtin=True, editable=False)
    registry.register("worktree_list", "列出所有工作树",
        {"type": "object", "properties": {}},
        lambda **kw: run_worktree_list(), builtin=True, editable=False)
    registry.register("worktree_status", "显示工作树的 Git 状态",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        lambda **kw: run_worktree_status(kw["name"]), builtin=True, editable=False)
    registry.register("worktree_run", "在工作树目录中执行命令",
        {"type": "object", "properties": {"name": {"type": "string"}, "command": {"type": "string"}}, "required": ["name", "command"]},
        lambda **kw: run_worktree_run(kw["name"], kw["command"]), builtin=True, editable=False)
    registry.register("worktree_keep", "保留工作树",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        lambda **kw: run_worktree_keep(kw["name"]), builtin=True, editable=False)
    registry.register("worktree_remove", "删除工作树，可选完成任务",
        {"type": "object", "properties": {"name": {"type": "string"}, "force": {"type": "boolean"}, "complete_task": {"type": "boolean"}}, "required": ["name"]},
        lambda **kw: run_worktree_remove(kw["name"], kw.get("force", False), kw.get("complete_task", False)), builtin=True, editable=False)
    registry.register("worktree_events", "查看工作树生命周期事件",
        {"type": "object", "properties": {"limit": {"type": "integer"}}},
        lambda **kw: run_worktree_events(kw.get("limit", 20)), builtin=True, editable=False)
    registry.register("workflow_start", "启动一个工程化工作流（架构→需求→设计→固化→执行→验证→修正），返回会话ID和第一步指令",
        {"type": "object", "properties": {"session_id": {"type": "string", "description": "可选，不提供则自动生成"},"task_id": {"type": "integer", "description": "可选，绑定任务板ID"},}, "additionalProperties": False,},
        lambda **kw: run_workflow_start(kw.get("session_id", ""), kw.get("task_id")), builtin=True, editable=False)
    registry.register("workflow_step", "推进工作流：提交当前阶段产出和事件（如 confirm, execute_done, verify_pass, verify_fail, refine_done）", {"type": "object", "properties": {"session_id": {"type": "string"}, "event": {"type": "string", "enum": ["confirm", "execute_done", "verify_pass", "verify_fail", "refine_done", "abort"]}, "artifact": {"type": "string", "description": "当前阶段的产出文本（可选，但建议传入）"},}, "required": ["session_id", "event"], },
        lambda **kw: run_workflow_step(kw["session_id"], kw["event"], kw.get("artifact", "")), builtin=True, editable=False)
    registry.register("workflow_status", "查询工作流当前状态", {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"], },
        lambda **kw: run_workflow_status(kw["session_id"]), builtin=True, editable=False)
    
    # 元操作工具组 (总管家模式 - 用户唯一入口)
    if META_DISPATCHER_AVAILABLE:
        # 用户唯一入口：启动 workflow
        registry.register("meta_dispatch", 
            "【用户唯一入口】启动 workflow，返回第一阶段执行指令。这是你（LLM）应该首先调用的工具！它会识别任务范式并返回具体的执行步骤。",
            {"type": "object", "properties": {
                "query": {"type": "string", "description": "用户的原始query"},
                "context": {"type": "object", "description": "上下文信息（可选）"},
                "force_paradigm": {"type": "string", "enum": ["CODE_DEV", "FEATURE_DESIGN", "ENGINEERING", "TEST_EVAL", "DOC_WRITING", "DATA_ANALYSIS", "GENERAL"], "description": "强制指定范式（可选）"}
            }, "required": ["query"]},
            lambda **kw: run_meta_dispatch(kw["query"], kw.get("context"), kw.get("force_paradigm")), builtin=True, editable=False)
        
        # 推进 workflow
        registry.register("meta_step",
            "【核心工具】推进 workflow 到下一阶段。在执行完当前阶段的工具调用后，必须调用此工具推进！",
            {"type": "object", "properties": {
                "session_id": {"type": "string", "description": "会话ID（从 meta_dispatch 获取）"},
                "event": {"type": "string", "enum": ["confirm", "execute_done", "verify_pass", "verify_fail", "refine_done"], "description": "触发事件，默认 confirm"},
                "artifact": {"type": "string", "description": "当前阶段产出（可选）"},
                "tool_calls": {"type": "array", "description": "当前阶段已执行的工具调用列表（可选）"}
            }, "required": ["session_id"]},
            lambda **kw: run_meta_step(kw["session_id"], kw.get("event", "confirm"), kw.get("artifact", ""), kw.get("tool_calls")), builtin=True, editable=False)
        
        # 查询状态
        registry.register("meta_status", "查询当前 workflow 执行状态",
            {"type": "object", "properties": {"session_id": {"type": "string", "description": "会话ID"}}},
            lambda **kw: run_meta_status(kw.get("session_id")), builtin=True, editable=False)
        
        # 列出流程
        registry.register("meta_list", "列出所有可用的流程",
            {"type": "object", "properties": {}},
            lambda **kw: run_meta_list(), builtin=True, editable=False)

# 创建全局注册表实例
registry = ToolRegistry()
# 在内置工具定义之前需要先有 run_* 函数，这些函数定义在后面，但这里先创建注册表，稍后调用 _make_base_tools() 时这些函数已存在
# 所以将 _make_base_tools 调用放在所有 run_* 函数定义之后，但为了代码组织，我们在所有 run_* 函数定义完后显式调用。
# 下面先定义 run_* 函数等，然后最后调用 _make_base_tools()

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
        return [clean_ellipsis(item) for item in obj]
    return obj

# ========== 工具实现 (lead 端使用) ==========
def run_bash(command: str, timeout: int = DEFAULT_TIMEOUT) -> Tuple[str, Optional[str]]:
    cmd_lower = command.lower().strip()
    for blocked in BLOCKED_COMMANDS:
        if blocked in cmd_lower:
            return "", f"危险命令被阻止: {command}"
    if IS_WINDOWS:
        while '\\\\' in command:
            command = command.replace('\\\\', '\\')
        command = command.replace('\\"', '"')
        if command.startswith('dir') and command.endswith('\\'):
            command = command.rstrip('\\')
    if IS_WINDOWS:
        shell_cmd = ["cmd.exe", "/c", command]
        use_shell = False
    else:
        shell_cmd = command
        use_shell = True
    try:
        if use_shell:
            result = subprocess.run(shell_cmd, shell=True, cwd=WORKDIR,
                                    capture_output=True, text=True, timeout=timeout)
        else:
            result = subprocess.run(shell_cmd, shell=False, cwd=WORKDIR,
                                    capture_output=True, text=True, timeout=timeout)
        output = (result.stdout + result.stderr).strip()
        if not output:
            output = "(无输出)"
        if len(output) > MAX_OUTPUT_SIZE:
            output = output[:MAX_OUTPUT_SIZE] + f"\n...[输出已截断]"
        return output, None
    except subprocess.TimeoutExpired:
        return "", f"命令执行超时 ({timeout}秒)"
    except FileNotFoundError:
        if 'python' in command.lower():
            return "", "错误: Python 命令未找到"
        return "", f"命令不存在: {command}"
    except Exception as e:
        return "", f"执行异常: {str(e)}"

def run_read(path: str, limit: Optional[int] = None) -> Tuple[str, Optional[str]]:
    try:
        fp = safe_path(path)
        if not fp.exists():
            return "", f"文件不存在: {path}"
        try:
            text = fp.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            text = fp.read_text(encoding='gbk', errors='replace')
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
        fp.write_text(content, encoding='utf-8')
        return f"已写入 {len(content)} 字节到 {path}", None
    except Exception as e:
        return "", f"写入文件失败: {str(e)}"

def run_edit(path: str, old_text: str, new_text: str) -> Tuple[str, Optional[str]]:
    try:
        fp = safe_path(path)
        if not fp.exists():
            return "", f"文件不存在: {path}"
        content = fp.read_text(encoding='utf-8', errors='replace')
        if old_text not in content:
            return "", f"在文件 {path} 中未找到要替换的文本"
        new_content = content.replace(old_text, new_text, 1)
        fp.write_text(new_content, encoding='utf-8')
        return f"已编辑文件 {path}", None
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
    return "手动压缩请求，将在下一轮汇总对话。", None

def run_background_run(command: str) -> Tuple[str, Optional[str]]:
    return BG.run(command), None

def run_check_background(task_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
    if task_id is None:
        return BG.check(None), None
    if task_id in BG.tasks:
        return BG.check(task_id), None
    if TEAM._find_member(task_id):
        return f"提示: '{task_id}' 是队友，请使用 list_teammates 查看队友状态，而不是 check_background。", None
    return f"未知任务ID: '{task_id}'。请使用 background_run 启动后台任务，或使用 list_teammates 查看队友。", None

def run_spawn_teammate(name: str, role: str, prompt: str) -> Tuple[str, Optional[str]]:
    team_dir = TEAM_DIR
    inbox_dir = TEAM_DIR / "inbox"
    team_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir.mkdir(parents=True, exist_ok=True)

    config_path = team_dir / "config.json"
    if not config_path.exists():
        default_config = {"team_name": "default", "members": []}
        config_path.write_text(json.dumps(default_config, indent=2, ensure_ascii=False), encoding="utf-8")

    return TEAM.spawn(name, role, prompt), None

def run_activate_teammate(name: str) -> Tuple[str, Optional[str]]:
    return TEAM.activate(name), None

def run_list_teammates() -> Tuple[str, Optional[str]]:
    return TEAM.list_all(), None

def run_send_message(to: str, content: str, msg_type: str = "message") -> Tuple[str, Optional[str]]:
    return BUS.send("lead", to, content, msg_type), None

def run_read_inbox() -> Tuple[str, Optional[str]]:
    msgs = BUS.read_inbox("lead")
    BUS.clear_inbox("lead")
    return json.dumps(msgs, indent=2, ensure_ascii=False), None

def run_broadcast(content: str) -> Tuple[str, Optional[str]]:
    """广播消息给所有队友"""
    for name in TEAM.member_names():
        BUS.send("lead", name, content, "broadcast")
    return f"已广播消息给 {len(TEAM.member_names())} 个队友", None

def handle_shutdown_request(teammate: str) -> Tuple[str, Optional[str]]:
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        _shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send("lead", teammate, "Please shut down gracefully.", "shutdown_request", {"request_id": req_id})
    return f"已向 '{teammate}' 发送关机请求 {req_id}", None

def handle_shutdown_status(request_id: str) -> Tuple[str, Optional[str]]:
    with _tracker_lock:
        info = _shutdown_requests.get(request_id)
    if not info:
        return f"错误: 未知请求ID '{request_id}'", None
    return json.dumps(info), None

def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> Tuple[str, Optional[str]]:
    with _tracker_lock:
        req = _plan_requests.get(request_id)
    if not req:
        return f"错误: 未知计划请求ID '{request_id}'", None
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send("lead", req["from"], feedback, "plan_approval_response", {"request_id": request_id, "approve": approve, "feedback": feedback})
    return f"计划已 {req['status']} (来自 '{req['from']}')", None

def run_subagent(prompt: str) -> Tuple[str, Optional[str]]:
    return "子代理功能已启用，请使用 spawn_teammate 创建持久队友。", None

def run_task_create(subject: str, description: str = "") -> Tuple[str, Optional[str]]:
    return TASKS.create(subject, description), None

def run_task_list() -> Tuple[str, Optional[str]]:
    return TASKS.list_all(), None

def run_task_get(task_id: int) -> Tuple[str, Optional[str]]:
    return TASKS.get(task_id), None

def run_task_update(task_id: int, status: str = None, owner: str = None) -> Tuple[str, Optional[str]]:
    return TASKS.update(task_id, status, owner), None

def run_task_bind_worktree(task_id: int, worktree: str, owner: str = "") -> Tuple[str, Optional[str]]:
    return TASKS.bind_worktree(task_id, worktree, owner), None

def run_worktree_create(name: str, task_id: int = None, base_ref: str = "HEAD") -> Tuple[str, Optional[str]]:
    return WORKTREES.create(name, task_id, base_ref), None

def run_worktree_list() -> Tuple[str, Optional[str]]:
    return WORKTREES.list_all(), None

def run_worktree_status(name: str) -> Tuple[str, Optional[str]]:
    return WORKTREES.status(name), None

def run_worktree_run(name: str, command: str) -> Tuple[str, Optional[str]]:
    return WORKTREES.run(name, command), None

def run_worktree_keep(name: str) -> Tuple[str, Optional[str]]:
    return WORKTREES.keep(name), None

def run_worktree_remove(name: str, force: bool = False, complete_task: bool = False) -> Tuple[str, Optional[str]]:
    return WORKTREES.remove(name, force, complete_task), None

def run_worktree_events(limit: int = 20) -> Tuple[str, Optional[str]]:
    return EVENTS.list_recent(limit), None

def run_workflow_start(session_id: str = "", task_id: int = None) -> Tuple[str, Optional[str]]:
    """启动一个新的工作流，返回当前阶段及指令"""
    if not session_id:
        session_id = str(uuid.uuid4())[:8]
    state = WORKFLOWS.get_or_create(session_id, task_id)
    instruction = {
        "phase": state.phase,
        "message": "请输出架构设计（整体结构、技术选型、约束），然后询问用户确认。"
    }
    return json.dumps({"session_id": session_id, "instruction": instruction}), None

def run_workflow_step(session_id: str, event: str, artifact: str = "") -> Tuple[str, Optional[str]]:
    """提交当前阶段的产出和事件，推进工作流"""
    try:
        new_phase, instruction = WORKFLOWS.transition(session_id, event, artifact)
        return json.dumps({"phase": new_phase, "instruction": instruction}), None
    except ValueError as e:
        return "", f"工作流错误: {str(e)}"

def run_workflow_status(session_id: str) -> Tuple[str, Optional[str]]:
    """查询当前工作流状态"""
    state = WORKFLOWS.get(session_id)
    if not state:
        return f"未找到工作流: {session_id}", None
    info = {
        "session_id": state.session_id,
        "phase": state.phase,
        "artifacts": list(state.artifacts.keys()),
        "task_id": state.task_id,
    }
    return json.dumps(info, indent=2), None

# 现在注册内置工具
_make_base_tools()

# ========== 工具调度矩阵 (由 registry 动态调用) ==========
# 注：agent_loop 中使用 registry.get_tools_def() 和 registry.get_handler()

# ========== TodoManager (保留内部待办) ==========
class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        if len(items) > 20:
            raise ValueError("最多允许 20 个任务")
        validated = []
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i+1)))
            if not text:
                raise ValueError(f"任务 {item_id}: 缺少文本内容")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"任务 {item_id}: 无效状态 '{status}'")
            validated.append({"id": item_id, "text": text, "status": status})
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "暂无待办任务。"
        lines = []
        for item in self.items:
            marker = {"pending":"[ ]", "in_progress":"[>]", "completed":"[x]"}.get(item["status"], "[?]")
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} 任务完成)")
        return "\n".join(lines)

TODO = TodoManager()

# ========== 系统提示（动态包含工具列表） ==========
def get_dynamic_system_prompt() -> str:
    # 获取当前启用的工具名称列表，用于提示
    tools_list = registry.list_tools()
    enabled_tools = [t["name"] for t in tools_list if t["enabled"]]
    tools_desc = ", ".join(enabled_tools) if enabled_tools else "无"
    return f"""你是一个智能助手（团队领导，名为 lead ），当前工作目录: {WORKDIR}，操作系统: {OS_INFO}。
Git 仓库根目录: {REPO_ROOT} (如果为空则不支持 worktree)。创建分支时，如分支已存在，则复用。

当前启用的工具: {tools_desc}

可用工具分类（根据 enable 状态，部分可能被禁用）：
1. 基础文件操作：read_file, write_file, edit_file, bash
2. 技能加载：load_skill
3. 内部待办清单：todo
4. 内部任务：task
5. 发布任务给队员，采用任务面板 + 隔离工作树方式：task_create, task_list, task_get, task_update, task_bind_worktree, worktree_create, worktree_list, worktree_status, worktree_run, worktree_keep, worktree_remove, worktree_events
6. 队员管理：spawn_teammate, activate_teammate, list_teammates, send_message, read_inbox, broadcast, shutdown_request, plan_approval
7. 上下文压缩：compact
8. 后台命令：background_run, check_background
9. 工作流工具：workflow_start, workflow_step, workflow_status
10. **元操作总管家（用户唯一入口）**：meta_dispatch, meta_step, meta_status, meta_list
11. 用户自定义工具

**【最重要】元操作总管家使用方式**：

你（LLM）应该按照以下方式处理用户任务：

1. **首先调用 meta_dispatch**（用户唯一入口）：
   - 参数：query = 用户原始请求
   - 返回：session_id、当前阶段、执行指令

2. **按照指令调用具体工具**：
   - meta_dispatch 会返回"建议调用的工具"
   - 根据指令调用 read_file, write_file, bash 等工具
   - 完成当前阶段任务

3. **调用 meta_step 推进**：
   - 参数：session_id（从 meta_dispatch 获取），event（默认 "confirm"）
   - 返回：下一阶段指令

4. **重复步骤 2-3** 直到 meta_step 返回 is_done=true

**范式类型**：
- CODE_DEV: 代码开发（ARCH → REQ → DESIGN → EXEC → VERIFY → DONE）
- TEST_EVAL: 测试评估（PLAN → DESIGN → EXEC → REPORT → DONE）
- FEATURE_DESIGN: 功能设计（ANALYZE → DESIGN → REVIEW → DONE）
- ENGINEERING: 工程实践（CONFIG → DEPLOY → VERIFY → DONE）
- DOC_WRITING: 文档编写（PLAN → WRITE → REVIEW → DONE）
- GENERAL: 通用问答（UNDERSTAND → ANSWER → DONE）

**示例执行流程**：
```
用户: "帮我实现一个用户登录功能"
LLM: 
  1. 调用 meta_dispatch(query="帮我实现一个用户登录功能")
     → 返回 session_id="abc123", 当前阶段="ARCH", 建议工具=[read_file, bash]
  
  2. 调用 read_file(path="main.py") 了解项目结构
     调用 bash(command="ls -la") 查看目录
  
  3. 调用 meta_step(session_id="abc123", event="confirm")
     → 返回 当前阶段="REQ", 建议工具=[write_file]
  
  4. 调用 write_file(path="requirements.md", content="...")
  
  5. 调用 meta_step(session_id="abc123", event="confirm")
     → 返回 当前阶段="DESIGN"
  
  ... 继续直到 DONE
  
  6. 当 meta_step 返回 is_done=true 时，任务完成
```

**重要原则**：
- meta_dispatch 是用户唯一入口，不要直接调用底层工具
- 每个阶段完成后必须调用 meta_step 推进
- 底层工具（read_file, write_file 等）只在流程指令建议时调用
- 确保 workflow 完整执行，不要中途停止

**任务隔离工作流**：
- 当你需要处理一个可能与其他工作冲突的任务时，先为该任务创建一个独立的工作树：worktree_create name=短名称 task_id=<任务ID>
- 在工作树内执行命令、修改文件：worktree_run name=短名称 command="..."
- 任务完成后，清理工作树并自动将任务标记为完成：worktree_remove name=短名称 complete_task=true
- 如果需要保留工作树以供后续使用，使用 worktree_keep

**任务发布工作流**：
- 当你需要发布任务给其它队员，先检查队友状态，如果是 shutdown，需要先使用 activate_teammate 激活
- 然后创建任务：task_create subject="描述" description="详细说明"
- 等待队员自动认领 pending 任务（无 owner），队员会进入各自的工作树执行，互不干扰

**工程化工作流使用指南（用于复杂多步任务）**：
- 当用户请求需要严格的分析-设计-实现-验证流程时，先调用 `workflow_start` 获取 session_id。
- 然后严格按照返回的指令输出当前阶段内容（架构、需求、设计...），并调用 `workflow_step` 传入事件 "confirm"（用户确认后）或 "execute_done" 等。
- 只有设计阶段获得用户明确“固化”后，才能进入执行。
- 执行完成后调用 `workflow_step(event="execute_done")`，然后进入验证，根据结果决定下一步。
- 工作流状态由系统自动维持，你只需每次调用 `workflow_step` 传递正确的产出和事件。

原则：
- 对于需要并行安全执行的任务，务必使用 task+worktree 模式。
- 分配任务给队友时，可以先确认队员存在且激活，然后创建任务，队友空闲时会自动认领。
- 给出最终答案前，确保所有 todo 都已标记完成。
"""

# ========== 多模型客户端 ==========
class MultiModelClient:
    def __init__(self):
        api_base = os.getenv("LLM_API_BASE")
        api_key = os.getenv("API_KEY") or os.getenv("LLM_API_KEY")  # 兼容两种变量名
        self.model = os.getenv("LLM_MODEL")
        if not all([api_base, api_key, self.model]):
            raise ValueError("请设置环境变量: LLM_API_BASE, LLM_API_KEY (或 API_KEY), LLM_MODEL")
        
        # 增强超时配置：连接超时 30s，读取超时 180s
        import httpx
        
        # 支持代理环境变量
        proxies = None
        if os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY"):
            proxies = {
                "http://": os.getenv("HTTP_PROXY"),
                "https://": os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"),
            }
        
        http_client = httpx.Client(
            timeout=httpx.Timeout(30.0, read=180.0, write=60.0, connect=30.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            proxy=proxies.get("https://") if proxies else None,
        )
        
        self.client = OpenAI(
            base_url=api_base, 
            api_key=api_key, 
            timeout=180.0,  # 增加总超时
            max_retries=3,   # 增加重试次数
            http_client=http_client
        )
    
    def _retry_on_timeout(self, func, *args, max_retries=3, **kwargs):
        """带重试的调用"""
        import time
        last_error = None
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                if "timeout" in error_str or "timed out" in error_str or "connection" in error_str:
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt  # 指数退避
                        print(f"\n[网络超时，{wait_time}秒后重试 ({attempt+1}/{max_retries})]")
                        time.sleep(wait_time)
                        continue
                raise
        raise last_error

    def chat(self, messages: List[Dict], tools: List[Dict], stream: bool = False, stream_callback: Optional[Callable[[str], None]] = None) -> Dict:
        clean_messages = clean_ellipsis(messages)
        clean_tools = clean_ellipsis(tools)
        if not all(isinstance(m, dict) for m in clean_messages):
            clean_messages = [m for m in clean_messages if isinstance(m, dict)]
        if not stream:
            return self._chat_no_stream(clean_messages, clean_tools)
        else:
            try:
                return self._chat_stream(clean_messages, clean_tools, stream_callback)
            except Exception as e:
                print(f"\n[流式调用失败，切换到非流式] {e}")
                return self._chat_no_stream(clean_messages, clean_tools)

    def _chat_no_stream(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        def _call_api():
            return self.client.chat.completions.create(
                model=self.model, messages=messages, tools=tools if tools else None,
                tool_choice="auto" if tools else None, temperature=0.7, max_tokens=4096)
        
        try:
            response = self._retry_on_timeout(_call_api, max_retries=3)
            choice = response.choices[0]
            message = choice.message
            tool_calls = []
            if message.tool_calls:
                for tc in message.tool_calls:
                    tool_calls.append({"id": tc.id, "type": tc.type,
                                       "function": {"name": tc.function.name, "arguments": tc.function.arguments}})
            return {"content": message.content or "", "tool_calls": tool_calls, "finish_reason": choice.finish_reason}
        except APIError as e:
            error_msg = str(e)
            # 增强错误提示
            if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                help_msg = "\n提示: 请检查网络连接或设置代理环境变量 (HTTP_PROXY/HTTPS_PROXY)"
                return {"content": "", "tool_calls": [], "finish_reason": "error", 
                        "error": f"网络超时: {error_msg}{help_msg}"}
            return {"content": "", "tool_calls": [], "finish_reason": "error", "error": f"API错误: {e}"}
        except Exception as e:
            error_msg = str(e)
            if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                return {"content": "", "tool_calls": [], "finish_reason": "error", 
                        "error": f"网络超时: {error_msg}\n提示: 请检查网络连接或设置代理"}
            return {"content": "", "tool_calls": [], "finish_reason": "error", "error": f"调用失败: {str(e)}"}
        except Exception as e:
            return {"content": "", "tool_calls": [], "finish_reason": "error", "error": f"调用失败: {str(e)}"}

    def _chat_stream(self, messages: List[Dict], tools: List[Dict], stream_callback: Optional[Callable[[str], None]] = None) -> Dict:
        stream = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, tool_choice="auto",
            temperature=0.7, max_tokens=4096, stream=True)
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
                print(delta.content, end='', flush=True)
                collected_content.append(delta.content)
                if stream_callback:
                    stream_callback(delta.content)
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_call_index_map:
                        new_tc = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                        tool_call_index_map[idx] = len(collected_tool_calls)
                        collected_tool_calls.append(new_tc)
                    pos = tool_call_index_map[idx]
                    if tc_delta.id:
                        collected_tool_calls[pos]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            collected_tool_calls[pos]["function"]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            collected_tool_calls[pos]["function"]["arguments"] += tc_delta.function.arguments
        print()
        formatted_tool_calls = []
        for tc in collected_tool_calls:
            if tc["function"]["name"]:
                args_str = tc["function"]["arguments"]
                if not isinstance(args_str, str):
                    args_str = str(args_str)
                formatted_tool_calls.append({"id": tc["id"], "type": tc["type"],
                                            "function": {"name": tc["function"]["name"], "arguments": args_str}})
        return {"content": "".join(collected_content), "tool_calls": formatted_tool_calls, "finish_reason": last_finish_reason or "stop"}

# ========== 数据类 ==========
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

# ========== 主控循环 ==========
def agent_loop(
    initial_prompt: str, 
    max_iterations: int = 200, 
    tool_callback: Optional[Callable] = None, 
    stream_callback: Optional[Callable[[str], None]] = None,
    history_messages: List[Dict] = None
) -> AgentResult:
    """
    Agent 主循环
    
    Args:
        initial_prompt: 用户的当前输入
        max_iterations: 最大迭代次数
        tool_callback: 工具调用回调
        stream_callback: 流式输出回调
        history_messages: 历史消息列表（用于恢复上下文）
    
    Returns:
        AgentResult: 包含最终答案和工具调用记录
    """
    try:
        llm = MultiModelClient()
    except ValueError as e:
        return AgentResult(final_answer="", error=str(e))

    system_prompt = get_dynamic_system_prompt()
    
    # 构建消息列表：系统提示 + 历史 + 当前输入
    messages = [{"role": "system", "content": system_prompt}]
    
    # 添加历史消息（排除系统消息和工具调用细节）
    if history_messages:
        for msg in history_messages:
            role = msg.get("role")
            # 只保留 user 和 assistant 消息
            if role in ("user", "assistant"):
                content = msg.get("content", "")
                if content and len(content) > 20:  # 过滤太短的消息
                    # 截断过长的消息
                    if len(content) > 2000:
                        content = content[:2000] + "\n...[已截断]"
                    messages.append({"role": role, "content": content})
        
        # 如果历史过长，添加摘要提示
        if len(messages) > 10:
            summary_hint = {"role": "user", "content": "[历史对话较长，请根据上下文继续]"}
            messages.insert(-1, summary_hint)
    
    # 添加当前用户输入
    messages.append({"role": "user", "content": initial_prompt})
    
    tool_records: List[ToolCallRecord] = []

    # 预分析（保持不变）
    need_analysis = False
    prompt_lower = initial_prompt.lower()
    # complex_keywords = ["网站", "系统", "完整", "实现", "所有", "开发", "部署", "功能", "数据库", "前端", "后端", "api", "接口", "模型", "页面", "登录", "注册", "上传", "搜索", "推荐", "评分", "小红书", "风景"]
    if len(initial_prompt) > 200:
        need_analysis = True
    elif any(keyword in prompt_lower for keyword in ["计划", "任务分解", "分步骤", "逐步", "一系列", "网站", "系统", "完整"]):
        need_analysis = True

    if need_analysis:
        print("\033[90m[预分析...]\033[0m")
        analysis_messages = [
            {"role": "system", "content": "你是一个任务分析器。分析用户请求，如果完成任务需要超过 3 个步骤，请输出一个 todo 列表（JSON数组，每个元素含 id, text, status='pending'）；否则只输出 'SIMPLE'。"},
            {"role": "user", "content": initial_prompt}
        ]
        analysis_resp = llm._chat_no_stream(analysis_messages, [])
        analysis_text = analysis_resp.get("content", "").strip()
        if analysis_text != "SIMPLE" and (analysis_text.startswith("[") or analysis_text.startswith("{")):
            try:
                todo_items = json.loads(analysis_text)
                if isinstance(todo_items, list):
                    todo_output, _ = run_todo(todo_items)
                    plan_msg = {"role": "assistant", "content": "根据任务复杂度，我已为您创建以下任务计划："}
                    tool_result_msg = {"role": "tool", "tool_call_id": "preanalysis", "content": todo_output}
                    messages.append(plan_msg)
                    messages.append(tool_result_msg)
                    tool_records.append(ToolCallRecord(tool_name="todo", arguments={"items": todo_items}, output=todo_output, error=None))
                    print(f"\033[32m[自动计划] 已生成任务列表:\n{todo_output}\033[0m")
            except json.JSONDecodeError:
                print("[分析] JSON 解析失败，不注入计划")
        else:
            pass        
    else:
        pass

    iteration = 0
    rounds_since_todo = 0
    STUCK_THRESHOLD = 2
    call_signatures = []
    consecutive_identical = 0
    wait_until = 0

    while iteration < max_iterations:
        iteration += 1

         # ----- 等待队友回复阶段 -----
        if wait_until > time.time():
            inbox_msgs = BUS.read_inbox("lead")
            if inbox_msgs:
                messages.append({"role": "user", "content": f"<inbox>\n{json.dumps(inbox_msgs, indent=2, ensure_ascii=False)}\n</inbox>"})
                BUS.clear_inbox("lead")
                wait_until = 0
            else:
                time.sleep(5)
                continue
        else:
            wait_until = 0
        # -------------

        micro_compact(messages)
        if estimate_tokens(messages) > THRESHOLD:
            print("\033[90m[auto_compact triggered]\033[0m")
            messages = auto_compact(messages, llm)
            call_signatures = []
            consecutive_identical = 0
            rounds_since_todo = 0

        notifs = BG.drain_notifications()
        if notifs:
            notif_text = "\n".join(f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs)
            messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})

        inbox_msgs = BUS.read_inbox("lead")
        if inbox_msgs:
            messages.append({"role": "user", "content": f"<inbox>\n{json.dumps(inbox_msgs, indent=2, ensure_ascii=False)}\n</inbox>"})
            BUS.clear_inbox("lead")

        todo_state = TODO.render()
        has_incomplete = any(item["status"] != "completed" for item in TODO.items) if TODO.items else False
        if has_incomplete:
            progress_msg = {"role": "user", "content": f"【当前待办进度】\n{todo_state}\n\n请继续执行下一个未完成的任务。不要输出最终答案。"}
            messages.append(progress_msg)

        if rounds_since_todo >= 3:
            reminder_msg = {"role": "user", "content": "<reminder>你已连续多轮未更新任务列表(todo)，请使用 todo 工具规划或更新当前进度。</reminder>"}
            messages.append(reminder_msg)
            rounds_since_todo = 0

        if consecutive_identical >= STUCK_THRESHOLD:
            last_sig = call_signatures[-1] if call_signatures else ""
            if "task:" in last_sig:
                stuck_msg = {"role": "user", "content": "<system-reminder>⚠️ 重复委托子任务，请停止并直接给出答案。</system-reminder>"}
            else:
                stuck_msg = {"role": "user", "content": "<system-reminder>⚠️ 检测到重复调用相同工具，请改变策略或完成任务。</system-reminder>"}
            messages.append(stuck_msg)
            consecutive_identical = 0
            call_signatures = []

        # 获取当前启用的工具定义
        tools_def = registry.get_tools_def()

        print("\033[90m[模型思考]\033[0m ", end='', flush=True)
        resp = llm.chat(messages, tools_def, stream=True, stream_callback=stream_callback)
        print()

        if resp.get("error"):
            return AgentResult(final_answer="", tool_calls=tool_records, error=resp["error"])

        assistant_msg = {"role": "assistant", "content": resp["content"]}
        if resp["tool_calls"]:
            assistant_msg["tool_calls"] = [{"id": tc["id"], "type": tc["type"], "function": tc["function"]} for tc in resp["tool_calls"]]
        messages.append(assistant_msg)

        if not resp["tool_calls"]:
            more_inbox = BUS.read_inbox("lead")
            if more_inbox:
                messages.append({"role": "user", "content": f"<inbox>\n{json.dumps(more_inbox, indent=2, ensure_ascii=False)}\n</inbox>"})
                BUS.clear_inbox("lead")
                continue

            todo_state = TODO.render()
            has_incomplete = any(item["status"] != "completed" for item in TODO.items) if TODO.items else False
            if has_incomplete:
                reminder = {"role": "user", "content": f"【尚未完成】你尝试结束，但以下任务仍待完成：\n{todo_state}\n请继续执行未完成的任务。"}
                messages.append(reminder)
                continue
            else:
                return AgentResult(final_answer=resp["content"], tool_calls=tool_records, error=None)

        # 处理工具调用
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
                prompt_key = prompt.strip()[:200] if isinstance(prompt, str) else str(prompt)[:200]
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

        used_todo = False
        manual_compact = False
        for tc in resp["tool_calls"]:
            tool_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
            except json.JSONDecodeError as e:
                args = {}
                error_msg = f"参数 JSON 解析失败: {e}"
                tool_records.append(ToolCallRecord(tool_name=tool_name, arguments={}, output="", error=error_msg))
                tool_result_content = f"错误: {error_msg}"
                tool_result_msg = {"role": "tool", "tool_call_id": tc["id"], "content": tool_result_content}
                messages.append(tool_result_msg)
                print(f"\n\033[33m🔧 {tool_name}\033[0m 参数解析失败\n   └─ {error_msg}")
                continue

            # 从注册表获取处理器
            handler = registry.get_handler(tool_name)
            if handler is None:
                output, error = "", f"工具 '{tool_name}' 未启用或不存在"
            else:
                try:
                    output, error = handler(**args)
                except Exception as e:
                    output, error = "", f"工具执行异常: {str(e)}\n{traceback.format_exc()}"
            
            if tool_callback:
                tool_callback(tool_name, args, output if not error else f"错误: {error}")

            tool_records.append(ToolCallRecord(tool_name=tool_name, arguments=args, output=output, error=error))
            tool_result_content = output if not error else f"错误: {error}"
            tool_result_msg = {"role": "tool", "tool_call_id": tc["id"], "content": tool_result_content}
            messages.append(tool_result_msg)

            print(f"\n\033[33m🔧 {tool_name}\033[0m {args}")
            preview = tool_result_content[:200] + ("..." if len(tool_result_content) > 200 else "")
            print(f"   └─ {preview}")

            if tool_name == "todo":
                used_todo = True
            elif tool_name == "compact":
                manual_compact = True

            if tool_name == "send_message" and args.get("to") in TEAM.member_names():
                wait_until = time.time() + 120
            elif tool_name == "spawn_teammate":
                wait_until = time.time() + 10

            if tool_name == "task_create":
                # 创建任务后，队友可能自动认领并回复，给予短暂等待
                wait_until = time.time() + 120
            elif tool_name == "task_update" and args.get("owner") in TEAM.member_names():
                wait_until = time.time() + 15

        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1

        if manual_compact:
            print("\033[90m[manual compact triggered]\033[0m")
            messages = auto_compact(messages, llm)
            call_signatures = []
            consecutive_identical = 0
            rounds_since_todo = 0

    return AgentResult(final_answer="达到最大循环次数，可能未完成任务。", tool_calls=tool_records, error="循环超限")

# ========== CLI 主入口（保留，但 Web UI 更推荐） ==========
def main():
    print("\033[1;36m=== Agent Loop with Task Isolation (Worktree + Task Binding) ===\033[0m")
    print(f"技能目录: {SKILLS_DIR}")
    print(f"团队通信目录: {TEAM_DIR}")
    print(f"任务板目录: {REPO_ROOT / '.tasks'}")
    print(f"工作树目录: {REPO_ROOT / '.worktrees'}")
    if not WORKTREES.git_available:
        print("\033[33m注意: 当前目录不在 Git 仓库中，工作树相关工具将不可用。\033[0m")
    print("输入 /help 查看帮助，多行输入使用 Esc+Enter 提交，普通 Enter 换行。\n")

    if PromptSession is None:
        print("错误: prompt_toolkit 未安装，CLI 不可用。请运行: pip install prompt_toolkit pygments")
        return

    session = PromptSession(
        history=FileHistory(str(WORKDIR / ".cli_history")),
        auto_suggest=AutoSuggestFromHistory(),
        multiline=False,
    )

    while True:
        try:
            user_input = session.prompt("用户 >> ")
        except KeyboardInterrupt:
            print("退出。")
            break
        except EOFError:
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        if user_input.startswith("/"):
            if run_builtin_command(user_input):
                continue

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