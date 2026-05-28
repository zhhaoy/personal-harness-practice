# Personal Harness

**A Personal Agent Infrastructure Framework** - An AI Agent runtime foundation with project management, intent tracking, and team collaboration.

## Core Features

- 🎯 **Intent Management** - Ensures LLM accurately understands and follows user's ultimate goals
- 📁 **Project Isolation** - Independent workspace, history, and task lists per project
- 🔄 **Intelligent Workflow** - Process-driven execution by Meta Dispatcher
- 🌳 **Worktree Support** - Git worktree task isolation with code merging and preservation
- 👥 **Team Collaboration** - Multi-agent collaboration with message passing
- 🛠️ **Tool Matrix** - 40+ tools with layered permission management

## Introduction

**The only "hands and feet" of a Large Language Model are tools.**

Every user can build their own "Agent runtime base", where the smallest building block is a tool. Any requirement, any rule can be transformed into a tool:

- File read/write
- Network access
- Skill loading
- Memory storage
- Context isolation
- Team collaboration
- Task isolation
- Intent management
- Project management
- ...

Your mission is to continuously improve this base, keep adding tools, and let your Agent grow and evolve.

## Quick Start

### Requirements

- Python 3.10+
- OpenAI API compatible model service

### Install Dependencies

```bash
pip install nicegui openai prompt-toolkit pyyaml requests httpx python-dotenv
```

### Configure Environment Variables

```bash
# Required
export LLM_API_BASE="your_api_endpoint"
export LLM_API_KEY="your_api_key"
export LLM_MODEL="your_model_name"
```

### Run

**Web UI Mode (Recommended):**

```bash
python ready-to-use-withUI/web_ui.py
# Visit http://localhost:8080
```

**CLI Mode:**

```bash
python ready-to-use-withUI/agent_loop.py
```

## Main Features

### 1. Project Management

Each project has an independent workspace. Switching projects automatically loads the corresponding history and task lists.

```
Project Directory/
├── .pdm/                 # Project-specific data
│   ├── chat_history.json # Conversation history
│   ├── tool_calls.json   # Tool call records
│   ├── todo.json         # Todo items
│   ├── intent.json       # Intent records
│   └── decisions.json    # Decision records
└── .worktrees/          # Worktree directory
```

**Web UI Operations**:
- Click the project name at the top to open the project management dialog
- Select an existing project or create a new one
- History is automatically loaded when switching projects

### 2. Intent Management

Before executing any design or operation, the system guides the LLM to clarify the user's ultimate goals:

| Tool | Purpose |
|------|---------|
| `register_intent` | Register user's primary goals, secondary goals, and constraints |
| `clarify_intent` | Request user clarification when intent is uncertain |
| `verify_action` | Verify if action aligns with user's original intent, detect intent drift |
| `track_decision` | Record important design decisions for traceability |
| `get_intent_status` | Query current intent status |

**Workflow**:
1. `meta_dispatch` identifies paradigm, enters intent clarification phase if low confidence
2. LLM calls `register_intent` to register user intent
3. Before critical operations, call `verify_action` to verify
4. When making important decisions, call `track_decision` to record

### 3. Worktree Management

Worktrees are bound to projects, supporting both Git Worktree and Fallback modes.

**File Operation Tools**:

| Tool | Purpose |
|------|---------|
| `worktree_list_files` | List files in worktree |
| `worktree_read_file` | Read file content in worktree |
| `worktree_copy_files` | Copy files to main project directory |
| `worktree_sync` | Merge code to main branch (without deleting worktree) |

**Code Preservation Methods**:

```python
# Recommended: Merge then delete
worktree_remove(name="xxx", merge_to_main=True)

# Sync during development
worktree_sync(name="xxx")

# Delete worktree only, keep branch
worktree_remove(name="xxx")
```

### 4. Process Types

| Process | Phases | Use Case |
|---------|--------|----------|
| code_development | ARCH → REQ → DESIGN → EXEC → VERIFY → DONE | Code development |
| test_evaluation | PLAN → DESIGN → EXEC → REPORT → DONE | Test evaluation |
| feature_design | ANALYZE → DESIGN → REVIEW → DONE | Feature design |
| engineering | CONFIG → DEPLOY → VERIFY → DONE | Engineering deployment |
| documentation | PLAN → WRITE → REVIEW → DONE | Documentation writing |
| general_qa | UNDERSTAND → ANSWER → DONE | General Q&A |

## Architecture

```
User Layer (Single Entry Point)
       │
       ▼
  meta_dispatch (Grand Steward)
       │
       │ Identify paradigm, create session
       │ Intent clarification (low confidence)
       │ Return first phase instruction
       ▼
  Workflow Execution
       │
       │ LLM calls specific tools
       │ Intent verification (critical operations)
       │ meta_step advances to next phase
       ▼
  Process Layer
       │
       │ CODE_DEV | TEST_EVAL | FEATURE_DESIGN
       │ ENGINEERING | DOC_WRITING | GENERAL
       ▼
  Tool Matrix Layer
       │
       │ File | Task | Team | Workflow | Intent
       │ 40+ tools, layered permission management
       ▼
```

### User-Visible Tools

Users only interact with two tools:

| Tool | Purpose |
|------|---------|
| `meta_dispatch` | Entry point - Start workflow, returns first phase instruction |
| `meta_step` | Advance - Move to next phase after completing current tasks |

## Project Structure

```
.
├── ready-to-use-withUI/        # Source code
│   ├── agent_loop.py           # Core Agent loop
│   ├── meta_dispatcher.py      # Meta dispatcher (Grand Steward)
│   ├── tool_matrix.py          # Tool matrix
│   ├── process_definition.py   # Process definitions
│   ├── intent_tools.py         # Intent management tools
│   ├── project_manager.py      # Project manager
│   └── web_ui.py               # Web interface
├── projects.json               # Project registry
├── AGENTS.md                   # Agent configuration
├── NETWORK_GUIDE.md            # Network configuration guide
├── README.md                   # Project entry
├── README_CN.md                # Chinese README
└── README_EN.md                # English README
```
> The `design/` and `agents/` directories constitute the learning path and process for building the agent_loop main agent loop. For details, refer to the requirements described in `design/`. Each chapter can independently run its corresponding agent for verification.

## Tool Overview

| Category | Tools | Description |
|----------|-------|-------------|
| Basic | `read_file`, `write_file`, `edit_file`, `bash` | File and command operations |
| Planning | `todo` | Todo list management |
| Sub-agent | `task` | Context-isolated subtasks |
| Skill | `load_skill` | Load skills on demand |
| Compression | `compact` | Context compression |
| Background | `background_run`, `check_background` | Parallel task execution |
| Team | `spawn_teammate`, `list_teammates`, `send_message`, `read_inbox` | Multi-agent collaboration |
| Task | `task_create`, `task_list`, `task_update` | Task board management |
| Worktree | `worktree_create`, `worktree_run`, `worktree_list_files`, `worktree_copy_files` | Git worktree management |
| Intent | `register_intent`, `clarify_intent`, `verify_action`, `track_decision` | Intent management |

## Permission Matrix

Different processes have different tool access permissions:

| Process | File | Task | Team | Workflow | Intent |
|---------|:----:|:----:|:----:|:--------:|:------:|
| CODE_DEV | ✅ | ✅ | ✅ | ✅ | ✅ |
| TEST_EVAL | ✅ | ✅ | ❌ | ❌ | ✅ |
| FEATURE_DESIGN | ✅ | ❌ | ❌ | ❌ | ✅ |
| ENGINEERING | ✅ | ✅ | ✅ | ❌ | ✅ |
| DOC_WRITING | ✅ | ❌ | ❌ | ❌ | ✅ |
| GENERAL | ✅ | ❌ | ❌ | ❌ | ✅ |

## Runtime Data

All runtime data is stored in the following directories and can be safely deleted:

- `<project>/.pdm/` - Project-specific data (conversations, tasks, intents)
- `<project>/.worktrees/` - Worktree states
- `.tasks/` - Task board data
- `.team/` - Team configurations
- `.tools/` - Custom tools
- `.transcripts/` - Conversation compression records

## Network Configuration

If you encounter network timeouts, please refer to `NETWORK_GUIDE.md` for proxy configuration or timeout adjustment.

## License

MIT License

## Acknowledgments

- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) for excellent design inspiration
- GLM / DeepSeek / Qwen open-source model communities
