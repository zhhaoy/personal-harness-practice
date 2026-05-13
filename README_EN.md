# 🤖 Personal Harness — Personal Agent Infrastructure Framework

**The only "hands and feet" of a Large Language Model are tools.**

**Every user can build their own "Agent runtime base", and the smallest building block of this base is the tool.**

**Any requirement/rule can be turned into a tool**:
- File reading/writing
- Network access
- Skill loading
- Memory storage/loading
- Context isolation
- Team creation / task publishing / autonomous claiming
- Task isolation
- ... 

**What you need to do is continuously improve this base, keep adding tools, and let your Agent grow and evolve.**

## 📌 Concept Definition

Besides the underlying dependencies (LLM API, computing environment), an Agent also needs a **framework** to connect itself with those dependencies. This framework is called **Harness**, which includes both general-purpose tools and application-specific tools.

- **General-purpose tools**  
  Core cross-platform capabilities: file reading/writing, context compression, skill loading, task publishing and dashboard, autonomous team collaboration, Git Worktree task isolation, etc.

- **Application-specific tools**  
  On top of the general layer, developers can mount **personal development tools or plugins** as needed (e.g., internal API calls, code review hooks, database queries, etc.).

> Personal Harness = General Harness + Personal Toolbox

## 🧱 Structural Paradigm

```text
+
Hot-reloadable Tool Matrix
├── General-purpose tools (read/write, network, skill, compression, task publishing, autonomous team, worktree…)
└── Personal specific tools (DIY or from plugin marketplace)
```

- **Core Loop**: A purely tool-driven Agent loop. Any LLM action is associated with a tool call, so the loop ends when no more tool calls are needed.
- **Hot-Reload**: Add or modify tools without restarting the main program (dynamically loaded via function mapping).
- **Extension-Friendly**: Each tool is an independent function registered into the tool matrix by category.

## 🛤️ Implementation Path for General-Purpose Tools of the Agent Base (01→10)

This project implements all mechanisms from the minimal core loop to the complete general-purpose tool set incrementally through 10 Python scripts.

1. Design first: in the `design/` folder, there are ten chapters, each adding a requirement/rule, broken down into one or more tools.

2. Each chapter corresponds to a `.py` file (located in the `agents/` folder), each runnable independently, incrementally implementing the mentioned tools, with validation questions.

| Chapter | Core Mechanism | Increment |
|:---:|:---|:---|
| 01 | Core loop + basic file tools | Minimal runnable agent |
| 02 | Tool dispatch matrix + path sandbox | Safe execution of basic operations |
| 03 | ToDo planning tool | Guide agent to plan before acting |
| 04 | `task` subagent (context isolation) | Isolate subtasks from main flow |
| 05 | `load_skill` progressive skill loading | On-demand loading of prompts from `skills/` |
| 06 | Three-level context compression | Micro compression, auto compression, manual compression |
| 07 | `background_run` background task | Run long-running commands in parallel |
| 08 | Persistent teammates + async mailbox | Multi-agent collaboration foundation |
| 09 | Autonomous agents + task board polling | Teammates auto-claim tasks |
| 10 | Git Worktree task isolation | Each task gets its own worktree, no conflicts in parallel |

> Full verification questions can be found in `design/01-10 完整需求文档.md`

> Thanks to the [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) project for providing excellent design ideas, implementation examples, and teaching iteration methodology.

## 📁 Project Structure

```text
.
├── agents/                 # 10 incremental scripts (01 to 10)
│ ├── 01-agent_loop.py
│ ├── ...
│ └── 10-agent_loop.py      # Final complete version (full-featured Harness)
├── design/                 # 10 chapter design docs
├── skills/                 # Optional skills directory (SKILL.md)
├── ready-to-go-withUI/     # 🎯 Anticipated good practice 1: UI example (WIP)
│ ├── agent_loop.py         # Adapted for hot‑reload, editable tool matrix
│ └── web_ui.py             # Conversation UI + DIY tool panel
├── .env.example
├── requirements.txt
├── README_CN.md
└── README_EN.md
```

## 🚀 Quick Start

### 1. Requirements

- Python 3.10+
- Git (required for worktree in Chapter 10)
- OpenAI API-compatible model service (GLM / DeepSeek / Qwen, etc.)

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
# Edit .env and fill in LLM_API_BASE, LLM_API_KEY, LLM_MODEL
```

Alternatively, set the three variables directly in your environment — simple and easy.

### 4. Run a script

```bash
python agents/10-agent_loop.py   # Run the final complete version
```

Then enter natural language tasks at the prompt, and the Agent will automatically call the appropriate tools.

## ✨ Anticipated Good Practices (To Be Continuously Improved)

### Practice I: UI Version (DIY Tool Panel)

Located in the `ready-to-go-withUI/` folder.  
Run `web_ui.py` to open a web chat interface. Currently, you can chat with the Agent and use the tools developed in steps 01-10.

*In the future*: you can describe and customize the tools you want using natural language, either in the interface or in a specific area. The Agent will:

- Complete and implement the functionality
- Automatically verify correctness
- Insert the functionality as a **personal specific tool** into the Harness tool matrix and enable it immediately

> Example: *"Create a tool that can query current weather"* → Agent automatically writes the function, registers it in the tool matrix, and hot-reloads it.

The basic concept is already implemented but still needs further refinement.

### Practice II: Plugin Marketplace Version

**Under planning**: support remote acquisition or upload of personal tools via a plugin marketplace.

- General Harness as the base
- Plugin packages follow standard interfaces (function signature, schema definition)
- One-click install / publish to private or public marketplaces

Let Personal Harness become a **growable agent operating system**.

## 🧰 Current Available Tools (Final version: `10-agent_loop.py`)

| Category | Tool Name | Description |
|:---|:---|:---|
| Basic | `read_file`, `write_file`, `edit_file`, `bash` | Sandboxed file and command operations |
| Planning | `todo` | Internal todo list |
| Subagent | `task` | One-off isolated subtask |
| Skill | `load_skill` | Load skills from `skills/` on demand |
| Compression | `compact` | Manually trigger context compression |
| Background | `background_run`, `check_background` | Run long-running commands in parallel |
| Team | `spawn_teammate`, `list_teammates`, `send_message`, `read_inbox`, `broadcast` | Persistent teammates + mailbox communication |
| Protocol | `shutdown_request`, `plan_approval` | Teammate shutdown and plan approval |
| Task Isolation | `task_create`, `task_list`, `task_get`, `task_update`, `task_bind_worktree` | Task board management |
| Worktree | `worktree_create`, `worktree_list`, `worktree_run`, … | Git worktree lifecycle |

## 📌 Notes

- The final version `10-agent_loop.py` requires the current directory to be a **Git repository**, otherwise worktree tools will not work.
- Teammate threads are based on `threading` — lightweight with no extra dependencies.
- All runtime data is stored in `.transcripts/` (conversation compression), `.tasks/` (task board), `.worktrees/` (worktrees), `.team/` (team configuration) , and `.tools/` (custom personal tools), all can be safely deleted.
- Environment variables must be correctly configured.

---

## 📄 License

MIT License — free to use, modify, and distribute.

---

## 🙏 Acknowledgements

- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) for providing excellent design ideas, implementation examples, and iterative teaching methods.
- GLM / DeepSeek / Qwen and other open-source model communities.
