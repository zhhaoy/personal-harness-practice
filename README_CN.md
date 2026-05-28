# Personal Harness

**个人智能体基础设施框架** - 一个支持项目管理、意图追踪、团队协作的 AI Agent 运行底座。

## 核心特性

- 🎯 **意图管理** - 确保 LLM 准确理解并遵循用户的最终目的
- 📁 **项目隔离** - 每个项目独立的工作目录、历史记录和任务列表
- 🔄 **智能工作流** - 元操作总管家驱动的流程化执行
- 🌳 **Worktree 支持** - Git worktree 任务隔离，支持代码合并与保留
- 👥 **团队协作** - 多 Agent 协作，消息互通
- 🛠️ **工具矩阵** - 40+ 工具，分层权限管理

## 简介

大模型唯一的「手脚」，就是工具。

每个用户都可以搭建属于自己的「智能体运行底座」，这个底座的最小构建单元就是工具。任何需求、任何规则都可以被工具化：

- 文件读写
- 网络访问
- 技能加载
- 记忆存储
- 上下文隔离
- 团队协作
- 任务隔离
- 意图管理
- 项目管理
- ...

你要做的，就是不断完善这个底座，不断添加工具，让智能体不断成长、不断进化。

## 快速开始

### 环境要求

- Python 3.10+
- OpenAI API 兼容的模型服务

### 安装依赖

```bash
pip install nicegui openai prompt-toolkit pyyaml requests httpx python-dotenv
```

### 配置环境变量

```bash
# 必需
export LLM_API_BASE="your_api_endpoint"
export LLM_API_KEY="your_api_key"
export LLM_MODEL="your_model_name"
```

### 运行

**Web UI 模式（推荐）：**

```bash
python ready-to-use-withUI/web_ui.py
# 访问 http://localhost:8080
```

**CLI 模式：**

```bash
python ready-to-use-withUI/agent_loop.py
```

## 主要功能

### 1. 项目管理

每个项目拥有独立的工作空间，切换项目时自动加载对应的历史记录和任务列表。

```
项目目录/
├── .pdm/                 # 项目专属数据
│   ├── chat_history.json # 对话历史
│   ├── tool_calls.json   # 工具调用记录
│   ├── todo.json         # 待办事项
│   ├── intent.json       # 意图记录
│   └── decisions.json    # 决策记录
└── .worktrees/          # 工作树目录
```

**Web UI 操作**：
- 点击顶部项目名称打开项目管理对话框
- 选择现有项目或创建新项目
- 项目切换后自动加载历史记录

### 2. 意图管理

在执行任何设计和操作前，系统会引导 LLM 明确用户的最终目的：

| 工具 | 用途 |
|------|------|
| `register_intent` | 注册用户主要目标、次要目标和约束条件 |
| `clarify_intent` | 当不确定用户意图时，请求用户澄清 |
| `verify_action` | 验证操作是否符合用户初衷，检测意图偏移 |
| `track_decision` | 记录重要设计决策，便于回溯 |
| `get_intent_status` | 查询当前意图状态 |

**工作流程**：
1. `meta_dispatch` 识别范式，低置信度时进入意图澄清阶段
2. LLM 调用 `register_intent` 注册用户意图
3. 执行关键操作前调用 `verify_action` 验证
4. 做出重要决策时调用 `track_decision` 记录

### 3. Worktree 管理

工作树与项目绑定，支持 Git Worktree 和 Fallback 两种模式。

**文件操作工具**：

| 工具 | 用途 |
|------|------|
| `worktree_list_files` | 列出工作树中的文件 |
| `worktree_read_file` | 读取工作树中的文件内容 |
| `worktree_copy_files` | 复制文件到主项目目录 |
| `worktree_sync` | 合并代码到主分支（不删除工作树） |

**代码保留方式**：

```python
# 推荐：合并后删除
worktree_remove(name="xxx", merge_to_main=True)

# 中途同步
worktree_sync(name="xxx")

# 仅删除工作树，保留分支
worktree_remove(name="xxx")
```

### 4. 流程类型

| 流程 | 阶段 | 适用场景 |
|------|------|----------|
| code_development | ARCH → REQ → DESIGN → EXEC → VERIFY → DONE | 代码开发 |
| test_evaluation | PLAN → DESIGN → EXEC → REPORT → DONE | 测试评估 |
| feature_design | ANALYZE → DESIGN → REVIEW → DONE | 功能设计 |
| engineering | CONFIG → DEPLOY → VERIFY → DONE | 工程部署 |
| documentation | PLAN → WRITE → REVIEW → DONE | 文档编写 |
| general_qa | UNDERSTAND → ANSWER → DONE | 通用问答 |

## 架构

```
用户层（唯一入口）
       │
       ▼
  meta_dispatch（总管家）
       │
       │ 识别范式，创建 session
       │ 意图澄清（低置信度时）
       │ 返回第一阶段指令
       ▼
  Workflow Execution
       │
       │ LLM 调用具体工具
       │ 意图验证（关键操作时）
       │ meta_step 推进到下一阶段
       ▼
  Process Layer（流程层）
       │
       │ CODE_DEV | TEST_EVAL | FEATURE_DESIGN
       │ ENGINEERING | DOC_WRITING | GENERAL
       ▼
  Tool Matrix Layer（工具矩阵层）
       │
       │ 文件 | 任务 | 团队 | 工作流 | 意图
       │ 40+ 工具，分层权限管理
       ▼
```

### 用户可见工具

用户只需操作两个工具：

| 工具 | 用途 |
|------|------|
| `meta_dispatch` | 入口 - 启动工作流，返回第一阶段指令 |
| `meta_step` | 推进 - 完成当前任务后进入下一阶段 |

## 项目结构

```
.
├── ready-to-use-withUI/        # 源代码
│   ├── agent_loop.py           # 核心 Agent 循环
│   ├── meta_dispatcher.py      # 元操作总管家
│   ├── tool_matrix.py          # 工具矩阵
│   ├── process_definition.py   # 流程定义
│   ├── intent_tools.py         # 意图管理工具
│   ├── project_manager.py      # 项目管理器
│   └── web_ui.py               # Web 界面
├── projects.json               # 项目注册表
├── AGENTS.md                   # Agent 配置说明
├── NETWORK_GUIDE.md            # 网络配置指南
├── README.md                   # 项目入口
├── README_CN.md                # 中文说明
└── README_EN.md                # English README
```
> `design/` 与 `agents/` 两个目录，是搭建`agent_loop`主智能体循环的学习路径和过程，详见 `design/` 中需求所述，每章节均可独立运行对应的智能体并作验证。

## 工具一览

| 分类 | 工具 | 说明 |
|------|------|------|
| 基础 | `read_file`, `write_file`, `edit_file`, `bash` | 文件与命令操作 |
| 规划 | `todo` | 待办列表管理 |
| 子代理 | `task` | 上下文隔离的子任务 |
| 技能 | `load_skill` | 按需加载技能 |
| 压缩 | `compact` | 上下文压缩 |
| 后台 | `background_run`, `check_background` | 并行任务执行 |
| 团队 | `spawn_teammate`, `list_teammates`, `send_message`, `read_inbox` | 多 Agent 协作 |
| 任务 | `task_create`, `task_list`, `task_update` | 任务板管理 |
| 工作树 | `worktree_create`, `worktree_run`, `worktree_list_files`, `worktree_copy_files` | Git worktree 管理 |
| 意图 | `register_intent`, `clarify_intent`, `verify_action`, `track_decision` | 意图管理 |

## 权限矩阵

不同流程拥有不同的工具访问权限：

| 流程 | 文件 | 任务 | 团队 | 工作流 | 意图 |
|------|:----:|:----:|:----:|:------:|:----:|
| CODE_DEV | ✅ | ✅ | ✅ | ✅ | ✅ |
| TEST_EVAL | ✅ | ✅ | ❌ | ❌ | ✅ |
| FEATURE_DESIGN | ✅ | ❌ | ❌ | ❌ | ✅ |
| ENGINEERING | ✅ | ✅ | ✅ | ❌ | ✅ |
| DOC_WRITING | ✅ | ❌ | ❌ | ❌ | ✅ |
| GENERAL | ✅ | ❌ | ❌ | ❌ | ✅ |

## 运行时数据

所有运行时数据存储在以下目录，可安全删除：

- `<project>/.pdm/` - 项目专属数据（对话、任务、意图）
- `<project>/.worktrees/` - 工作树状态
- `.tasks/` - 任务板数据
- `.team/` - 团队配置
- `.tools/` - 自定义工具
- `.transcripts/` - 对话压缩记录

## 网络配置

如遇网络超时，请参考 `NETWORK_GUIDE.md` 配置代理或调整超时设置。

## 许可证

MIT License

## 致谢

- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 提供优秀的设计思路
- GLM / DeepSeek / Qwen 等开源模型社区
