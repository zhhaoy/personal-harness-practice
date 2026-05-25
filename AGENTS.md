# AGENTS.md

## Project Overview

Personal Harness is a Python-based agent infrastructure framework with a **layered tool matrix architecture**. The meta-dispatcher serves as the grand steward, managing all tools through process-driven workflows.

## Architecture (v3.0 - Process-Driven Workflow)

```
┌─────────────────────────────────────────────┐
│            User Layer (唯一入口)             │
│                                             │
│           meta_dispatch (总管家)            │
└──────────────────────┬──────────────────────┘
                       │
                       │ 1. 识别范式，创建 session
                       │ 2. 返回第一阶段指令
                       ▼
┌─────────────────────────────────────────────┐
│            Workflow Execution               │
│                                             │
│  LLM 调用具体工具 → meta_step 推进 → 下一阶段 │
│                                             │
│  循环直到 is_done=true                       │
└──────────────────────┬──────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────┐
│            Process Layer (流程层)            │
│                                             │
│  CODE_DEV | TEST_EVAL | FEATURE_DESIGN     │
│  ENGINEERING | DOC_WRITING | GENERAL        │
│                                             │
│  每个流程: 章程 + 方法 + 机制                │
└──────────────────────┬──────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────┐
│          Tool Matrix Layer (工具矩阵层)      │
│                                             │
│  文件工具 | 任务工具 | 团队工具 | 工作流工具  │
│  (30+ 工具，用户不可见，仅被流程调用)        │
└─────────────────────────────────────────────┘
```

## Entry Points

```bash
# CLI mode
python src/agent_loop.py

# Web UI mode (recommended)
python src/web_ui.py  # runs on http://localhost:8080
```

## Environment Setup

Required environment variables:
- `LLM_API_BASE` - OpenAI-compatible API endpoint
- `LLM_API_KEY` (or `API_KEY`) - API key
- `LLM_MODEL` - model name

Optional:
- `HTTP_PROXY` / `HTTPS_PROXY` - proxy settings (recommended for China mainland)
- `SKILLS_DIR` - custom skills directory

## Dependencies

```bash
pip install nicegui openai prompt-toolkit pyyaml requests httpx
```

## Network Configuration

If you encounter `Request timed out` errors:

1. **Check network connection**
2. **Set proxy** (recommended for China mainland):
   ```bash
   # Windows PowerShell
   $env:HTTP_PROXY = "http://127.0.0.1:7890"
   $env:HTTPS_PROXY = "http://127.0.0.1:7890"
   
   # Linux/Mac
   export HTTP_PROXY="http://127.0.0.1:7890"
   export HTTPS_PROXY="http://127.0.0.1:7890"
   ```
3. **Timeout settings** (default: 180s read, 30s connect)
   - Client auto-retries 3 times with exponential backoff

## User-Visible Tools

**Only TWO tools are visible to users:**

| Tool | Purpose |
|------|---------|
| `meta_dispatch` | **Entry point** - Start workflow, returns first phase instruction |
| `meta_step` | **Advance workflow** - Move to next phase after completing current tasks |

## How It Works

### Step 1: Start Workflow
```python
meta_dispatch(query="帮我实现一个用户登录功能")
# Returns: session_id, current_phase="ARCH", tools_to_call=[read_file, bash]
```

### Step 2: Execute Tools
```python
# Follow instructions from meta_dispatch
read_file(path="main.py")
bash(command="ls -la")
```

### Step 3: Advance Workflow
```python
meta_step(session_id="abc123", event="confirm")
# Returns: current_phase="REQ", tools_to_call=[write_file]
```

### Step 4: Repeat Until Done
```python
# Continue executing tools and calling meta_step
# Until is_done=true
```

## Process Types

| Process | Phases | Events |
|---------|--------|--------|
| code_development | ARCH → REQ → DESIGN → EXEC → VERIFY → DONE | confirm, execute_done, verify_pass, verify_fail |
| test_evaluation | PLAN → DESIGN → EXEC → REPORT → DONE | confirm, execute_done |
| feature_design | ANALYZE → DESIGN → REVIEW → DONE | confirm, approve, reject |
| engineering | CONFIG → DEPLOY → VERIFY → DONE | confirm, deploy_done, verify_pass |
| documentation | PLAN → WRITE → REVIEW → DONE | confirm, draft_done, approve |
| general_qa | UNDERSTAND → ANSWER → DONE | understood, answered |

## Key Principle

**DO NOT call tools directly!** Always use the workflow:
1. `meta_dispatch` → get instruction
2. Execute suggested tools
3. `meta_step` → advance to next phase
4. Repeat until done

## Permission Matrix

| Process | File | Task | Team | Workflow | Context | Background |
|---------|:----:|:----:|:----:|:--------:|:-------:|:----------:|
| CODE_DEV | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| TEST_EVAL | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ |
| FEATURE_DESIGN | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ |
| ENGINEERING | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| DOC_WRITING | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ |
| GENERAL | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ |

## Key Files

| File | Purpose |
|------|---------|
| `src/meta_dispatcher.py` | Grand steward with workflow execution |
| `src/tool_matrix.py` | Tool matrix with permission control |
| `src/process_definition.py` | Process definitions (charter + method + mechanism) |
| `src/agent_loop.py` | Main loop (uses meta_dispatch) |
| `src/web_ui.py` | Web UI interface |
