#!/usr/bin/env python3
"""
Web UI for PDM Agent – 支持实时工具调用、流式输出、会话持久化、深色模式
增加心跳保持连接，集成队友消息回调与折叠显示
工具调用显示为统一可折叠面板（位于回复框上方）
"""

import os
import threading
import queue
import json
from datetime import datetime
from nicegui import ui, app

# 设置 WebSocket 超时环境变量
os.environ["WEBSOCKET_PING_INTERVAL"] = "20"
os.environ["WEBSOCKET_PING_TIMEOUT"] = "60"

try:
    from agent_loop import agent_loop, AgentResult, set_teammate_callback
except ImportError:
    print("错误: 无法导入 pdm_main.py，请确保文件在同一目录下。")
    exit(1)

# ---------- 全局状态 ----------
messages: list = []
tool_calls_history: list = []          # 存储所有工具调用记录（用于统一面板）
result_queue = queue.Queue()
is_running = False
STORAGE_KEY = "pdm_chat_history"
CURRENT_ASSISTANT_MSG_INDEX = -1
timer_handle = None

# ---------- 心跳线程 ----------
heartbeat_stop = threading.Event()

def heartbeat_sender():
    while not heartbeat_stop.wait(15):
        if is_running:
            result_queue.put({"type": "heartbeat"})

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
            "content": "👋 你好！我是 PDM Agent，基于 Git Worktree 实现任务隔离。\n\n你可以提出开发需求，我会自动创建任务、工作树并执行。"
        }]
        tool_calls_history = []
    ui_chat.refresh()
    tool_panel.refresh()

def save_history():
    app.storage.user[STORAGE_KEY] = messages

def add_user_message(content: str):
    messages.append({"role": "user", "content": content, "timestamp": datetime.now().isoformat()})
    save_history()
    ui_chat.refresh()

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
    ui_chat.refresh()

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
    ui_chat.refresh()

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
    ui_chat.refresh()
    tool_panel.refresh()                # 刷新工具调用面板

def refresh_teammate_panel():
    """安全地刷新队友面板"""
    try:
        teammate_panel.refresh()
    except Exception as e:
        print(f"刷新队友面板失败: {e}")

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
    """由 pdm_main 调用的队友消息回调"""
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
        result_queue.put({"type": "error", "content": str(e)})
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
    timer_handle = ui.timer(0.05, check_result)

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

def create_ui():
    global input_field
    ui.page_title("PDM Agent – 任务隔离助手")

    dark_mode_saved = app.storage.user.get('dark_mode', False)
    ui.dark_mode().value = dark_mode_saved

    with ui.header().classes("bg-primary text-white p-4"):
        ui.label("PDM Agent").classes("text-xl font-bold")
        ui.label("基于 Git Worktree 的任务隔离助手").classes("text-sm opacity-80")
        ui.space()
        ui.button(icon="dark_mode", on_click=toggle_dark_mode).props("flat round").classes("text-white")

    with ui.column().classes("w-full items-center"):
        ui_chat()
        tool_panel()
        teammate_panel()   # 添加队友面板

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