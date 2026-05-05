#!/usr/bin/env python3
"""
Agent Loop - Autonomous Agents 最终版
- 修复 check_background 工具：若传入队友名称则提示使用 list_teammates
- 强制队友认领/完成任务时向 lead 报告
- 增加 create_task 工具创建任务板文件
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
TASKS_DIR = WORKDIR / ".tasks"
VALID_MSG_TYPES = {"message", "broadcast", "shutdown_request", "shutdown_response", "plan_approval_response"}

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
        if msg_type not in VALID_MSG_TYPES:
            return f"错误: 无效类型 '{msg_type}'。有效类型: {VALID_MSG_TYPES}"
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

# ========== 任务板操作 ==========
def scan_unclaimed_tasks() -> list:
    TASKS_DIR.mkdir(exist_ok=True)
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        try:
            task = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (task.get("status") == "pending"
                and not task.get("owner")
                and not task.get("blockedBy")):
            unclaimed.append(task)
    return unclaimed

def claim_task(task_id: int, owner: str) -> str:
    with _claim_lock:
        path = TASKS_DIR / f"task_{task_id}.json"
        if not path.exists():
            return f"错误: 任务 #{task_id} 不存在"
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            return f"错误: 读取任务 #{task_id} 失败: {e}"
        if task.get("owner"):
            existing = task.get("owner") or "someone else"
            return f"错误: 任务 #{task_id} 已被 {existing} 认领"
        if task.get("status") != "pending":
            return f"错误: 任务 #{task_id} 状态为 '{task.get('status')}'，无法认领"
        if task.get("blockedBy"):
            return f"错误: 任务 #{task_id} 被其他任务阻塞，暂不可认领"
        task["owner"] = owner
        task["status"] = "in_progress"
        path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
        BUS.send(owner, "lead", f"我已认领任务 #{task_id}: {task.get('subject', '')}", "message")
        return f"已认领任务 #{task_id} 给 {owner}（已通知 lead）"

def complete_task(task_id: int, owner: str) -> str:
    with _claim_lock:
        path = TASKS_DIR / f"task_{task_id}.json"
        if not path.exists():
            return f"错误: 任务 #{task_id} 不存在"
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            return f"错误: 读取任务 #{task_id} 失败: {e}"
        if task.get("owner") != owner:
            return f"错误: 任务 #{task_id} 不属于 {owner}"
        if task.get("status") != "in_progress":
            return f"错误: 任务 #{task_id} 状态为 '{task.get('status')}'，无法完成"
        task["status"] = "completed"
        path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
        BUS.send(owner, "lead", f"任务 #{task_id} 已完成: {task.get('subject', '')}", "message")
        return f"任务 #{task_id} 已完成并通知 lead"

def create_task_file(task_id: int, subject: str, description: str = "") -> str:
    TASKS_DIR.mkdir(exist_ok=True)
    task_data = {
        "id": task_id,
        "subject": subject,
        "description": description,
        "status": "pending",
        "owner": None,
        "blockedBy": None,
        "created_at": time.time()
    }
    path = TASKS_DIR / f"task_{task_id}.json"
    if path.exists():
        return f"错误: 任务 #{task_id} 已存在"
    path.write_text(json.dumps(task_data, indent=2, ensure_ascii=False), encoding="utf-8")
    return f"成功创建任务 #{task_id}: {subject}"

def make_identity_block(name: str, role: str, team_name: str) -> dict:
    return {
        "role": "user",
        "content": f"<identity>你是 '{name}'，角色: {role}，团队: {team_name}。请继续你的工作。</identity>",
    }

# ========== TeammateManager ==========
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
            f"你可以使用 read_file, write_file, edit_file, send_message, read_inbox, shutdown_response, plan_approval, idle, claim_task, complete_task, bash 工具。\n"
            f"当你没有更多工作时，调用 idle 工具进入空闲状态。空闲时你会自动轮询收件箱和任务板，发现新任务后会继续工作。\n"
            f"当收到 shutdown_request 时，请调用 shutdown_response 工具响应（approve 表示同意关机）。\n"
            f"对于重大更改，请先使用 plan_approval 工具提交计划给 lead，并等待批准。\n"
            f"你可以主动使用 claim_task 工具认领任务板上的任务，认领后必须立即调用 send_message 向 lead 报告（内容：我已认领任务 #X）。\n"
            f"完成任务后调用 complete_task 工具标记完成（会自动发送消息）。\n"
            f"你的线程会一直运行，除非你同意关机或在空闲超时后自动关机。\n"
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
             "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}}},
            {"type": "function", "function": {"name": "read_inbox", "description": "读取并清空自己的收件箱",
             "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "shutdown_response", "description": "响应 shutdown 请求",
             "parameters": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]}}},
            {"type": "function", "function": {"name": "plan_approval", "description": "向 lead 提交需要批准的计划",
             "parameters": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}}},
            {"type": "function", "function": {"name": "idle", "description": "通知系统进入空闲状态",
             "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "claim_task", "description": "认领任务板上的任务",
             "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}},
            {"type": "function", "function": {"name": "complete_task", "description": "标记任务完成（自动通知lead）",
             "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}},
        ]

        messages = [{"role": "user", "content": prompt}]
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
            if len(messages) <= 3:
                messages.insert(0, make_identity_block(name, role, team_name))
                messages.insert(1, {"role": "assistant", "content": f"我是 {name}。继续工作。"})

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
        polls = IDLE_TIMEOUT // POLL_INTERVAL
        for _ in range(polls):
            time.sleep(POLL_INTERVAL)
            inbox = BUS.read_inbox(name)
            if inbox:
                for msg in inbox:
                    messages.append({"role": "user", "content": f"<inbox>\n{json.dumps(msg, indent=2, ensure_ascii=False)}\n</inbox>"})
                return True
            unclaimed = scan_unclaimed_tasks()
            if unclaimed:
                task = unclaimed[0]
                result = claim_task(task["id"], name)
                if result.startswith("错误:"):
                    continue
                task_prompt = (
                    f"<auto-claimed>任务 #{task['id']}: {task['subject']}\n"
                    f"{task.get('description', '')}</auto-claimed>"
                )
                if len(messages) <= 3:
                    messages.insert(0, make_identity_block(name, role, team_name))
                    messages.insert(1, {"role": "assistant", "content": f"我是 {name}。继续工作。"})
                messages.append({"role": "user", "content": task_prompt})
                messages.append({"role": "assistant", "content": f"已认领任务 #{task['id']}，现在开始工作。"})
                BUS.send(name, "lead", f"我已认领任务 #{task['id']}: {task['subject']}", "message")
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
        if tool_name == "claim_task":
            return claim_task(args["task_id"], sender)
        if tool_name == "complete_task":
            return complete_task(args["task_id"], sender)
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

# ========== 工具实现 ==========
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
    """检查后台任务状态。如果是队友名称，提示使用 list_teammates。"""
    if task_id is None:
        return BG.check(None), None
    # 检查是否是后台任务
    if task_id in BG.tasks:
        return BG.check(task_id), None
    # 检查是否是队友
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

def run_create_task(task_id: int, subject: str, description: str = "") -> Tuple[str, Optional[str]]:
    return create_task_file(task_id, subject, description), None

# ========== Lead 端的协议处理器 ==========
def handle_shutdown_request(teammate: str) -> Tuple[str, Optional[str]]:
    (INBOX_DIR).mkdir(exist_ok=True)
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
    "create_task": lambda **kw: run_create_task(kw["task_id"], kw["subject"], kw.get("description", "")),
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
     "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}}},
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

CREATE_TASK_TOOL_DEF = {"type": "function", "function": {"name": "create_task", "description": "在任务板上创建新任务（生成 .tasks/task_<id>.json 文件，队友会自动认领）",
    "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}, "subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["task_id", "subject"]}}}

TOOLS = BASE_TOOLS_DEF + [TODO_TOOL_DEF, TASK_TOOL_DEF, LOAD_SKILL_TOOL_DEF, COMPACT_TOOL_DEF,
                          BACKGROUND_RUN_TOOL_DEF, CHECK_BACKGROUND_TOOL_DEF] + AGENT_TEAM_TOOLS_DEF + PROTOCOL_TOOLS_DEF + [CREATE_TASK_TOOL_DEF]

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

# ========== 系统提示 ==========
PARENT_SYSTEM_PROMPT = f"""你是一个智能助手（团队领导），当前工作目录: {WORKDIR}，操作系统: {OS_INFO}。
Windows 系统下请注意命令兼容性。

可用工具分类：
1. 基础文件操作：read_file, write_file, edit_file, bash
2. 任务规划：todo（内部待办列表）
3. 任务板管理：create_task（在 .tasks/ 目录创建任务，队友会自动认领）
4. 队友管理：spawn_teammate, list_teammates, send_message, read_inbox, broadcast, shutdown_request, plan_approval
5. 后台命令：background_run（返回 task_id），check_background（只能用返回的 task_id，不可用于队友！）
6. 其他：load_skill, compact, task（子代理，一次性）

重要区分：
- 要查看队友状态，必须使用 list_teammates
- check_background 仅适用于 background_run 启动的后台任务，如果你传入队友名称，会收到错误提示
- shutdown_response 工具（查询）需要传入 request_id，用于查看关机请求状态

任务板工作流：
- 当你需要队友自动完成某项工作时，使用 create_task 创建正式任务（编号、主题、描述）
- 队友会在空闲时自动扫描并认领，认领后会自动向你的收件箱发送消息
- 你也可以直接使用 send_message 分配特定任务给特定队友

原则：
- 避免无限等待队友，如果长时间没有进展，可以主动发送消息询问或使用 shutdown_request
- 任务完成后，如果不再需要队友，请发送 shutdown_request 让其优雅退出
- 给出最终答案前，请确保所有 todo 都已标记完成
"""

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
            item_id = str(item.get("id", str(i+1)))
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
            marker = {"pending":"[ ]", "in_progress":"[>]", "completed":"[x]"}.get(item["status"], "[?]")
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} 任务完成)")
        return "\n".join(lines)

TODO = TodoManager()

# ========== 主控循环 ==========
def agent_loop(initial_prompt: str, max_iterations: int = 50) -> AgentResult:
    try:
        llm = MultiModelClient()
    except ValueError as e:
        return AgentResult(final_answer="", error=str(e))

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

def main():
    print("\n=== 多模型Agent循环 (Autonomous Agents 最终修复版) ===")
    print("环境变量: LLM_API_BASE, LLM_API_KEY, LLM_MODEL")
    print(f"技能目录: {SKILLS_DIR}")
    print(f"团队通信目录: {TEAM_DIR}")
    print(f"任务板目录: {TASKS_DIR}")
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