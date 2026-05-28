# Personal Harness

**个人智能体基础设施框架** - 一个支持项目管理、意图追踪、团队协作的 AI Agent 运行底座。

## 核心特性

- 🎯 **意图管理** - 确保 LLM 准确理解并遵循用户的最终目的
- 📁 **项目隔离** - 每个项目独立的工作目录、历史记录和任务列表
- 🔄 **智能工作流** - 元操作总管家驱动的流程化执行
- 🌳 **Worktree 支持** - Git worktree 任务隔离，支持代码合并与保留
- 👥 **团队协作** - 多 Agent 协作，消息互通
- 🛠️ **工具矩阵** - 40+ 工具，分层权限管理

## 快速开始

### 环境要求

- Python 3.10+
- OpenAI API 兼容的模型服务

### 安装

```bash
pip install nicegui openai prompt-toolkit pyyaml requests httpx python-dotenv
```

### 配置

```bash
# 必需
export LLM_API_BASE="your_api_endpoint"
export LLM_API_KEY="your_api_key"
export LLM_MODEL="your_model_name"
```

### 运行

```bash
# Web UI 模式（推荐）
python ready-to-use-withUI/web_ui.py
# 访问 http://localhost:8080

# CLI 模式
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
│   └── intent.json       # 意图记录
└── .worktrees/          # 工作树目录
```

### 2. 意图管理

在执行任何操作前，系统会引导 LLM 明确用户的最终目的：

| 工具 | 用途 |
|------|------|
| `register_intent` | 注册用户主要目标和约束条件 |
| `clarify_intent` | 当不确定时请求用户澄清 |
| `verify_action` | 验证操作是否符合用户初衷 |
| `track_decision` | 记录重要设计决策 |

### 3. Worktree 文件管理

从工作树获取生成的代码：

| 工具 | 用途 |
|------|------|
| `worktree_list_files` | 列出工作树中的文件 |
| `worktree_read_file` | 读取工作树中的文件内容 |
| `worktree_copy_files` | 复制文件到主项目目录 |
| `worktree_sync` | 合并代码到主分支 |

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
       │ meta_step 推进到下一阶段
       ▼
  Process Layer（流程层）
       │
       ▼
  Tool Matrix Layer（工具矩阵层）
       │
       │ 文件 | 任务 | 团队 | 工作流 | 意图
       │ 40+ 工具，分层权限管理
       ▼
```

## 项目结构

```
.
├── ready-to-use-withUI/
│   ├── agent_loop.py        # 核心 Agent 循环
│   ├── meta_dispatcher.py   # 元操作总管家
│   ├── tool_matrix.py       # 工具矩阵
│   ├── process_definition.py# 流程定义
│   ├── intent_tools.py      # 意图管理工具
│   ├── project_manager.py   # 项目管理器
│   └── web_ui.py            # Web 界面
├── projects.json            # 项目注册表
└── README.md
```
> `design/` 与 `agents/` 两个目录，是搭建`agent_loop`主智能体循环的学习路径和过程，详见 `design/` 中需求所述，每章节均可独立运行对应的智能体并作验证。

## 许可证

MIT License