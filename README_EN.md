# Personal Harness

**A Personal Agent Infrastructure Framework**

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
- ...

Your mission is to continuously improve this base, keep adding tools, and let your Agent grow and evolve.

## Core Concepts

### Harness (Agent Framework)

Beyond the underlying dependencies (LLM API, computing environment), an Agent needs a **framework** layer to connect the Agent with these dependencies. This layer is called **Harness**.

- **General-purpose Tools**: Cross-platform core capabilities such as file I/O, context compression, skill loading, task publishing, team collaboration, etc.
- **Application-specific Tools**: Personal tools or plugins that developers can mount on demand

> The core LLM loop of this project, `agent_loop.py`, is developed and extended step by step based on "`design/01-10 完整需求文档.md`". Each `agent_loop.py` in the "`agents/`" directory strictly corresponds to the respective section in "`design/01-10 完整需求文档.md`", allowing each corresponding `agent_loop.py` to be run section by section.

> At the same time, adhering to the aforementioned philosophy of "everything can be toolified", this project further extends the toolset based on "`10-agent_loop.py`", adding a new set of meta-operational tools called "process tools", and re-stratifies the hierarchy, ultimately forming the architectural shape described below.

### Tool Matrix Architecture

This project adopts a **Layered Tool Matrix Architecture**, with the Meta Dispatcher as the core, managing workflows through process-driven execution.

```
User Layer (Single Entry Point)
       │
       ▼
  meta_dispatch (Grand Steward)
       │
       │ Identify paradigm, create session
       │ Return first phase instruction
       ▼
  Workflow Execution
       │
       │ LLM calls specific tools
       │ meta_step advances to next phase
       ▼
  Process Layer
       │
       │ CODE_DEV | TEST_EVAL | FEATURE_DESIGN
       │ ENGINEERING | DOC_WRITING | GENERAL
       ▼
  Tool Matrix Layer
       │
       │ File | Task | Team | Workflow tools
       │ 30+ tools, invisible to users
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
│   ├── process_definition.py   # Process definitions
│   ├── tool_matrix.py          # Tool matrix
│   └── web_ui.py               # Web interface
├── design/                     # Design documents
│   ├── meta_operation_architecture.md
│   ├── meta_operation_design.md
│   ├── tool_matrix_layer_architecture.md
│   └── tool_matrix_layer_design.md
├── AGENTS.md                   # Agent configuration
├── NETWORK_GUIDE.md            # Network configuration guide
├── README.md                   # Project entry
├── README_CN.md                # Chinese README
└── README_EN.md                # English README
```

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

**CLI Mode:**

```bash
python ready-to-use-withUI/agent_loop.py
```

**Web UI Mode (Recommended):**

```bash
python ready-to-use-withUI/web_ui.py
# Visit http://localhost:8080
```

## Web UI Features

- Real-time streaming output
- Tool call visualization
- Dark mode toggle
- Session persistence
- Tool DIY panel: Enable/disable built-in tools, create custom tools
- Teammate activity panel: Multi-agent collaboration messages

## Process Types

| Process | Phases | Use Case |
|---------|--------|----------|
| code_development | ARCH → REQ → DESIGN → EXEC → VERIFY → DONE | Code development |
| test_evaluation | PLAN → DESIGN → EXEC → REPORT → DONE | Test evaluation |
| feature_design | ANALYZE → DESIGN → REVIEW → DONE | Feature design |
| engineering | CONFIG → DEPLOY → VERIFY → DONE | Engineering deployment |
| documentation | PLAN → WRITE → REVIEW → DONE | Documentation writing |
| general_qa | UNDERSTAND → ANSWER → DONE | General Q&A |

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
| Worktree | `worktree_create`, `worktree_run` | Git worktree management |

## Permission Matrix

Different processes have different tool access permissions:

| Process | File Tools | Task Tools | Team Tools | Workflow Tools |
|---------|:----------:|:----------:|:----------:|:--------------:|
| CODE_DEV | ✅ | ✅ | ✅ | ✅ |
| TEST_EVAL | ✅ | ✅ | ❌ | ❌ |
| FEATURE_DESIGN | ✅ | ❌ | ❌ | ❌ |
| ENGINEERING | ✅ | ✅ | ✅ | ❌ |
| DOC_WRITING | ✅ | ❌ | ❌ | ❌ |
| GENERAL | ✅ | ❌ | ❌ | ❌ |

## Runtime Data

All runtime data is stored in the following directories and can be safely deleted:

- `.transcripts/` - Conversation compression records
- `.tasks/` - Task board data
- `.worktrees/` - Worktree states
- `.team/` - Team configurations
- `.tools/` - Custom tools

## Network Configuration

If you encounter network timeouts, please refer to `NETWORK_GUIDE.md` for proxy configuration or timeout adjustment.

## License

MIT License

## Acknowledgments

- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) for excellent design inspiration
- GLM / DeepSeek / Qwen open-source model communities
