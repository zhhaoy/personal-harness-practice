# 工具矩阵分层架构 - 需求设计文档

**版本**: v2.0  
**日期**: 2025-01-23  
**作者**: Personal Harness Team

---

## 1. 背景与问题

### 1.1 当前架构问题

**问题1: 工具调用混乱**
- 用户直接访问所有工具（30+工具），无统一入口
- LLM 随意调用工具，缺乏章程和流程
- 同一任务可能有多种工具调用路径，效率低下

**问题2: 元操作地位不明确**
- 元操作（meta_dispatch）只是众多工具之一
- 用户可能绕开元操作直接调用底层工具
- 元操作的价值未充分发挥

**问题3: 流程不可控**
- 没有标准化的任务执行流程
- 工具调用顺序随机，难以追踪和优化
- 缺乏"章程"概念

### 1.2 目标

将 **meta_dispatch 提升为总管家**，实现：

1. **统一入口**：用户只与 meta_dispatch 交互
2. **分层管理**：工具矩阵分为用户层和底层工具层
3. **流程驱动**：每套流程有明确章程、方法、机制
4. **自进化**：流程可学习、改进、热插拔

---

## 2. 核心概念定义

### 2.1 工具矩阵分层

```
┌─────────────────────────────────────────────────────────────┐
│                     用户层 (User Layer)                      │
│                                                             │
│                    meta_dispatch (总管家)                   │
│                                                             │
│   • 用户唯一接口                                              │
│   • 范式识别 + 流程调度                                       │
│   • 流程编排 + 执行控制                                       │
│                                                             │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           │ 调度
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                   流程层 (Process Layer)                     │
│                                                             │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐          │
│  │CODE_DEV │ │FEATURE  │ │TEST_EVAL│ │GENERAL  │  ...     │
│  │ 流程    │ │DESIGN   │ │ 流程    │ │ 流程    │          │
│  │         │ │ 流程    │ │         │ │         │          │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘          │
│       │           │           │           │                │
│  章程 • 方法 • 机制 • 状态机                                 │
│                                                             │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           │ 调用
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                 工具矩阵层 (Tool Matrix Layer)               │
│                                                             │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐          │
│  │ 文件工具    │ │ 任务工具    │ │ 团队工具    │          │
│  │ bash        │ │ task_create │ │ spawn_      │          │
│  │ read_file   │ │ worktree_*  │ │ teammate    │          │
│  │ write_file  │ │ task_list   │ │ send_msg    │          │
│  └─────────────┘ └─────────────┘ └─────────────┘          │
│                                                             │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐          │
│  │ 工作流工具  │ │ 上下文工具  │ │ 后台工具    │          │
│  │ workflow_*  │ │ compact     │ │ background  │          │
│  │             │ │ load_skill  │ │             │          │
│  └─────────────┘ └─────────────┘ └─────────────┘          │
│                                                             │
│  （共30+工具，对用户不可见，仅被流程调用）                    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 总管家 (Meta Dispatcher)

**定义**: meta_dispatch 是用户与系统的唯一交互入口，负责：
1. 范式识别
2. 流程匹配
3. 流程编排
4. 执行控制
5. 结果聚合

**特征**:
- 用户不可绕过
- 拥有所有底层工具的调用权限
- 内置多套标准流程
- 支持流程动态扩展

### 2.3 流程 (Process)

**定义**: 流程是一套有明确目标、章程、方法、机制的执行规范。

**流程组成**:
```yaml
流程定义:
  名称: code_development
  范式: CODE_DEV
  
  章程:
    目标: 完成代码开发任务
    输入: 用户需求描述
    输出: 可运行的代码 + 测试 + 文档
    
  方法:
    阶段:
      - ARCH: 架构设计
      - REQ: 需求分析
      - DESIGN: 详细设计
      - EXEC: 执行开发
      - VERIFY: 验证测试
      - DONE: 完成
    
    每阶段工具调用序列:
      ARCH: [read_file, bash, workflow_step]
      REQ: [read_file, write_file, todo]
      DESIGN: [write_file, workflow_step]
      EXEC: [bash, write_file, edit_file, worktree_*]
      VERIFY: [bash, background_run]
      
  机制:
    状态机:
      ARCH → REQ → DESIGN → EXEC → VERIFY → DONE
    
    质量门禁:
      DESIGN阶段需用户固化确认
      VERIFY阶段需测试通过
      
    异常处理:
      VERIFY失败 → REFINE → VERIFY
      
  热插拔:
    enabled: true
    version: "2.0.0"
    last_updated: 2025-01-23
```

### 2.4 章程、方法、机制

| 概念 | 定义 | 示例 |
|------|------|------|
| **章程** | 流程的目标、输入输出、约束 | "完成代码开发，输出代码+测试+文档" |
| **方法** | 执行步骤、工具调用序列 | "ARCH阶段：read_file → bash → workflow_step" |
| **机制** | 状态转移、质量门禁、异常处理 | "VERIFY失败则REFINE" |

---

## 3. 系统架构设计

### 3.1 分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         用户交互层                               │
│                                                                 │
│   Web UI / CLI ──────> meta_dispatch (总管家)                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                        调度核心层                                │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              MetaDispatcherCore                          │   │
│  │                                                          │   │
│  │  • ParadigmRecognizer      范式识别器                    │   │
│  │  • ProcessRegistry          流程注册表                    │   │
│  │  • ProcessOrchestrator      流程编排器                    │   │
│  │  • ExecutionContext         执行上下文                    │   │
│  │  • ResultAggregator         结果聚合器                    │   │
│  │                                                          │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                         流程层                                  │
│                                                                 │
│  ProcessDefinition (流程定义)                                   │
│  ├──章程 (Charter): 目标、输入输出、约束                        │
│  ├──方法 (Method): 阶段、工具序列                               │
│  └──机制 (Mechanism): 状态机、门禁、异常                        │
│                                                                 │
│  内置流程:                                                       │
│  ├── CodeDevelopmentProcess    (代码开发)                       │
│  ├── FeatureDesignProcess      (功能设计)                       │
│  ├── TestEvaluationProcess     (测试评估)                       │
│  ├── EngineeringProcess        (工程实践)                       │
│  ├── DocumentationProcess      (文档编写)                       │
│  └── GeneralQAProcess          (通用问答)                       │
│                                                                 │
│  动态流程:                                                       │
│  └── (可通过LLM动态生成)                                        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                       工具矩阵层                                │
│                                                                 │
│  ToolMatrix (工具矩阵)                                          │
│  ├── 文件工具组: bash, read_file, write_file, edit_file        │
│  ├── 任务工具组: task_*, worktree_*                             │
│  ├── 团队工具组: spawn_teammate, send_message, ...              │
│  ├── 工作流工具组: workflow_*                                   │
│  ├── 上下文工具组: compact, load_skill, todo                    │
│  └── 后台工具组: background_run, check_background              │
│                                                                 │
│  特性:                                                          │
│  • 对用户不可见 (用户层屏蔽)                                    │
│  • 仅被流程调用                                                 │
│  • 支持权限控制 (哪些流程可调用哪些工具)                        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 核心组件设计

#### 3.2.1 MetaDispatcherCore (总管家核心)

```python
class MetaDispatcherCore:
    """总管家核心：用户唯一入口"""
    
    def __init__(self):
        self.recognizer = ParadigmRecognizer()      # 范式识别器
        self.process_registry = ProcessRegistry()   # 流程注册表
        self.orchestrator = ProcessOrchestrator()   # 流程编排器
        self.tool_matrix = ToolMatrix()             # 工具矩阵
        self.context = ExecutionContext()           # 执行上下文
    
    def dispatch(self, user_query: str) -> DispatchResult:
        """
        主调度入口（用户唯一可调用）
        
        流程：
        1. 识别范式
        2. 匹配流程
        3. 编排执行
        4. 聚合结果
        """
        pass
    
    def call_tool(self, tool_name: str, params: dict) -> ToolResult:
        """
        调用底层工具（仅被流程内部调用）
        用户不可直接调用
        """
        pass
```

#### 3.2.2 ProcessDefinition (流程定义)

```python
class ProcessDefinition:
    """流程定义：章程 + 方法 + 机制"""
    
    def __init__(self, config: dict):
        # 章程
        self.name = config["name"]
        self.paradigm = config["paradigm"]
        self.objective = config["charter"]["objective"]
        self.inputs = config["charter"]["inputs"]
        self.outputs = config["charter"]["outputs"]
        self.constraints = config["charter"]["constraints"]
        
        # 方法
        self.phases = config["method"]["phases"]
        self.tool_sequences = config["method"]["tool_sequences"]
        
        # 机制
        self.state_machine = config["mechanism"]["state_machine"]
        self.quality_gates = config["mechanism"]["quality_gates"]
        self.exception_handlers = config["mechanism"]["exception_handlers"]
    
    def get_tools_for_phase(self, phase: str) -> List[str]:
        """获取某阶段可用的工具列表"""
        return self.tool_sequences.get(phase, [])
    
    def transition(self, current_phase: str, event: str) -> str:
        """状态转移"""
        pass
    
    def check_quality_gate(self, phase: str, artifact: Any) -> bool:
        """检查质量门禁"""
        pass
```

#### 3.2.3 ToolMatrix (工具矩阵)

```python
class ToolMatrix:
    """工具矩阵：底层工具管理"""
    
    def __init__(self, tool_registry):
        self.registry = tool_registry
        self.tool_groups = {
            "file": ["bash", "read_file", "write_file", "edit_file"],
            "task": ["task_create", "task_list", "task_update", 
                    "task_bind_worktree", "worktree_*"],
            "team": ["spawn_teammate", "activate_teammate", "list_teammates",
                    "send_message", "read_inbox", "broadcast"],
            "workflow": ["workflow_start", "workflow_step", "workflow_status"],
            "context": ["compact", "load_skill", "todo"],
            "background": ["background_run", "check_background"],
        }
        
        # 权限控制：哪些流程可调用哪些工具组
        self.permissions = {
            "CODE_DEV": ["file", "task", "team", "workflow", "context"],
            "TEST_EVAL": ["file", "task", "background"],
            "GENERAL": ["file", "context"],
            # ...
        }
    
    def call(self, tool_name: str, params: dict, process: str) -> ToolResult:
        """
        调用工具（带权限检查）
        """
        if not self._check_permission(tool_name, process):
            raise PermissionError(f"流程 {process} 无权调用工具 {tool_name}")
        
        return self.registry.get_handler(tool_name)(**params)
    
    def list_tools_for_process(self, process: str) -> List[str]:
        """列出某流程可用的工具"""
        pass
```

---

## 4. 流程设计详解

### 4.1 CodeDevelopmentProcess (代码开发流程)

```yaml
name: code_development
paradigm: CODE_DEV

charter:
  objective: "完成代码开发任务，输出高质量代码、测试、文档"
  inputs:
    - 用户需求描述
    - 可选：现有代码库
  outputs:
    - 可运行的代码
    - 测试用例
    - 技术文档
  constraints:
    - 遵循编码规范
    - 测试覆盖率 >= 80%
    - 文档完整

method:
  phases:
    ARCH:
      description: "架构设计"
      tools: [read_file, bash, workflow_step]
      outputs: [架构文档]
    REQ:
      description: "需求分析"
      tools: [read_file, write_file, todo]
      outputs: [需求文档]
    DESIGN:
      description: "详细设计"
      tools: [write_file, workflow_step]
      outputs: [设计文档]
    EXEC:
      description: "执行开发"
      tools: [bash, write_file, edit_file, worktree_create, worktree_run]
      outputs: [代码]
    VERIFY:
      description: "验证测试"
      tools: [bash, background_run]
      outputs: [测试报告]
    REFINE:
      description: "修正问题"
      tools: [edit_file, bash]
      outputs: [修正后代码]
    DONE:
      description: "完成"
      tools: []
      outputs: []

mechanism:
  state_machine:
    transitions:
      - {from: ARCH, to: REQ, event: confirm}
      - {from: REQ, to: DESIGN, event: confirm}
      - {from: DESIGN, to: EXEC, event: confirm}  # 用户固化
      - {from: EXEC, to: VERIFY, event: execute_done}
      - {from: VERIFY, to: DONE, event: verify_pass}
      - {from: VERIFY, to: REFINE, event: verify_fail}
      - {from: REFINE, to: VERIFY, event: refine_done}
  
  quality_gates:
    DESIGN:
      - 用户必须明确"固化"确认
    VERIFY:
      - 测试必须通过
      - 覆盖率 >= 80%
  
  exception_handlers:
    verify_fail:
      action: "进入REFINE阶段"
      max_retries: 3
    tool_error:
      action: "记录错误，尝试替代工具"
```

### 4.2 TestEvaluationProcess (测试评估流程)

```yaml
name: test_evaluation
paradigm: TEST_EVAL

charter:
  objective: "完成测试任务，输出测试报告和覆盖率数据"
  inputs:
    - 测试目标（模块/功能）
    - 测试类型要求
  outputs:
    - 测试用例
    - 测试报告
    - 覆盖率报告

method:
  phases:
    PLAN:
      description: "测试计划"
      tools: [read_file, write_file, todo]
    DESIGN:
      description: "用例设计"
      tools: [write_file]
    EXEC:
      description: "执行测试"
      tools: [bash, background_run]
    REPORT:
      description: "生成报告"
      tools: [write_file]
    DONE:
      description: "完成"

mechanism:
  state_machine:
    transitions:
      - {from: PLAN, to: DESIGN, event: confirm}
      - {from: DESIGN, to: EXEC, event: confirm}
      - {from: EXEC, to: REPORT, event: execute_done}
      - {from: REPORT, to: DONE, event: confirm}
  
  quality_gates:
    EXEC:
      - 所有测试用例执行完毕
```

### 4.3 GeneralQAProcess (通用问答流程)

```yaml
name: general_qa
paradigm: GENERAL

charter:
  objective: "回答用户问题"
  inputs:
    - 用户问题
  outputs:
    - 回答内容

method:
  phases:
    UNDERSTAND:
      description: "理解问题"
      tools: [read_file]  # 可选读取相关文件
    ANSWER:
      description: "生成回答"
      tools: []
    DONE:
      description: "完成"

mechanism:
  state_machine:
    transitions:
      - {from: UNDERSTAND, to: ANSWER, event: understood}
      - {from: ANSWER, to: DONE, event: answered}
```

---

## 5. 工具矩阵分层实现

### 5.1 用户层工具（仅1个）

| 工具名 | 可见性 | 功能 |
|--------|--------|------|
| `meta_dispatch` | 用户可见 | 总管家入口，范式识别+流程调度 |

### 5.2 工具矩阵层工具（30+，用户不可见）

| 工具组 | 工具列表 |
|--------|----------|
| 文件工具 | bash, read_file, write_file, edit_file |
| 任务工具 | task_create, task_list, task_get, task_update, task_bind_worktree |
| 工作树工具 | worktree_create, worktree_list, worktree_status, worktree_run, worktree_keep, worktree_remove, worktree_events |
| 团队工具 | spawn_teammate, activate_teammate, list_teammates, send_message, read_inbox, broadcast, shutdown_request, plan_approval |
| 工作流工具 | workflow_start, workflow_step, workflow_status |
| 上下文工具 | compact, load_skill, todo, task |
| 后台工具 | background_run, check_background |

### 5.3 权限控制矩阵

| 流程 | 文件工具 | 任务工具 | 团队工具 | 工作流工具 | 上下文工具 | 后台工具 |
|------|:--------:|:--------:|:--------:|:----------:|:----------:|:--------:|
| CODE_DEV | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| TEST_EVAL | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ |
| FEATURE_DESIGN | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ |
| ENGINEERING | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| DOC_WRITING | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ |
| GENERAL | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ |

---

## 6. 执行流程

### 6.1 标准执行流程

```
用户输入 Query
      │
      ▼
┌─────────────────────┐
│  meta_dispatch      │ ◄── 用户唯一入口
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 1. 范式识别          │
│    ParadigmRecognizer│
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 2. 流程匹配          │
│    ProcessRegistry   │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 3. 流程编排执行      │
│    ProcessOrchestrator│
│                     │
│    ┌───────────┐   │
│    │ 阶段1     │   │
│    │ 调用工具  │   │
│    └─────┬─────┘   │
│          │         │
│    ┌─────▼─────┐   │
│    │ 阶段2     │   │
│    │ 调用工具  │   │
│    └─────┬─────┘   │
│          │         │
│         ...        │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 4. 结果聚合          │
│    ResultAggregator  │
└──────────┬──────────┘
           │
           ▼
      返回给用户
```

### 6.2 流程内部工具调用

```
流程执行中...
      │
      ▼
┌─────────────────────┐
│ 当前阶段: EXEC      │
│ 可用工具: [bash,    │
│   write_file, ...] │
└──────────┬──────────┘
           │
           ▼
    调用 write_file
           │
           ▼
┌─────────────────────┐
│ ToolMatrix.call()   │
│                     │
│ 1. 权限检查         │
│ 2. 调用底层工具     │
│ 3. 返回结果         │
└──────────┬──────────┘
           │
           ▼
    工具执行结果
           │
           ▼
    继续下一阶段
```

---

## 7. 自进化与热插拔

### 7.1 流程自进化

**机制**：
1. **反馈收集**：用户对流程执行结果的评分和反馈
2. **数据分析**：统计工具调用成功率、阶段耗时等
3. **改进建议**：LLM 分析并提出改进建议
4. **自动更新**：更新流程定义（工具序列、质量门禁等）

```python
class ProcessEvolver:
    """流程自进化器"""
    
    def analyze_feedback(self, process: str, feedbacks: List[Feedback]):
        """分析反馈，生成改进建议"""
        pass
    
    def evolve(self, process: str, improvement: dict):
        """进化流程"""
        pass
```

### 7.2 流程热插拔

**机制**：
1. **流程定义文件**：每个流程是一个独立 YAML/JSON 文件
2. **运行时加载**：启动时扫描 `.processes/` 目录加载流程
3. **动态注册**：新增流程无需重启，调用 `process_registry.reload()`
4. **版本管理**：支持多版本流程共存

```
.processes/
├── code_development.yaml
├── test_evaluation.yaml
├── feature_design.yaml
└── custom_process.yaml  # 用户自定义流程
```

---

## 8. 与现有系统集成

### 8.1 改造点

| 现有组件 | 改造内容 |
|----------|----------|
| `agent_loop.py` | 移除所有工具的直接暴露，仅保留 `meta_dispatch` |
| `ToolRegistry` | 改为 `ToolMatrix`，增加权限控制 |
| `WorkflowManager` | 被 `ProcessOrchestrator` 替代 |
| 系统提示词 | 更新为引导用户使用 `meta_dispatch` |

### 8.2 向后兼容

- **过渡期**：保留所有工具，但标记为"内部工具"
- **用户层**：明确引导用户使用 `meta_dispatch`
- **错误提示**：直接调用工具时提示"请通过 meta_dispatch 调度"

---

## 9. 非功能性需求

### 9.1 性能要求

| 指标 | 目标值 |
|------|--------|
| 范式识别延迟 | < 10ms |
| 流程匹配延迟 | < 5ms |
| 工具调用开销 | < 1ms |
| 流程切换延迟 | < 100ms |

### 9.2 可扩展性

- 支持运行时新增流程
- 支持流程版本升级
- 支持流程自定义（用户可通过文件定义新流程）

### 9.3 可观测性

- 每次流程执行记录完整日志
- 工具调用链可追踪
- 流程执行统计（成功率、耗时等）

---

## 10. 验收标准

### 10.1 功能验收

- [ ] 用户只能看到 `meta_dispatch` 工具
- [ ] `meta_dispatch` 能正确识别范式并匹配流程
- [ ] 流程能按章程、方法、机制执行
- [ ] 工具权限控制生效
- [ ] 流程状态机正确转移
- [ ] 质量门禁正确执行
- [ ] 支持流程热插拔

### 10.2 质量验收

- [ ] 代码覆盖率 >= 70%
- [ ] 无 P0/P1 级别 bug
- [ ] 文档完整
- [ ] 性能达标

---

## 11. 实施计划

| 阶段 | 内容 | 预计工时 |
|------|------|----------|
| Phase 1 | 架构设计文档 | 1h |
| Phase 2 | ToolMatrix 实现 | 2h |
| Phase 3 | ProcessDefinition 实现 | 2h |
| Phase 4 | MetaDispatcherCore 重构 | 3h |
| Phase 5 | 内置流程定义 | 2h |
| Phase 6 | agent_loop.py 改造 | 2h |
| Phase 7 | 测试与验证 | 2h |
| **总计** | | **14h** |

---

**文档结束**
