#!/usr/bin/env python3
"""
Web UI for PDM Agent – 支持实时工具调用、流式输出、会话持久化、深色模式
增加心跳保持连接，集成队友消息回调与折叠显示
工具调用显示为统一可折叠面板（位于回复框上方）
可插拔工具 DIY 区域：支持启用/禁用内置工具，创建/编辑/删除自定义工具
"""

import os
import threading
import queue
import json
import traceback
import asyncio
import ast
from datetime import datetime
from nicegui import ui, app

from dotenv import load_dotenv
load_dotenv(override=True)

# 设置 WebSocket 超时环境变量
os.environ["WEBSOCKET_PING_INTERVAL"] = "20"
os.environ["WEBSOCKET_PING_TIMEOUT"] = "60"

try:
    from agent_loop import (
        agent_loop, AgentResult, set_teammate_callback,
        registry, run_spawn_teammate, run_list_teammates,  # 用于后端操作
        CUSTOM_TOOLS_DIR, WORKDIR, SKILL_LOADER, validate_tool_code_safety
    )
except ImportError:
    print("错误: 无法导入 agent_loop.py，请确保文件在同一目录下。")
    exit(1)

# ---------- 全局状态 ----------
messages: list = []
tool_calls_history: list = []          # 存储所有工具调用记录（用于统一面板）
result_queue = queue.Queue()
is_running = False
STORAGE_KEY = "pdm_chat_history"
CURRENT_ASSISTANT_MSG_INDEX = -1
timer_handle = None

# 防抖控制：避免频繁刷新
_pending_refresh = {"chat": False, "tool": False, "teammate": False}
_refresh_lock = threading.Lock()

# ---------- 心跳线程 ----------
heartbeat_stop = threading.Event()

def heartbeat_sender():
    while not heartbeat_stop.wait(15):
        if is_running:
            result_queue.put({"type": "heartbeat"})

# ---------- 防抖刷新 ----------
def schedule_refresh(refresh_type: str, delay: float = 0.1):
    """调度延迟刷新，避免频繁刷新导致断连"""
    with _refresh_lock:
        if not _pending_refresh.get(refresh_type):
            _pending_refresh[refresh_type] = True
            ui.timer(delay, lambda: do_refresh(refresh_type), once=True)

def do_refresh(refresh_type: str):
    """执行实际刷新"""
    with _refresh_lock:
        _pending_refresh[refresh_type] = False
    
    try:
        if refresh_type == "chat":
            ui_chat.refresh()
        elif refresh_type == "tool":
            tool_panel.refresh()
        elif refresh_type == "teammate":
            teammate_panel.refresh()
    except Exception as e:
        print(f"刷新失败: {e}")

# ---------- 会话持久化 ----------
def load_history():
    global messages, tool_calls_history
    saved = app.storage.user.get(STORAGE_KEY, [])
    if saved:
        messages = saved
        # 从历史消息中重建工具调用列表
        tool_calls_history = [msg for msg in messages if msg.get("role") == "tool"]
    else:
        messages = [{
            "role": "assistant",
            "content": "👋 你好！我是 PDM Agent，一名会主动干活的智能体。"
        }]
        tool_calls_history = []
    ui_chat.refresh()
    tool_panel.refresh()

def save_history():
    app.storage.user[STORAGE_KEY] = messages

def add_user_message(content: str):
    messages.append({"role": "user", "content": content, "timestamp": datetime.now().isoformat()})
    save_history()
    schedule_refresh("chat")

def add_assistant_message(content: str, tool_calls: list = None):
    global CURRENT_ASSISTANT_MSG_INDEX
    messages.append({
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls,
        "timestamp": datetime.now().isoformat()
    })
    CURRENT_ASSISTANT_MSG_INDEX = len(messages) - 1
    save_history()
    schedule_refresh("chat")

def update_last_assistant_content(fragment: str):
    global CURRENT_ASSISTANT_MSG_INDEX
    if CURRENT_ASSISTANT_MSG_INDEX < 0 or CURRENT_ASSISTANT_MSG_INDEX >= len(messages):
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": None,
            "timestamp": datetime.now().isoformat()
        })
        CURRENT_ASSISTANT_MSG_INDEX = len(messages) - 1
    messages[CURRENT_ASSISTANT_MSG_INDEX]["content"] += fragment
    save_history()
    schedule_refresh("chat")

def add_tool_call_notification(tool_name: str, args: dict, output: str):
    """添加工具调用记录（用于 lead 的工具调用）"""
    record = {
        "role": "tool",
        "tool_name": tool_name,
        "args": args,
        "output": output,
        "timestamp": datetime.now().isoformat()
    }
    messages.append(record)
    tool_calls_history.append(record)   # 同步到历史列表
    save_history()
    schedule_refresh("chat", 0.15)  # 稍长延迟，避免频繁刷新
    schedule_refresh("tool", 0.15)

def refresh_teammate_panel():
    """安全地刷新队友面板"""
    try:
        schedule_refresh("teammate", 0.2)
    except Exception as e:
        print(f"刷新队友面板失败: {e}")

def refresh_tool_diy_panel():
    try:
        ui.timer(0.1, lambda: tool_diy_panel.refresh(), once=True)
    except Exception as e:
        print(f"刷新工具DIY面板失败: {e}")
        traceback.print_exc()

def add_teammate_message(teammate_name: str, subtype: str, data: dict):
    """添加队友消息，并刷新队友面板"""
    if subtype == "assistant":
        content = f"💬 说：\n\n{data.get('content', '')}"
    elif subtype == "tool":
        tool_name = data.get("tool_name", "")
        args = data.get("arguments", {})
        output = data.get("output", "")
        content = f"🔧 调用工具 `{tool_name}`\n\n**参数**\n```json\n{json.dumps(args, ensure_ascii=False, indent=2)[:500]}\n```\n**输出**\n```\n{output[:500]}{'...' if len(output)>500 else ''}\n```"
    elif subtype == "info":
        content = f"ℹ️ {data.get('content', '')}"
    else:
        return

    messages.append({
        "role": "teammate",
        "name": teammate_name,
        "subtype": subtype,
        "content": content,
        "timestamp": datetime.now().isoformat(),
        "raw_data": data
    })
    save_history()
    # 只刷新队友面板，主聊天区不显示队友消息
    refresh_teammate_panel()

# ---------- 工具DIY 相关函数 ----------
def get_builtin_tools():
    """获取内置工具列表（从注册表）"""
    tools = registry.list_tools()
    return [t for t in tools if t.get("builtin")]

def get_custom_tools():
    """获取自定义工具列表"""
    tools = registry.list_tools()
    return [t for t in tools if not t.get("builtin") and t.get("editable")]

def toggle_tool_enable(tool_name: str, enable: bool):
    """启用/禁用工具"""
    if enable:
        registry.enable(tool_name)
    else:
        registry.disable(tool_name)
    refresh_tool_diy_panel()
    ui.notify(f"工具 {tool_name} 已{'启用' if enable else '禁用'}", type="info")

def get_custom_tool_code(tool_name: str) -> str:
    """获取自定义工具代码"""
    # 从注册表获取代码（存储在 _tools 的 code 字段中）
    # 由于 registry 未直接暴露 code，我们通过读取文件获取
    meta_path = CUSTOM_TOOLS_DIR / "custom_tools.json"
    if meta_path.exists():
        with open(meta_path, "r", encoding="utf-8") as f:
            tools_data = json.load(f)
        for t in tools_data:
            if t.get("name") == tool_name:
                return t.get("code", "")
    return ""

def update_custom_tool(name: str, code: str, description: str = None, enabled: bool = None):
    """更新自定义工具"""
    # 需要解析代码获取 description 和 parameters？这里简化，仅更新代码，保留原有的 description 和 parameters
    # 更好的方式：从代码中自动提取参数 schema，复杂，先保持简单
    if not description:
        # 默认描述
        description = f"自定义工具: {name}"
    # 简单参数 schema，假设工具接受任意参数
    parameters = {"type": "object", "properties": {}, "additionalProperties": True}
    # 调用 registry.update_custom_tool
    success = registry.update_custom_tool(name, description=description, parameters=parameters, code=code, enabled=enabled)
    if success:
        refresh_tool_diy_panel()
        ui.notify(f"工具 {name} 已更新", type="positive")
    else:
        ui.notify(f"更新工具 {name} 失败", type="negative")

def delete_custom_tool(name: str):
    """删除自定义工具"""
    success = registry.delete_custom_tool(name)
    if success:
        refresh_tool_diy_panel()
        ui.notify(f"工具 {name} 已删除", type="positive")
    else:
        ui.notify(f"删除工具 {name} 失败", type="negative")

async def create_custom_tool_from_requirement(requirement: str):
    if not requirement or not requirement.strip():
        ui.notify("请输入工具需求", type="warning")
        return

    # 优先尝试使用 tool-generator 技能
    skill_name = "tool-generator"
    skill_content = None
    if skill_name in SKILL_LOADER.skills:
        skill_content = SKILL_LOADER.get_content(skill_name)
        ui.notify("✨ 使用内置「工具生成技能」生成代码...", type="info")
    else:
        ui.notify("未找到 tool-generator 技能，使用普通模式生成", type="warning")

    # 构造提示词
    if skill_content:
        system_prompt = f"""你是一个严格的工具代码生成器。请遵循以下技能规范：

{skill_content}

严格遵守：只输出代码，用```python包裹。"""
    else:
        system_prompt = (
            "你是一个工具生成器。根据用户需求，生成一个完整的 Python 函数，函数名必须为 execute，接收一个字典参数 args，返回字符串。\n"
            "要求：\n- 函数签名: def execute(args: dict) -> str\n- 不要包含危险操作\n- 可以导入标准库模块：json, re, math, datetime, random, itertools, collections\n"
            "只输出代码，用```python和```包裹。"
        )

    user_prompt = f"用户需求: {requirement}"

    try:
        from agent_loop import MultiModelClient
        llm = await asyncio.to_thread(MultiModelClient)
    except Exception as e:
        ui.notify(f"初始化 LLM 失败: {e}", type="negative")
        return

    resp = await asyncio.to_thread(llm._chat_no_stream, [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ], [])
    content = resp.get("content", "")
    if not content:
        ui.notify("LLM 未返回内容", type="warning")
        return

    # 提取代码（复用原有正则）
    import re
    code = None
    match = re.search(r"```python\s*\n(.*?)\n```", content, re.DOTALL)
    if match:
        code = match.group(1).strip()
    else:
        match = re.search(r"```\s*\n(.*?)\n```", content, re.DOTALL)
        if match:
            code = match.group(1).strip()
        else:
            if "def execute" in content:
                code = content.strip()
            else:
                ui.notify("生成的代码不包含 execute 函数，请重试", type="negative")
                return

    # 强制安全检查
    safe, err = validate_tool_code_safety(code)
    if not safe:
        ui.notify(f"生成的代码不安全: {err}", type="negative")
        return

    # 打开编辑对话框
    with ui.dialog() as dialog, ui.card():
        ui.label("生成的工具代码（已通过安全验证），请确认或编辑后保存").classes("text-lg font-bold")
        code_input = ui.textarea(value=code, placeholder="Python 代码...").classes("w-full h-80 font-mono")
        name_input = ui.input(label="工具名称", placeholder="my_tool").classes("w-full")
        
        # 先定义异步函数
        async def do_save():
            tool_name = name_input.value.strip()
            if not tool_name:
                ui.notify("❌ 工具名称不能为空", type="warning")
                return

            final_code = code_input.value
            # 1. 安全检查
            safe, err = validate_tool_code_safety(final_code)
            if not safe:
                ui.notify(f"❌ 代码不安全: {err}", type="negative")
                return

            # 2. 可选：检查是否包含 execute 函数（validate_tool_code_safety 已检查过，但提示更友好）
            if "def execute" not in final_code:
                ui.notify("❌ 工具代码必须包含 'def execute(args: dict) -> str:'", type="negative")
                return

            try:
                # 3. 调用注册（注意这里直接在异步函数中调用同步方法，可能会阻塞 UI，但时间极短）
                #    如果不想阻塞，可以保持 asyncio.to_thread，但要确保内部异常被捕获。
                success, msg = await asyncio.to_thread(
                    registry.add_custom_tool,
                    tool_name,
                    f"自定义工具: {tool_name}",
                    {"type": "object", "properties": {}, "additionalProperties": True},
                    final_code,
                    True
                )
                if success:
                    ui.notify(f"✅ 工具 '{tool_name}' 已创建", type="positive")
                    # 强制刷新 DIY 面板（直接调用 refreshable 对象的 refresh 方法）
                    ui.timer(0.5, lambda: tool_diy_panel.refresh(), once=True)
                    # 关闭对话框
                    dialog.close()
                else:
                    ui.notify(f"❌ 创建失败: {msg}", type="negative")
                    print(f"[DEBUG] add_custom_tool 返回: success={success}, msg={msg}")
            except Exception as e:
                import traceback
                traceback.print_exc()
                ui.notify(f"❌ 保存时发生异常: {str(e)}", type="negative")
        
        # 创建按钮（现在 do_save 已定义）
        ui.button("保存", on_click=do_save).props("color=primary")
        ui.button("取消", on_click=dialog.close).props("flat")
    dialog.open()

# ---------- 回调函数 ----------
def tool_callback(tool_name: str, args: dict, output: str):
    result_queue.put({
        "type": "tool",
        "tool_name": tool_name,
        "args": args,
        "output": output
    })

def stream_callback(fragment: str):
    result_queue.put({
        "type": "stream",
        "fragment": fragment
    })

def teammate_callback(teammate_name: str, subtype: str, data: dict):
    """由 agent_loop 调用的队友消息回调"""
    result_queue.put({
        "type": "teammate",
        "name": teammate_name,
        "subtype": subtype,
        "data": data
    })

def run_agent_in_thread(prompt: str):
    global is_running, CURRENT_ASSISTANT_MSG_INDEX
    CURRENT_ASSISTANT_MSG_INDEX = -1
    try:
        set_teammate_callback(teammate_callback)
        result: AgentResult = agent_loop(prompt, tool_callback=tool_callback, stream_callback=stream_callback)
        result_queue.put({
            "type": "final",
            "content": result.final_answer if result.final_answer else "[无输出]",
            "error": result.error,
            "tool_calls": result.tool_calls
        })
    except Exception as e:
        result_queue.put({"type": "error", "content": str(e) + "\n" + traceback.format_exc()})
    finally:
        is_running = False
        heartbeat_stop.set()

def stop_timer():
    global timer_handle
    if timer_handle is not None:
        timer_handle.active = False
        timer_handle = None

def start_check_timer():
    global timer_handle
    stop_timer()
    # 降低刷新频率：从 0.05 秒改为 0.1 秒
    timer_handle = ui.timer(0.1, check_result)

def send_message(prompt: str):
    global is_running, input_field
    if is_running:
        ui.notify("Agent 正在运行中，请稍后...", type="warning")
        return
    if not prompt.strip():
        return

    add_user_message(prompt)
    input_field.value = ""

    is_running = True
    heartbeat_stop.clear()
    threading.Thread(target=heartbeat_sender, daemon=True).start()
    threading.Thread(target=run_agent_in_thread, args=(prompt,), daemon=True).start()
    start_check_timer()

def check_result():
    global is_running
    try:
        while True:
            res = result_queue.get_nowait()
            if res["type"] == "heartbeat":
                continue
            elif res["type"] == "stream":
                update_last_assistant_content(res["fragment"])
            elif res["type"] == "tool":
                add_tool_call_notification(res["tool_name"], res["args"], res["output"])
            elif res["type"] == "teammate":
                add_teammate_message(res["name"], res["subtype"], res["data"])
            elif res["type"] == "final":
                content = res["content"]
                if res.get("error"):
                    content = f"❌ **错误**: {res['error']}\n\n{content}"
                if CURRENT_ASSISTANT_MSG_INDEX >= 0 and messages[CURRENT_ASSISTANT_MSG_INDEX]["content"] != content:
                    messages[CURRENT_ASSISTANT_MSG_INDEX]["content"] = content
                    save_history()
                    # 最终结果，直接刷新
                    ui_chat.refresh()
                elif CURRENT_ASSISTANT_MSG_INDEX == -1:
                    add_assistant_message(content, res.get("tool_calls"))
                ui.notify("任务完成", type="positive")
                is_running = False
                stop_timer()
                return
            elif res["type"] == "error":
                add_assistant_message(f"❌ **错误**: {res['content']}")
                is_running = False
                stop_timer()
                return
    except queue.Empty:
        pass

# ---------- UI 组件 ----------
@ui.refreshable
def ui_chat():
    """渲染聊天消息（仅用户和助手）"""
    with ui.column().classes("w-full max-w-4xl mx-auto p-4"):
        for msg in messages:
            role = msg.get("role")
            if role == "user":
                with ui.row().classes("justify-end w-full"):
                    with ui.card().classes("bg-blue-100 dark:bg-blue-900 max-w-[80%]").props("flat"):
                        ui.markdown(msg["content"]).classes("text-sm")
            elif role == "assistant":
                with ui.row().classes("justify-start w-full"):
                    with ui.card().classes("bg-gray-100 dark:bg-gray-800 max-w-[80%]").props("flat"):
                        ui.markdown(msg["content"]).classes("text-sm")

@ui.refreshable
def teammate_panel():
    """独立的队友活动面板 - 每个队友一个折叠面板"""
    teammate_msgs = [m for m in messages if m.get("role") == "teammate"]
    with ui.column().classes("w-full max-w-4xl mx-auto mb-2"):
        if not teammate_msgs:
            with ui.expansion(text="🤖 队友活动", icon="group").classes("w-full").props("dense"):
                ui.label("暂无队友消息").classes("text-xs text-gray-400")
            return

        # 按队友名称分组
        groups = {}
        for msg in teammate_msgs:
            name = msg.get("name", "unknown")
            groups.setdefault(name, []).append(msg)

        for name, msgs in groups.items():
            msgs_sorted = sorted(msgs, key=lambda x: x.get("timestamp", ""))
            latest = msgs_sorted[-1]
            preview = latest.get("content", "").replace('\n', ' ')[:100]
            if len(latest.get("content", "")) > 100:
                preview += "..."

            with ui.expansion(
                text=f"🤖 队友 {name} ({len(msgs_sorted)} 条消息)",
                caption=preview,
                icon="group"
            ).classes("w-full").props("dense"):
                for idx, msg in enumerate(msgs_sorted):
                    subtype = msg.get("subtype", "message")
                    icon = "💬" if subtype == "assistant" else ("🔧" if subtype == "tool" else "ℹ️")
                    label = "发言" if subtype == "assistant" else ("工具调用" if subtype == "tool" else "信息")
                    with ui.column().classes("p-2 border-b border-gray-200 dark:border-gray-700"):
                        ui.markdown(f"**{icon} {label}**").classes("text-xs text-gray-500")
                        ui.markdown(msg["content"]).classes("text-sm")
                        if idx != len(msgs_sorted)-1:
                            ui.separator().classes("my-1")

@ui.refreshable
def tool_panel():
    """工具调用统一面板（位于消息区域底部，紧贴输入框）"""
    if not tool_calls_history:
        with ui.expansion(text="🔧 工具调用历史", icon="build").classes("w-full max-w-4xl mx-auto mb-2"):
            ui.label("暂无工具调用记录").classes("text-xs text-gray-400")
    else:
        latest = tool_calls_history[-1]
        latest_preview = latest.get("output", "").replace('\n', ' ')[:100]
        if len(latest.get("output", "")) > 100:
            latest_preview += "..."
        caption = f"{latest.get('tool_name')} - {latest_preview}" if latest_preview else latest.get('tool_name')
        with ui.expansion(
            text=f"🔧 工具调用历史 ({len(tool_calls_history)} 次)",
            caption=caption,
            icon="build"
        ).classes("w-full max-w-4xl mx-auto mb-2").props("dense"):
            for tc in tool_calls_history:
                with ui.column().classes("p-2 border-b border-gray-200 dark:border-gray-700"):
                    ui.markdown(f"**{tc['tool_name']}**").classes("text-sm font-mono")
                    ui.markdown(f"参数: ```json\n{json.dumps(tc.get('args', {}), ensure_ascii=False, indent=2)[:300]}\n```").classes("text-xs")
                    output_preview = tc.get('output', '')[:300]
                    if len(tc.get('output', '')) > 300:
                        output_preview += "..."
                    ui.markdown(f"输出: ```\n{output_preview}\n```").classes("text-xs")

# ---------- 工具 DIY 面板 ----------
@ui.refreshable
def tool_diy_panel():
    """可插拔工具管理面板：内置工具开关 + 自定义工具编辑/创建"""
    with ui.card().classes("w-full max-w-4xl mx-auto mb-2 shadow-lg"):
        ui.label("🔧 工具管理 (DIY)").classes("text-lg font-bold")
        # 不可编辑的内置工具部分
        ui.label("内置工具 (不可编辑代码)").classes("text-md font-semibold mt-2")
        builtins = get_builtin_tools()
        with ui.row().classes("flex-wrap gap-2"):
            for tool in builtins:
                name = tool["name"]
                enabled = tool["enabled"]
                with ui.card().tight().classes("p-2").props("flat bordered"):
                    # 修改点：移除 label 参数，在按钮后加文字
                    row = ui.row().classes("items-center")
                    sw = ui.switch(value=enabled, on_change=lambda e, n=name: toggle_tool_enable(n, e.value)).props("dense")
                    ui.label(name).classes("text-sm font-mono ml-2")
        ui.separator()
        # 可编辑的自定义工具部分
        ui.label("自定义工具 (可编辑代码)").classes("text-md font-semibold mt-2")
        custom_tools = get_custom_tools()
        if custom_tools:
            for tool in custom_tools:
                name = tool["name"]
                enabled = tool["enabled"]
                with ui.expansion(text=f"📦 {name}", icon="code").classes("w-full").props("dense"):
                    with ui.column().classes("p-2"):
                        # 获取代码
                        code = get_custom_tool_code(name)
                        code_edit = ui.textarea(value=code, label="Python代码", placeholder="def execute(args: dict) -> str: ...").classes("w-full h-64 font-mono")
                        # 修改点：开关布局
                        row = ui.row().classes("items-center")
                        sw = ui.switch(value=enabled, on_change=lambda e, n=name: toggle_tool_enable(n, e.value)).props("dense")
                        ui.label("启用").classes("ml-2")
                        # 保存按钮
                        def save_tool(n=name, code_widget=code_edit):
                            new_code = code_widget.value
                            if "def execute" not in new_code:
                                ui.notify("代码必须包含 execute 函数", type="warning")
                                return
                            update_custom_tool(n, new_code, description=f"自定义工具: {n}", enabled=enabled)
                        ui.button("保存", on_click=save_tool).props("color=primary")
                        # 删除按钮
                        def delete_tool(n=name):
                            delete_custom_tool(n)
                        ui.button("删除", on_click=delete_tool).props("color=negative")
        else:
            ui.label("暂无自定义工具，点击下方按钮创建").classes("text-gray-400")
        # 创建新工具区域
        ui.label("创建自定义工具").classes("text-md font-semibold mt-2")
        with ui.row().classes("gap-2 items-center"):
            requirement_input = ui.input(placeholder="请输入工具需求，例如: 获取当前北京时间").classes("flex-grow")
            ui.button("生成工具", on_click=lambda: create_custom_tool_from_requirement(requirement_input.value)).props("color=positive")

def clear_history():
    global messages, tool_calls_history, CURRENT_ASSISTANT_MSG_INDEX
    messages = [{
        "role": "assistant",
        "content": "🧹 历史已清空。有什么可以帮你的？"
    }]
    tool_calls_history = []
    CURRENT_ASSISTANT_MSG_INDEX = -1
    save_history()
    ui_chat.refresh()
    tool_panel.refresh()
    teammate_panel.refresh()
    ui.notify("会话已清空", type="info")

def toggle_dark_mode():
    ui.dark_mode().toggle()
    app.storage.user['dark_mode'] = ui.dark_mode().value
    ui.update()

input_field = None
drawer = None

def create_ui():
    global input_field, drawer
    ui.page_title("PDM Agent – 任务隔离助手")

    dark_mode_saved = app.storage.user.get('dark_mode', False)
    ui.dark_mode().value = dark_mode_saved

    with ui.header().classes("bg-primary text-white p-4"):
        ui.label("PDM Agent").classes("text-xl font-bold")
        ui.label("Personal Harness").classes("text-sm opacity-80")
        ui.space()
        # 工具DIY按钮
        ui.button(icon="build", on_click=lambda: drawer.toggle()).props("flat round").classes("text-white")
        ui.button(icon="dark_mode", on_click=toggle_dark_mode).props("flat round").classes("text-white")

    # 右侧可滑动抽屉（工具DIY区域）
    with ui.right_drawer(fixed=False, value=False).classes("bg-gray-100 dark:bg-gray-900 p-4 w-96") as drawer:
        ui.label("工具DIY区域").classes("text-xl font-bold mb-4")
        tool_diy_panel()

    with ui.column().classes("w-full items-center"):
        ui_chat()
        tool_panel()
        teammate_panel()

    with ui.footer().classes("bg-gray-100 dark:bg-gray-900 p-4"):
        with ui.row().classes("w-full max-w-4xl mx-auto gap-2"):
            input_field = ui.input(
                placeholder="输入你的需求..."
            ).classes("flex-grow").props("clearable rounded outlined")
            input_field.on('keydown.enter', lambda: send_message(input_field.value))
            ui.button("发送", icon="send", on_click=lambda: send_message(input_field.value)) \
                .props("color=primary flat").classes("px-4")
            ui.button("清空会话", icon="delete", on_click=clear_history) \
                .props("flat").classes("px-2")

    set_teammate_callback(teammate_callback)
    ui.timer(0.1, load_history, once=True)

if __name__ == "__main__":
    ui.colors(primary="#10a37f")
    create_ui()
    ui.run(
        title="PDM Agent",
        reload=False,
        port=8080,
        show=True,
        dark=False,
        storage_secret="pdm_secret"
    )