# 🤖 Personal Harness — 个人智能体自主协作框架

**让每个开发者拥有自己的「智能体运行底座」—— 通用层 + 个人层，热插拔、可扩展、真自主。**

## 📌 概念定义

Agent 运行除了底层依赖（LLM API、计算环境），还需要一层**框架**来连接 Agent 与底层依赖。这层框架称为 **Harness**。

- **通用层框架**  
  包含跨平台通用的核心能力：文件读写、上下文压缩、技能（Skill）加载、任务发布与看板、自主团队协作、Git Worktree 隔离等。

- **特定应用层框架**  
  在通用层之上，开发者按需挂载**个人开发工具或插件**（如内部 API 调用、代码审查钩子、数据库查询等）。

> Personal Harness = 通用 Harness + 你的私人工具箱

## 🧱 结构范式

```text
+
热重载工具/扩展插件矩阵
├── 通用工具（读写、网络、skill、压缩、任务发布、自主团队、工作树…）
└── 个人特定工具（DIY 或从插件市场获取）
```

- **核心循环**：纯工具驱动的 Agent Loop，LLM 自主决定何时退出。  
- **热重载**：新增或修改工具无需重启主程序（通过函数映射动态加载）。  
- **扩展友好**：每个工具都是一个独立函数，按分类注册到工具矩阵。

## 🛤️ 智能体底座实现路径（01→10）

本项目通过 **10 个增量式 Python 脚本**，循序渐进地实现了从最小核心循环到完整 Personal Harness 的全部机制。设计先行，在 `design/` 文件夹，共十章节，每个章节对应一个 `.py` 文件（位于 `agents/` 文件夹），均可独立运行，并配有验证问题。（此处感谢[learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 项目提供优秀的设计思路、实现案例与教学迭代方式）

| 章节 | 核心机制 | 增量点 |
|:---:|:---|:---|
| 01 | 核心循环 + 基础文件工具 | 最小可运行 Agent |
| 02 | 工具调度矩阵 + 路径沙箱 | 安全执行基础操作 |
| 03 | ToDo 规划工具 | 引导 Agent 先规划再行动 |
| 04 | `task` 子代理（上下文隔离） | 隔离子任务不被主流程干扰 |
| 05 | `load_skill` 渐进式技能 | 按需加载 skills/ 中的提示 |
| 06 | 三级上下文压缩 | 微压缩、自动压缩、手动压缩 |
| 07 | `background_run` 后台任务 | 耗时命令并行执行 |
| 08 | 持久化队友 + 异步邮箱通信 | 多 Agent 协作基础 |
| 09 | 自主智能体 + 任务看板轮询 | 队友自动认领任务 |
| 10 | Git Worktree 任务隔离 | 每个任务独立工作树，并行不冲突 |

> 完整验证问题请参考 `design/01-10 完整需求文档.md`

## 📁 项目结构

```text
.
├── agents/                 # 10 章渐进式脚本（01 至 10）
│ ├── 01-agent_loop.py
│ ├── ...
│ └── 10-agent_loop.py      # 最终完整版（全功能 Harness）
├── design/                 # 10 章设计文档
├── skills/                 # 可选技能目录（SKILL.md）
├── ready-to-go-withUI/     # 🎯 预期良好实践一：带 UI 的案例（待完善）
│ ├── agent_loop.py         # = 10-agent_loop.py
│ └── web_ui.py             # 对话界面 + DIY 工具栏（待完善）
├── .env.example
├── requirements.txt
├── README_CN.md
└── README_EN.md
```

## 🚀 快速开始

### 1. 环境要求

- Python 3.10+
- Git（第 10 章需要 worktree 功能）
- 支持 OpenAI API 格式的模型服务（GLM / DeepSeek / Qwen 等）

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入 LLM_API_BASE, LLM_API_KEY, LLM_MODEL
```

或，请直接在环境变量中新增以上三个变量，简单易行。

### 4. 运行某一章脚本

```bash
python agents/10-agent_loop.py   # 运行最终完整版
```

然后在提示符下输入自然语言任务，Agent 会自动调用相应工具。

## ✨ 预期良好实践（待持续完善）

### 实践一：界面 UI 版（DIY 工具栏）

位于 `ready-to-go-withUI/` 文件夹。  
运行 `web_ui.py` 即可打开一个 Web 对话界面，目前能对话 Agent ，使用既有 01-10 过程中所开发工具。

*未来*：你可以在界面中、或界面特定区域，用自然语言描述、定制想要的工具，Agent 会：

- 完善并实现该功能  
- 自动验证功能正确性  
- 将该功能作为**个人特定工具**插入 Harness 的工具矩阵，并立即启用  

> 例如：*“创建一个工具，能够查询当前天气”* → Agent 自动写出函数、注册到工具矩阵、热重载生效。

### 实践二：扩展插件版（插件市场）

**规划中**：支持通过插件市场远程获取或上传个人工具。

- 通用 Harness 作为底座  
- 插件包遵循标准接口（函数签名、schema 定义）  
- 一键安装 / 发布到私有或公共市场

让 Personal Harness 成为一个**可生长的智能体操作系统**。

---

## 🧰 当前可用工具一览（最终版 10-agent_loop.py ）

| 分类 | 工具名 | 说明 |
|:---|:---|:---|
| 基础 | `read_file`, `write_file`, `edit_file`, `bash` | 沙箱化文件与命令 |
| 规划 | `todo` | 内部待办列表 |
| 子代理 | `task` | 一次性隔离子任务 |
| 技能 | `load_skill` | 按需加载 `skills/` 下的技能 |
| 压缩 | `compact` | 手动触发上下文压缩 |
| 后台 | `background_run`, `check_background` | 并行运行耗时命令 |
| 团队 | `spawn_teammate`, `list_teammates`, `send_message`, `read_inbox`, `broadcast` | 持久化队友 + 邮箱通信 |
| 协议 | `shutdown_request`, `plan_approval` | 队友关机与计划审批 |
| 任务隔离 | `task_create`, `task_list`, `task_get`, `task_update`, `task_bind_worktree` | 任务板管理 |
| 工作树 | `worktree_create`, `worktree_list`, `worktree_run`, … | Git worktree 生命周期 |

---

## 📌 注意事项

- 最终版 10-agent_loop.py 要求当前目录是一个 **Git 仓库**，否则 worktree 工具不可用。  
- 队友线程基于 `threading`，轻量无额外依赖。  
- 所有运行时数据保存在 `.transcripts/`（对话压缩）、`.tasks/`（任务板）、`.worktrees/`（工作树）、`.team/`（团队配置），均可安全删除。  
- 环境变量必须正确配置。

---

## 📄 许可证

MIT 许可证，自由使用、修改和分发。

---

## 🙏 致谢

- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 提供优秀的设计思路与教学迭代方式。  
- GLM / DeepSeek / Qwen 等开源模型社区。
