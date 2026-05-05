# 🤖 Personal Harness — 多智能体自主协作框架

本项目参照 [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 设计，实现了一个**增量式**的个人开发工具链。通过 10 个循序渐进的 Python 脚本，逐步引入了上下文压缩、后台任务、队友团队、自主智能体以及基于 Git Worktree 的任务隔离等机制。

> 每一章对应的 `.py` 文件均可直接运行，并附带一组验证问题，帮助理解每个新特性的行为。

---

## ✨ 特性概览

- ✅ **核心循环** – 纯工具驱动的 Agent Loop，LLM 自主决定何时退出。
- 🔧 **工具矩阵** – 路径沙箱化、工具与函数映射，安全且易扩展。
- 📋 **ToDo 规划** – 引导 Agent 先规划、再执行，避免遗忘长程目标。
- 🧩 **子代理/任务** – 上下文隔离的子任务，防止主流程被干扰。
- 📚 **渐进式技能** – 按需加载 `skills/` 中的 Markdown 技能（SKILL.md）。
- 🗜️ **三级上下文压缩** – 微压缩（占位符）、自动压缩（摘要）、手动压缩工具。
- ⏱️ **后台任务** – 执行耗时命令时模型继续思考，完成后自动注入结果。
- 👥 **团队协作** – 持久化队友（线程），基于 JSONL 邮箱的异步通信。
- 🧠 **自主智能体** – 队友自动轮询任务看板，认领未分配任务。
- 🔒 **任务隔离** – 每个任务独立 Git worktree，并行工作永不冲突，全生命周期可审计。

---

## 📁 项目结构
.
├── agents/ # 10 章渐进式脚本（01 至 10）
│ ├── 01-agent_loop.py
│ ├── 02-agent_loop.py
│ ├── ...
│ └── 10-agent_loop.py # 最终完整版
├── design/ # 设计文档
│ └── 01-10 完整需求文档.md
├── skills/ # 可选技能目录（SKILL.md）
│ └── example/
│ └── SKILL.md
├── .env.example # 环境变量模板
├── .gitignore
├── requirements.txt
├── LICENSE
└── README.md

---

## 🚀 快速开始

### 1. 环境要求

- Python 3.10+
- Git（**必须**，第 10 章需要 worktree 功能）
- 支持 OpenAI API 格式的模型服务（GLM / DeepSeek / Qwen 等）

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量
复制环境变量模板并填写你的 API 信息：
```bash
cp .env.example .env
```

编辑 .env 文件：
```ini
LLM_API_BASE=https://api.your-service.com/v1
LLM_API_KEY=sk-xxxxxx
LLM_MODEL=your-model-name
```

### 4. 运行某一章脚本
例如运行最终完整版：
```bash
python agents/10-agent_loop.py
```

然后在提示符下输入自然语言命令，Agent 会自动调用相应工具完成任务。

提示：每一章的验证问题（见下文）可以直接复制到交互界面中体验。

## 📖 各章核心机制与验证问题

| 章节 | 文件 | 核心机制 | 验证问题示例 |
|:---:|:---|:---|:---|
| 01 | `01-agent_loop.py` | 核心循环 + 基础文件工具 | `创建一个 hello.py 并打印 "Hello, World!"` |
| 02 | `02-agent_loop.py` | 工具调度矩阵 + 路径沙箱 | `编辑 greet.py 添加文档字符串` |
| 03 | `03-agent_loop.py` | ToDo 规划工具 | `重构 hello.py：类型注解、文档字符串、main 保护` |
| 04 | `04-agent_loop.py` | `task` 子代理（上下文隔离） | `使用子任务查找项目使用的测试框架` |
| 05 | `05-agent_loop.py` | `load_skill` 渐进式技能 | `有哪些可用技能？` → `加载 agent-builder 技能` |
| 06 | `06-agent_loop.py` | 三级上下文压缩 | `逐个读取 agents/ 目录下所有 Python 文件`（观察微压缩替换历史结果） |
| 07 | `07-agent_loop.py` | `background_run` 后台任务 | `在后台运行 "sleep 5 && echo done"，同时创建一个文件` |
| 08 | `08-agent_loop.py` | 持久化队友 + 异步邮箱通信 | `生成队友 alice 和 bob，让 alice 给 bob 发消息` |
| 09 | `09-agent_loop.py` | 自主智能体 + 任务看板轮询 | `在任务板上创建 3 个任务，观察队友自动认领` |
| 10 | `10-agent_loop.py` | Git Worktree 任务隔离 | `为后端认证任务创建工作树，在隔离环境中运行 git status` |

> 完整的验证问题列表请参考 `design/01-10 完整需求文档.md` 中每章的「提供验证问题」小节。


## 🧪 使用示例（第 10 章完整版）

以下对话可在 `10-agent_loop.py` 中直接尝试：

**用户**  
> 为后端认证和前端登录页面创建任务，然后列出所有任务。

**Agent 行为**  
- 调用 `task_create` 生成两个任务文件（`.tasks/task_1.json` 等）  
- 调用 `task_list` 展示任务列表  

**用户**  
> 为任务 1 创建工作树 "auth-refactor"，并将任务 2 绑定到 "ui-login"。

**Agent 行为**  
- 执行 `worktree_create name=auth-refactor task_id=1`（自动创建 Git 分支并绑定）  
- 执行 `worktree_create name=ui-login task_id=2`  
- 任务状态自动从 `pending` 变为 `in_progress`  

**用户**  
> 在工作树 "auth-refactor" 中运行 `git status --short`。

**Agent 行为**  
- 调用 `worktree_run name=auth-refactor command="git status --short"`  
- 显示隔离目录下的 git 状态  

## 🛠️ 可用工具一览（最终版）

| 分类 | 工具名 | 说明 |
|:---|:---|:---|
| 基础 | `read_file`, `write_file`, `edit_file`, `bash` | 文件与命令操作（沙箱化） |
| 规划 | `todo` | 创建/更新内部待办列表 |
| 子代理 | `task` | 一次性隔离子任务 |
| 技能 | `load_skill` | 按需加载 `skills/` 下的技能 |
| 压缩 | `compact` | 手动触发上下文压缩 |
| 后台 | `background_run`, `check_background` | 并行运行耗时命令 |
| 团队 | `spawn_teammate`, `list_teammates`, `send_message`, `read_inbox`, `broadcast` | 持久化队友与邮箱通信 |
| 协议 | `shutdown_request`, `plan_approval` | 队友关机与计划审批 |
| 任务隔离 | `task_create`, `task_list`, `task_get`, `task_update`, `task_bind_worktree` | 持久任务板管理 |
| 工作树 | `worktree_create`, `worktree_list`, `worktree_status`, `worktree_run`, `worktree_keep`, `worktree_remove`, `worktree_events` | Git worktree 生命周期管理，支持任务绑定 |

## 📌 注意事项

- 第 10 章要求工作区是一个 **Git 仓库**，否则 worktree 相关工具会提示不可用。
- 队友线程的后台运行依赖于 Python 的 `threading`，轻量且无额外依赖。
- 所有压缩的对话转录保存在 `.transcripts/`，任务板在 `.tasks/`，工作树在 `.worktrees/`，团队配置在 `.team/`，均可安全删除（不影响工作区代码）。
- 环境变量需正确配置，默认使用 OpenAI 兼容的 API。