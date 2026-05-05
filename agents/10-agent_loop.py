#!/usr/bin/env python3
"""
Agent Loop with Task Isolation (Worktree + Task Binding)
- 每个任务拥有独立的 git worktree 目录，并行执行永不冲突
- 任务状态持久化在 .tasks/ 目录，工作树状态记录在 .worktrees/index.json
- 队友可自动认领任务、创建工作树、在隔离环境中运行命令，完成后清理
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

try:
    import yaml
except ImportError:
    print("警告: 未安装 PyYAML，技能加载功能将不可用。请运行: pip install pyyaml")
    yaml = None

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
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60
MAX_TOOL_CALLS_PER_PHASE = 30
MAX_TOTAL_STEPS = 200
STUCK_SIMILAR_THRESHOLD = 2

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
            # 确保返回字符串，而不是 None
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
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name or ""):
            raise ValueError(
                "Invalid worktree name. Use 1-40 chars: letters, numbers, ., _, -"
            )

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

        # 使用 Popen 实现实时输出
        try:
            # Windows 下使用 shell=True 以支持复杂命令
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,   # 行缓冲
            )
            output_lines = []
            # 设置超时计时器
            timer = threading.Timer(timeout, process.terminate)
            timer.start()
            try:
                for line in process.stdout:
                    # 实时打印（带前缀标识）
                    print(f"\033[36m[worktree {name}] {line.rstrip()}\033[0m")
                    output_lines.append(line)
                    # 避免输出过长导致内存暴涨，限制总长度
                    if len(''.join(output_lines)) > MAX_OUTPUT_SIZE:
                        output_lines.append("...[输出过长已截断]...\n")
                        break
            finally:
                timer.cancel()
            process.wait(timeout=1)  # 等待进程结束
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
        # 如果索引中不存在，但实际目录存在，尝试直接从 git 中清理
        if not wt:
            path_candidate = self.dir / name
            if path_candidate.exists():
                # 尝试强制删除这个路径的 worktree
                try:
                    self._run_git(["worktree", "remove", "--force", str(path_candidate)])
                    # 手动从索引中清除（因为之前没有记录）
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

            # 更新索引状态为 removed（或直接删除条目）
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
        inbox_path.write_text("", encoding="utf-8")
        return messages

    def broadcast(self, sender: str, content: str, recipients: list) -> str:
        count = 0
        for name in recipients:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"已广播消息给 {count} 个队友"

BUS = MessageBus(INBOX_DIR)

# ========== TeammateManager (队友线程，使用新的任务+工作树机制) ==========
def make_identity_block(name: str, role: str, team_name: str) -> dict:
    return {
        "role": "user",
        "content": f"<identity>你是 '{name}'，角色: {role}，团队: {team_name}。请继续你的工作。</identity>",
    }

class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        (self.dir / "inbox").mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads: Dict[str, threading.Thread] = {}

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
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        if name in self.threads and self.threads[name].is_alive():
            return f"错误: 队友 '{name}' 的旧线程仍在运行"
        thread = threading.Thread(target=self._autonomous_loop, args=(name, role, prompt), daemon=True)
        self.threads[name] = thread
        thread.start()
        return f"已生成自主队友 '{name}' (角色: {role})"

    def _autonomous_loop(self, name: str, role: str, prompt: str):
        team_name = self.config["team_name"]
        sys_prompt = (
            f"你是队友 '{name}'，角色: {role}，团队: {team_name}，工作目录: {WORKDIR}，操作系统: {OS_INFO}。！！需使用适配该操作系统的bash命令！！ \n"
            f"你可以使用以下工具：\n"
            f"  - 基础文件: read_file, write_file, edit_file, bash\n"
            f"  - 任务管理: task_create, task_list, task_get, task_update, task_bind_worktree\n"
            f"  - 工作树隔离: worktree_create, worktree_list, worktree_status, worktree_run, worktree_keep, worktree_remove, worktree_events\n"
            f"  - 通信工具: send_message, read_inbox, shutdown_response, plan_approval, idle\n"
            f"工作流程建议：\n"
            f"1. 从任务板认领任务：使用 task_list 查看 pending 任务，然后 task_update 设置 owner 为自己，状态 in_progress。\n"
            f"2. 为任务创建工作树：worktree_create name=<任务名> task_id=<任务ID> base_ref=HEAD\n"
            f"3. 在工作树中执行修改：worktree_run name=<任务名> command=\"...\"\n"
            f"4. 任务完成后，使用 worktree_remove name=<任务名> complete_task=true 自动清理并标记完成。\n"
            f"5. 如果需要保留工作树，使用 worktree_keep。\n"
            f"当没有更多工作时，调用 idle 工具进入空闲状态。空闲时会自动轮询收件箱和任务板。\n"
            f"当收到 shutdown_request 时，请调用 shutdown_response 工具响应（approve 表示同意关机）。\n"
            f"对于重大更改，请先使用 plan_approval 工具提交计划给 lead，等待批准。\n"
            f"每个工作阶段最多调用 {MAX_TOOL_CALLS_PER_PHASE} 次工具，超过将强制进入空闲。"
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
            # 任务和工作树工具
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
        client = MultiModelClient()
        global_step = 0
        total_tool_calls = 0
        pending_plan_request_id = None
        error_count = 0
        last_tool_signature = ""
        similar_count = 0

        while True:
            # WORK 阶段
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

            # IDLE 阶段
            self._set_status(name, "idle")
            resume = self._idle_poll(name, messages, role, team_name)
            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")

    def _idle_poll(self, name: str, messages: list, role: str, team_name: str) -> bool:
        """空闲轮询：检查收件箱和未认领的任务"""
        polls = IDLE_TIMEOUT // POLL_INTERVAL
        for _ in range(polls):
            time.sleep(POLL_INTERVAL)
            inbox = BUS.read_inbox(name)
            if inbox:
                for msg in inbox:
                    messages.append({"role": "user", "content": f"<inbox>\n{json.dumps(msg, indent=2, ensure_ascii=False)}\n</inbox>"})
                return True
            # 检查是否有 pending 且未认领的任务
            unclaimed = TASKS.find_pending_unclaimed()
            if unclaimed:
                task = unclaimed[0]
                # 自动认领：更新任务状态和所有者
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
                return True
        print(f"\033[36m[队友 {name}] 空闲超时，自动关机\033[0m")
        return False

    def _execute_teammate_tool(self, sender: str, tool_name: str, args: dict) -> str:
        # 基础文件工具
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
        # 通信工具
        if tool_name == "send_message":
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            msgs = BUS.read_inbox(sender)
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
        # 任务和工作树工具
        if tool_name == "task_create":
            return TASKS.create(args["subject"], args.get("description", ""))
        if tool_name == "task_list":
            return TASKS.list_all()
        if tool_name == "task_get":
            return TASKS.get(args["task_id"])
        if tool_name == "task_update":
            return TASKS.update(args["task_id"], args.get("status"), args.get("owner"))
        if tool_name == "task_bind_worktree":
            return TASKS.bind_worktree(args["task_id"], args["worktree"], args.get("owner", ""))
        if tool_name == "worktree_create":
            return WORKTREES.create(args["name"], args.get("task_id"), args.get("base_ref", "HEAD"))
        if tool_name == "worktree_list":
            return WORKTREES.list_all()
        if tool_name == "worktree_status":
            return WORKTREES.status(args["name"])
        if tool_name == "worktree_run":
            return WORKTREES.run(args["name"], args["command"])
        if tool_name == "worktree_keep":
            return WORKTREES.keep(args["name"])
        if tool_name == "worktree_remove":
            return WORKTREES.remove(args["name"], args.get("force", False), args.get("complete_task", False))
        if tool_name == "worktree_events":
            return EVENTS.list_recent(args.get("limit", 20))
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

TEAM = TeammateManager(TEAM_DIR)

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
    return TEAM.spawn(name, role, prompt), None

def run_list_teammates() -> Tuple[str, Optional[str]]:
    return TEAM.list_all(), None

def run_send_message(to: str, content: str, msg_type: str = "message") -> Tuple[str, Optional[str]]:
    return BUS.send("lead", to, content, msg_type), None

def run_read_inbox() -> Tuple[str, Optional[str]]:
    msgs = BUS.read_inbox("lead")
    return json.dumps(msgs, indent=2, ensure_ascii=False), None

def run_broadcast(content: str) -> Tuple[str, Optional[str]]:
    return BUS.broadcast("lead", content, TEAM.member_names()), None

# lead 端的协议处理器
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
    return "子代理功能已启用", None

# 新任务/工作树工具的 lead 端封装 (直接调用全局实例)
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

# ========== 工具调度矩阵 ==========
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
    "spawn_teammate": lambda **kw: run_spawn_teammate(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates": lambda **kw: run_list_teammates(),
    "send_message": lambda **kw: run_send_message(kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox": lambda **kw: run_read_inbox(),
    "broadcast": lambda **kw: run_broadcast(kw["content"]),
    "shutdown_request": lambda **kw: handle_shutdown_request(kw["teammate"]),
    "shutdown_response": lambda **kw: handle_shutdown_status(kw.get("request_id", "")),
    "plan_approval": lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    # 新任务和工作树工具
    "task_create": lambda **kw: run_task_create(kw["subject"], kw.get("description", "")),
    "task_list": lambda **kw: run_task_list(),
    "task_get": lambda **kw: run_task_get(kw["task_id"]),
    "task_update": lambda **kw: run_task_update(kw["task_id"], kw.get("status"), kw.get("owner")),
    "task_bind_worktree": lambda **kw: run_task_bind_worktree(kw["task_id"], kw["worktree"], kw.get("owner", "")),
    "worktree_create": lambda **kw: run_worktree_create(kw["name"], kw.get("task_id"), kw.get("base_ref", "HEAD")),
    "worktree_list": lambda **kw: run_worktree_list(),
    "worktree_status": lambda **kw: run_worktree_status(kw["name"]),
    "worktree_run": lambda **kw: run_worktree_run(kw["name"], kw["command"]),
    "worktree_keep": lambda **kw: run_worktree_keep(kw["name"]),
    "worktree_remove": lambda **kw: run_worktree_remove(kw["name"], kw.get("force", False), kw.get("complete_task", False)),
    "worktree_events": lambda **kw: run_worktree_events(kw.get("limit", 20)),
}

# ========== 工具定义 ==========
BASE_TOOLS_DEF = [
    {"type": "function", "function": {"name": "bash", "description": "执行Shell命令",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "读取文件",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "写入文件",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "编辑文件",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
]

TODO_TOOL_DEF = {"type": "function", "function": {"name": "todo", "description": "创建或更新任务列表",
    "parameters": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object",
        "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
        "required": ["id", "text", "status"]}}}, "required": ["items"]}}}

TASK_TOOL_DEF = {"type": "function", "function": {"name": "task", "description": "启动子代理",
    "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}}}

LOAD_SKILL_TOOL_DEF = {"type": "function", "function": {"name": "load_skill", "description": "加载技能",
    "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}}

COMPACT_TOOL_DEF = {"type": "function", "function": {"name": "compact", "description": "手动压缩上下文",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}

BACKGROUND_RUN_TOOL_DEF = {"type": "function", "function": {"name": "background_run", "description": "后台运行命令（会返回 task_id）",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}

CHECK_BACKGROUND_TOOL_DEF = {"type": "function", "function": {"name": "check_background", "description": "检查后台任务状态（仅用于 background_run 返回的 task_id，不可用于队友）",
    "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": []}}}

AGENT_TEAM_TOOLS_DEF = [
    {"type": "function", "function": {"name": "spawn_teammate", "description": "生成持久化自主队友",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}}},
    {"type": "function", "function": {"name": "list_teammates", "description": "列出所有队友（查看队友状态）",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "send_message", "description": "给队友发送消息",
     "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": ["message", "broadcast", "shutdown_request", "shutdown_response", "plan_approval_response"]}}, "required": ["to", "content"]}}},
    {"type": "function", "function": {"name": "read_inbox", "description": "读取收件箱",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "broadcast", "description": "广播消息",
     "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}}},
]

PROTOCOL_TOOLS_DEF = [
    {"type": "function", "function": {"name": "shutdown_request", "description": "请求队友关机",
     "parameters": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}}},
    {"type": "function", "function": {"name": "shutdown_response", "description": "查询关机请求状态（传入 request_id）",
     "parameters": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}}},
    {"type": "function", "function": {"name": "plan_approval", "description": "批准或拒绝计划",
     "parameters": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}}},
]

# 新任务和工作树工具定义
TASK_WORKTREE_TOOLS_DEF = [
    {"type": "function", "function": {"name": "task_create", "description": "创建新任务（持久化到 .tasks/task_<id>.json）",
     "parameters": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}}},
    {"type": "function", "function": {"name": "task_list", "description": "列出所有任务",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "task_get", "description": "获取任务详情",
     "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "task_update", "description": "更新任务状态或所有者",
     "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "owner": {"type": "string"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "task_bind_worktree", "description": "将任务绑定到工作树",
     "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}, "worktree": {"type": "string"}, "owner": {"type": "string"}}, "required": ["task_id", "worktree"]}}},
    {"type": "function", "function": {"name": "worktree_create", "description": "创建 Git worktree，可选绑定任务",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "task_id": {"type": "integer"}, "base_ref": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "worktree_list", "description": "列出所有工作树",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "worktree_status", "description": "显示工作树的 Git 状态",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "worktree_run", "description": "在工作树目录中执行命令",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "command": {"type": "string"}}, "required": ["name", "command"]}}},
    {"type": "function", "function": {"name": "worktree_keep", "description": "保留工作树（不删除）",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "worktree_remove", "description": "删除工作树，可选完成任务",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "force": {"type": "boolean"}, "complete_task": {"type": "boolean"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "worktree_events", "description": "查看工作树生命周期事件",
     "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}}}},
]

TOOLS = BASE_TOOLS_DEF + [TODO_TOOL_DEF, TASK_TOOL_DEF, LOAD_SKILL_TOOL_DEF, COMPACT_TOOL_DEF,
                          BACKGROUND_RUN_TOOL_DEF, CHECK_BACKGROUND_TOOL_DEF] + AGENT_TEAM_TOOLS_DEF + PROTOCOL_TOOLS_DEF + TASK_WORKTREE_TOOLS_DEF

# ========== TodoManager (保留内部待办) ==========
class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        if len(items) > 20:
            raise ValueError("最多允许 20 个任务")
        validated = []
        # 不再检查 in_progress 数量
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

# ========== 系统提示 ==========
PARENT_SYSTEM_PROMPT = f"""你是一个智能助手（团队领导），当前工作目录: {WORKDIR}，操作系统: {OS_INFO}。
Git 仓库根目录: {REPO_ROOT} (如果为空则不支持 worktree)。创建分支时，如分支已存在，则复用。

可用工具分类：
1. 基础文件操作：read_file, write_file, edit_file, bash
2. 内部待办：todo（用于拆分你自己当前的工作步骤）
3. **任务面板 + 隔离工作树（推荐并行的任务）**：
   - task_create / task_list / task_get / task_update / task_bind_worktree
   - worktree_create / worktree_list / worktree_status / worktree_run / worktree_keep / worktree_remove / worktree_events
4. 队友管理：spawn_teammate, list_teammates, send_message, read_inbox, broadcast, shutdown_request, plan_approval
5. 后台命令：background_run（返回 task_id），check_background（只能用返回的 task_id，不可用于队友！）
6. 其他：load_skill, compact, task（一次性子代理）

**任务隔离工作流**：
- 当你需要处理一个复杂或可能与其他工作冲突的任务时，先创建任务：task_create subject="描述" description="详细说明"
- 为该任务创建一个独立的工作树：worktree_create name=短名称 task_id=<任务ID>
- 在工作树内执行命令、修改文件：worktree_run name=短名称 command="..."
- 任务完成后，清理工作树并自动将任务标记为完成：worktree_remove name=短名称 complete_task=true
- 如果需要保留工作树以供后续使用，使用 worktree_keep

队友会自动认领 pending 任务（无 owner）并进入各自的工作树执行，互不干扰。

原则：
- 对于需要并行安全执行的任务，务必使用 task+worktree 模式。
- 分配任务给队友时，可以先创建任务，队友空闲时会自动认领。
- 避免无限等待队友，可以主动发送消息询问。
- 给出最终答案前，确保所有 todo 都已标记完成。
"""

# ========== 多模型客户端 ==========
class MultiModelClient:
    def __init__(self):
        api_base = os.getenv("LLM_API_BASE")
        api_key = os.getenv("LLM_API_KEY")
        self.model = os.getenv("LLM_MODEL")
        if not all([api_base, api_key, self.model]):
            raise ValueError("请设置环境变量: LLM_API_BASE, LLM_API_KEY, LLM_MODEL")
        self.client = OpenAI(base_url=api_base, api_key=api_key, timeout=60.0, max_retries=2)

    def chat(self, messages: List[Dict], tools: List[Dict], stream: bool = False) -> Dict:
        clean_messages = clean_ellipsis(messages)
        clean_tools = clean_ellipsis(tools)
        if not all(isinstance(m, dict) for m in clean_messages):
            clean_messages = [m for m in clean_messages if isinstance(m, dict)]
        if not stream:
            return self._chat_no_stream(clean_messages, clean_tools)
        else:
            try:
                return self._chat_stream(clean_messages, clean_tools)
            except Exception as e:
                print(f"\n[流式调用失败，切换到非流式] {e}")
                return self._chat_no_stream(clean_messages, clean_tools)

    def _chat_no_stream(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        try:
            response = self.client.chat.completions.create(
                model=self.model, messages=messages, tools=tools if tools else None,
                tool_choice="auto" if tools else None, temperature=0.7, max_tokens=4096)
            choice = response.choices[0]
            message = choice.message
            tool_calls = []
            if message.tool_calls:
                for tc in message.tool_calls:
                    tool_calls.append({"id": tc.id, "type": tc.type,
                                       "function": {"name": tc.function.name, "arguments": tc.function.arguments}})
            return {"content": message.content or "", "tool_calls": tool_calls, "finish_reason": choice.finish_reason}
        except APIError as e:
            return {"content": "", "tool_calls": [], "finish_reason": "error", "error": f"API错误: {e}"}
        except Exception as e:
            return {"content": "", "tool_calls": [], "finish_reason": "error", "error": f"调用失败: {str(e)}"}

    def _chat_stream(self, messages: List[Dict], tools: List[Dict]) -> Dict:
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

# ========== 主控循环 ==========
def agent_loop(initial_prompt: str, max_iterations: int = 200) -> AgentResult:
    try:
        llm = MultiModelClient()
    except ValueError as e:
        return AgentResult(final_answer="", error=str(e))

    need_analysis = False
    analysis_text = "SIMPLE"
    prompt_lower = initial_prompt.lower()
    complex_keywords = ["网站", "系统", "完整", "实现", "所有", "开发", "部署", "功能", "数据库", "前端", "后端", "api", "接口", "模型", "页面", "登录", "注册", "上传", "搜索", "推荐", "评分", "小红书", "风景"]
    if len(initial_prompt) > 100:
        need_analysis = True
    elif any(keyword in prompt_lower for keyword in complex_keywords):
        need_analysis = True

    if need_analysis:
        print("\033[90m[预分析...]\033[0m")
        analysis_messages = [
            {"role": "system", "content": "你是一个任务分析器。分析用户请求，如果完成任务需要超过2个步骤，请输出一个 todo 列表（JSON数组，每个元素含 id, text, status='pending'）；否则只输出 'SIMPLE'。"},
            {"role": "user", "content": initial_prompt}
        ]
        analysis_resp = llm._chat_no_stream(analysis_messages, [])
        analysis_text = analysis_resp.get("content", "").strip()
    messages = [
        {"role": "system", "content": PARENT_SYSTEM_PROMPT},
        {"role": "user", "content": initial_prompt}
    ]
    tool_records: List[ToolCallRecord] = []
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
        print("[分析] 任务较简单，无需预规划")

    iteration = 0
    rounds_since_todo = 0
    STUCK_THRESHOLD = 2
    call_signatures = []
    consecutive_identical = 0
    working_timeout = 0
    MAX_WORKING_WAIT = 30

    while iteration < max_iterations:
        iteration += 1

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

        print("\033[90m[模型思考]\033[0m ", end='', flush=True)
        resp = llm.chat(messages, TOOLS, stream=True)
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
                working_timeout = 0
                print("\033[90m[检测到新收件消息，继续处理]\033[0m")
                continue

            if TEAM.has_working_members():
                working_timeout += 1
                if working_timeout > MAX_WORKING_WAIT:
                    print(f"\033[91m[警告] 队友一直处于 working 状态超过 {MAX_WORKING_WAIT} 轮，可能已僵死。强制退出。\033[0m")
                    return AgentResult(final_answer="等待队友超时，任务可能未完全完成。", tool_calls=tool_records, error="队友僵死")
                if working_timeout % 5 == 0:
                    print(f"\033[90m[等待队友完成任务... 第{working_timeout}轮]\033[0m")
                time.sleep(1)
                messages.append({"role": "user", "content": "<system-reminder>有队友仍在工作中，请继续等待或检查收件箱。</system-reminder>"})
                continue

            todo_state = TODO.render()
            has_incomplete = any(item["status"] != "completed" for item in TODO.items) if TODO.items else False
            if has_incomplete:
                reminder = {"role": "user", "content": f"【尚未完成】你尝试结束，但以下任务仍待完成：\n{todo_state}\n请继续执行未完成的任务。"}
                messages.append(reminder)
                continue
            else:
                return AgentResult(final_answer=resp["content"], tool_calls=tool_records, error=None)

        working_timeout = 0

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

            handler = TOOL_HANDLERS.get(tool_name)
            if handler is None:
                output, error = "", f"未知工具: {tool_name}"
            else:
                try:
                    output, error = handler(**args)
                except Exception as e:
                    output, error = "", f"工具执行异常: {str(e)}"

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

        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1

        if manual_compact:
            print("\033[90m[manual compact triggered]\033[0m")
            messages = auto_compact(messages, llm)
            call_signatures = []
            consecutive_identical = 0
            rounds_since_todo = 0

    return AgentResult(final_answer="达到最大循环次数，可能未完成任务。", tool_calls=tool_records, error="循环超限")

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

def main():
    print("\n=== Agent Loop with Task Isolation (Worktree + Task Binding) ===")
    print("环境变量: LLM_API_BASE, LLM_API_KEY, LLM_MODEL")
    print(f"技能目录: {SKILLS_DIR}")
    print(f"团队通信目录: {TEAM_DIR}")
    print(f"任务板目录: {REPO_ROOT / '.tasks'}")
    print(f"工作树目录: {REPO_ROOT / '.worktrees'}")
    if not WORKTREES.git_available:
        print("注意: 当前目录不在 Git 仓库中，工作树相关工具将不可用。")
    print("输入任务后，Agent将循环调用工具直至完成。输入 'exit' 退出。\n")
    while True:
        try:
            user_input = input("\033[36m用户 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
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