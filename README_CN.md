# Personal Harness

**个人智能体基础设施框架**

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
- ...

你要做的，就是不断完善这个底座，不断添加工具，让智能体不断成长、不断进化。

## 核心概念

### Harness（智能体框架）

Agent 运行除了底层依赖（LLM API、计算环境），还需要一层**框架**来连接 Agent 与底层依赖。这层框架称为 **Harness**。

- **通用层工具**：跨平台通用的核心能力，如文件读写、上下文压缩、技能加载、任务发布、团队协作等
- **特定应用层工具**：开发者按需挂载的个人工具或插件

> 本项目的核心LLM循环 agent_loop.py ，是依据 “design/01-10 完整需求文档.md” 一步一步开发、扩展而来，“agents/”中的每一个 agent_loop.py ，与 “design/01-10 完整需求文档.md” 中的对应章节严格对应，可以逐个章节运行对应的 agent_loop.py。

> 同时本项目贯彻前述“一切均可工具化”的理念，在“10-agent_loop.py”基础上再次扩展工具集，新增“流程工具”这种元操作工具集，并重新进行层级分划，最终形成下述架构形态。

### 工具矩阵架构

本项目采用**分层工具矩阵架构**，以元操作总管家为核心，通过流程驱动的方式管理工作流。

```
用户层（唯一入口）
       │
       ▼
  meta_dispatch（总管家）
       │
       │ 识别范式，创建 session
       │ 返回第一阶段指令
       ▼
  Workflow Execution
       │
       │ LLM 调用具体工具
       │ meta_step 推进到下一阶段
       ▼
  Process Layer（流程层）
       │
       │ CODE_DEV | TEST_EVAL | FEATURE_DESIGN
       │ ENGINEERING | DOC_WRITING | GENERAL
       ▼
  Tool Matrix Layer（工具矩阵层）
       │
       │ 文件工具 | 任务工具 | 团队工具 | 工作流工具
       │ 30+ 工具，用户不可见，仅被流程调用
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
│   ├── process_definition.py   # 流程定义
│   ├── tool_matrix.py          # 工具矩阵
│   └── web_ui.py               # Web 界面
├── design/                     # 设计文档
│   ├── meta_operation_architecture.md
│   ├── meta_operation_design.md
│   ├── tool_matrix_layer_architecture.md
│   └── tool_matrix_layer_design.md
├── AGENTS.md                   # Agent 配置说明
├── NETWORK_GUIDE.md            # 网络配置指南
├── README.md                   # 项目入口
├── README_CN.md                # 中文说明
└── README_EN.md                # English README
```

## 快速开始

### 环境要求

- Python 3.10+
- 支持 OpenAI API 格式的模型服务

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

**CLI 模式：**

```bash
python ready-to-use-withUI/agent_loop.py
```

**Web UI 模式（推荐）：**

```bash
python ready-to-use-withUI/web_ui.py
# 访问 http://localhost:8080
```

## Web UI 功能

- 实时流式输出
- 工具调用可视化
- 深色模式切换
- 会话持久化
- 工具 DIY 区域：启用/禁用内置工具，创建自定义工具
- 队友活动面板：多 Agent 协作消息展示

## 流程类型

| 流程 | 阶段 | 适用场景 |
|------|------|----------|
| code_development | ARCH → REQ → DESIGN → EXEC → VERIFY → DONE | 代码开发 |
| test_evaluation | PLAN → DESIGN → EXEC → REPORT → DONE | 测试评估 |
| feature_design | ANALYZE → DESIGN → REVIEW → DONE | 功能设计 |
| engineering | CONFIG → DEPLOY → VERIFY → DONE | 工程部署 |
| documentation | PLAN → WRITE → REVIEW → DONE | 文档编写 |
| general_qa | UNDERSTAND → ANSWER → DONE | 通用问答 |

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
| 工作树 | `worktree_create`, `worktree_run` | Git worktree 管理 |

## 权限矩阵

不同流程拥有不同的工具访问权限：

| 流程 | 文件工具 | 任务工具 | 团队工具 | 工作流工具 |
|------|:--------:|:--------:|:--------:|:----------:|
| CODE_DEV | ✅ | ✅ | ✅ | ✅ |
| TEST_EVAL | ✅ | ✅ | ❌ | ❌ |
| FEATURE_DESIGN | ✅ | ❌ | ❌ | ❌ |
| ENGINEERING | ✅ | ✅ | ✅ | ❌ |
| DOC_WRITING | ✅ | ❌ | ❌ | ❌ |
| GENERAL | ✅ | ❌ | ❌ | ❌ |

## 运行时数据

所有运行时数据存储在以下目录，可安全删除：

- `.transcripts/` - 对话压缩记录
- `.tasks/` - 任务板数据
- `.worktrees/` - 工作树状态
- `.team/` - 团队配置
- `.tools/` - 自定义工具

## 网络配置

如遇网络超时，请参考 `NETWORK_GUIDE.md` 配置代理或调整超时设置。

## 许可证

MIT License

## 致谢

- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 提供优秀的设计思路
- GLM / DeepSeek / Qwen 等开源模型社区
