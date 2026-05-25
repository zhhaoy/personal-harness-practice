# 元操作工具组（Meta-Operation Tool Suite）需求设计文档

**版本**: v1.0  
**日期**: 2025-01-23  
**作者**: Personal Harness Team

---

## 1. 背景与问题

### 1.1 当前痛点

1. **工具调用混乱**: LLM在处理不同类型任务时，交叉混合调用各类工具，缺乏清晰的调用章程
2. **缺乏任务类型识别**: 系统无法识别用户query属于哪种范式（开发/设计/测试/工程实践），导致工具选择不合理
3. **工具粒度过细**: 现有工具都是原子操作，缺乏"元操作"概念——即一组有明确目标和流程的工具组合
4. **无法动态扩展**: 当遇到新范式时，系统无法即时生成对应的元操作工具

### 1.2 现有架构分析

| 组件 | 职责 | 局限性 |
|------|------|--------|
| `ToolRegistry` | 管理单个工具的注册/调用 | 无工具分组、无范式识别 |
| `WorkflowManager` | 管理开发类工作流状态 | 仅覆盖开发范式，不可扩展 |
| `agent_loop()` | 主控循环，直接调用工具 | 缺少范式识别和元操作调度层 |

---

## 2. 核心概念定义

### 2.1 元操作（Meta-Operation）

**定义**: 元操作是一组有序工具调用的封装，代表一个完整的、有明确目标的行动范式。

**特征**:
- 封装了工具组合的调用逻辑
- 有明确的输入输出规范
- 可独立执行，也可组合执行
- 支持状态管理和进度追踪

**示例**:
```
元操作: code_development
  ├─ 工具序列: [workflow_start → workflow_step → workflow_step ...]
  ├─ 状态机: ARCH → REQ → DESIGN → CONFIRM → EXEC → VERIFY → DONE
  └─ 适用场景: 需要完整工程流程的代码开发任务
```

### 2.2 行动范式（Action Paradigm）

**定义**: 用户任务的分类模式，决定了应使用哪种元操作工具组。

**初步定义的范式类别**:

| 范式ID | 名称 | 描述 | 对应元操作 |
|--------|------|------|------------|
| `CODE_DEV` | 代码开发类 | 实现具体功能、修复bug、重构代码 | `code_development` |
| `FEATURE_DESIGN` | 功能设计类 | 系统设计、架构设计、接口设计 | `feature_design` |
| `ENGINEERING` | 工程实践类 | CI/CD、部署、监控、性能优化 | `engineering_practice` |
| `TEST_EVAL` | 测试评估类 | 编写测试、覆盖率分析、性能测试 | `test_evaluation` |
| `DOC_WRITING` | 文档编写类 | README、API文档、用户手册 | `documentation` |
| `DATA_ANALYSIS` | 数据分析类 | 数据处理、可视化、报表生成 | `data_analysis` |
| `GENERAL` | 通用问答类 | 简单问答、信息查询 | `general_qa` |

### 2.3 范式识别器（Paradigm Recognizer）

**职责**: 分析用户query，判断最合适的行动范式。

**实现方式**: 
- 基于关键词匹配的快速识别
- 基于LLM的语义理解识别
- 基于历史数据的经验识别

---

## 3. 系统架构设计

### 3.1 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                      用户 Query                               │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              元操作调度器 (MetaOperationDispatcher)           │
│  ┌─────────────────┐  ┌──────────────────┐                 │
│  │  范式识别器      │  │  元操作路由器      │                 │
│  │ ParadigmRecognizer│ │ MetaOpRouter     │                 │
│  └─────────────────┘  └──────────────────┘                 │
└──────────────────────┬──────────────────────────────────────┘
                       │
           ┌───────────┼───────────┬───────────┐
           │           │           │           │
           ▼           ▼           ▼           ▼
    ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
    │元操作:    │ │元操作:    │ │元操作:    │ │元操作:    │
    │code_dev  │ │feature   │ │engineering│ │test_eval │
    │          │ │_design   │ │          │ │          │
    └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
         │            │            │            │
         └────────────┴────────────┴────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │   ToolRegistry   │
                    │  (原子工具层)     │
                    └──────────────────┘
```

### 3.2 核心模块设计

#### 3.2.1 MetaOperationDispatcher（元操作调度器）

**职责**: 
1. 接收用户query
2. 调用范式识别器判断任务类型
3. 根据识别结果路由到对应元操作
4. 管理元操作的执行和状态

**核心方法**:
```python
class MetaOperationDispatcher:
    def dispatch(self, query: str, context: dict) -> MetaOpResult:
        """主调度入口"""
        pass
    
    def recognize_paradigm(self, query: str) -> ParadigmType:
        """范式识别"""
        pass
    
    def route_to_meta_op(self, paradigm: ParadigmType) -> MetaOperation:
        """路由到元操作"""
        pass
    
    def generate_missing_meta_op(self, paradigm: ParadigmType) -> MetaOperation:
        """动态生成缺失的元操作"""
        pass
```

#### 3.2.2 ParadigmRecognizer（范式识别器）

**职责**: 分析query，输出范式类型和置信度

**识别策略**:
1. **关键词匹配** (快速): 检测关键动词、名词
2. **模式匹配**: 检测句式结构
3. **语义理解** (精确): 调用LLM进行意图分析
4. **历史学习**: 基于用户反馈调整识别结果

**核心方法**:
```python
class ParadigmRecognizer:
    def recognize(self, query: str, context: dict) -> RecognitionResult:
        """识别范式，返回 (paradigm, confidence, reasoning)"""
        pass
    
    def fast_match(self, query: str) -> Optional[ParadigmType]:
        """快速关键词匹配"""
        pass
    
    def semantic_recognize(self, query: str) -> RecognitionResult:
        """基于LLM的语义识别"""
        pass
```

#### 3.2.3 MetaOperation（元操作基类）

**职责**: 定义元操作的统一接口和生命周期

```python
class MetaOperation(ABC):
    """元操作基类"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """元操作名称"""
        pass
    
    @property
    @abstractmethod
    def paradigm(self) -> ParadigmType:
        """对应的范式"""
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        """描述"""
        pass
    
    @abstractmethod
    def validate_input(self, query: str, context: dict) -> bool:
        """验证输入是否适用"""
        pass
    
    @abstractmethod
    def execute(self, query: str, context: dict) -> MetaOpResult:
        """执行元操作"""
        pass
    
    @abstractmethod
    def get_progress(self) -> float:
        """获取执行进度 (0.0-1.0)"""
        pass
    
    @abstractmethod
    def can_handover(self) -> bool:
        """是否支持移交到其他元操作"""
        pass
```

#### 3.2.4 MetaOperationRegistry（元操作注册表）

**职责**: 管理所有元操作的注册、查询、动态生成

```python
class MetaOperationRegistry:
    def register(self, meta_op: MetaOperation):
        """注册元操作"""
        pass
    
    def get(self, paradigm: ParadigmType) -> Optional[MetaOperation]:
        """获取元操作"""
        pass
    
    def list_all(self) -> List[MetaOperationInfo]:
        """列出所有元操作"""
        pass
    
    def generate_dynamic(self, paradigm: ParadigmType, query: str) -> MetaOperation:
        """动态生成元操作"""
        pass
```

---

## 4. 工具定义

### 4.1 新增工具列表

| 工具名 | 类型 | 描述 |
|--------|------|------|
| `meta_dispatch` | 总调度 | 范式识别并路由到元操作 |
| `meta_status` | 查询 | 查询当前元操作执行状态 |
| `meta_handover` | 切换 | 将当前任务移交给其他元操作 |
| `meta_feedback` | 反馈 | 用户对元操作结果的反馈 |
| `meta_improve` | 改进 | 根据反馈改进元操作实现 |

### 4.2 工具详细定义

#### 4.2.1 meta_dispatch

```json
{
  "name": "meta_dispatch",
  "description": "分析用户query，识别行动范式，并调度到对应的元操作工具。返回范式类型、元操作名称和初始指令。",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "用户的原始query"
      },
      "context": {
        "type": "object",
        "description": "上下文信息（可选）"
      },
      "force_paradigm": {
        "type": "string",
        "enum": ["CODE_DEV", "FEATURE_DESIGN", "ENGINEERING", "TEST_EVAL", "DOC_WRITING", "DATA_ANALYSIS", "GENERAL"],
        "description": "强制指定范式（可选，用于纠正识别）"
      }
    },
    "required": ["query"]
  }
}
```

**返回值示例**:
```json
{
  "paradigm": "CODE_DEV",
  "confidence": 0.92,
  "meta_operation": "code_development",
  "instruction": "已识别为代码开发类任务，将启动开发工作流。请描述具体需求...",
  "reasoning": "检测到关键词：实现、功能、代码，且任务复杂度较高"
}
```

#### 4.2.2 meta_status

```json
{
  "name": "meta_status",
  "description": "查询当前元操作的执行状态、进度和产出物。",
  "parameters": {
    "type": "object",
    "properties": {
      "session_id": {
        "type": "string",
        "description": "会话ID（可选，默认为当前会话）"
      }
    }
  }
}
```

#### 4.2.3 meta_handover

```json
{
  "name": "meta_handover",
  "description": "将当前任务移交给其他元操作，保留上下文和进度。",
  "parameters": {
    "type": "object",
    "properties": {
      "target_paradigm": {
        "type": "string",
        "description": "目标范式"
      },
      "reason": {
        "type": "string",
        "description": "移交原因"
      },
      "carry_context": {
        "type": "boolean",
        "description": "是否携带当前上下文",
        "default": true
      }
    },
    "required": ["target_paradigm", "reason"]
  }
}
```

#### 4.2.4 meta_feedback

```json
{
  "name": "meta_feedback",
  "description": "用户对元操作执行结果的反馈，用于改进元操作。",
  "parameters": {
    "type": "object",
    "properties": {
      "session_id": {
        "type": "string"
      },
      "rating": {
        "type": "integer",
        "minimum": 1,
        "maximum": 5,
        "description": "满意度评分"
      },
      "feedback_text": {
        "type": "string",
        "description": "具体反馈内容"
      },
      "issue_type": {
        "type": "string",
        "enum": ["wrong_paradigm", "incomplete", "tool_misuse", "performance", "other"]
      }
    },
    "required": ["rating"]
  }
}
```

#### 4.2.5 meta_improve

```json
{
  "name": "meta_improve",
  "description": "根据用户反馈或问题，改进元操作工具的实现。",
  "parameters": {
    "type": "object",
    "properties": {
      "paradigm": {
        "type": "string",
        "description": "要改进的元操作范式"
      },
      "improvement_request": {
        "type": "string",
        "description": "改进需求描述"
      },
      "auto_validate": {
        "type": "boolean",
        "description": "是否自动验证改进结果",
        "default": true
      }
    },
    "required": ["paradigm", "improvement_request"]
  }
}
```

---

## 5. 执行流程

### 5.1 标准执行流程

```
用户Query
    │
    ▼
┌─────────────────────┐
│ 1. meta_dispatch    │ ← 范式识别 + 路由
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 2. 元操作执行        │ ← 调用原子工具组合
│    (如 workflow_*)   │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 3. meta_status      │ ← 查询进度（可选）
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 4. 完成/移交/改进    │
└─────────────────────┘
```

### 5.2 动态生成流程

当元操作不存在时:

```
meta_dispatch → 发现无对应元操作
    │
    ▼
调用 MetaOperationRegistry.generate_dynamic()
    │
    ├─ 1. 搜索最佳实践 (GitHub API / 文档)
    │
    ├─ 2. 生成元操作定义 (LLM)
    │
    ├─ 3. 生成工具序列和状态机
    │
    ├─ 4. 验证工具可用性
    │
    └─ 5. 注册并返回
```

### 5.3 改进流程

当用户要求改进时:

```
meta_improve(paradigm, request)
    │
    ├─ 1. 分析当前实现
    │
    ├─ 2. 识别改进点
    │
    ├─ 3. 生成新版本代码
    │
    ├─ 4. 回归测试
    │
    └─ 5. 更新注册表
```

---

## 6. 数据结构设计

### 6.1 范式识别结果

```python
@dataclass
class RecognitionResult:
    paradigm: ParadigmType          # 识别的范式
    confidence: float               # 置信度 0.0-1.0
    reasoning: str                  # 识别推理过程
    keywords_matched: List[str]     # 匹配的关键词
    alternative_paradigms: List[Tuple[ParadigmType, float]]  # 备选范式
```

### 6.2 元操作执行结果

```python
@dataclass
class MetaOpResult:
    success: bool                   # 是否成功
    paradigm: ParadigmType          # 执行的范式
    meta_op_name: str               # 元操作名称
    output: str                     # 输出内容
    artifacts: Dict[str, str]       # 产出物
    tool_calls: List[ToolCallRecord]  # 调用的工具记录
    next_action: Optional[str]      # 下一步建议
    can_continue: bool              # 是否可继续
    error: Optional[str]            # 错误信息
```

### 6.3 元操作元数据

```python
@dataclass
class MetaOperationMeta:
    name: str                       # 元操作名称
    paradigm: ParadigmType          # 对应范式
    version: str                    # 版本号
    description: str                # 描述
    author: str                     # 作者 (system/llm-generated/user-defined)
    tools_used: List[str]           # 使用的原子工具
    state_machine: Dict             # 状态机定义
    created_at: float
    updated_at: float
    usage_count: int                # 使用次数
    avg_rating: float               # 平均评分
```

---

## 7. 持久化设计

### 7.1 存储结构

```
.repo_root/
├── .meta_operations/
│   ├── registry.json              # 元操作注册表
│   ├── sessions/                  # 会话状态
│   │   ├── session_xxx.json
│   │   └── ...
│   ├── definitions/               # 元操作定义文件
│   │   ├── code_development.json
│   │   ├── feature_design.json
│   │   └── ...
│   └── feedback/                  # 反馈记录
│       └── feedback_xxx.json
```

### 7.2 registry.json 示例

```json
{
  "version": "1.0",
  "meta_operations": [
    {
      "name": "code_development",
      "paradigm": "CODE_DEV",
      "definition_file": "definitions/code_development.json",
      "enabled": true,
      "builtin": true,
      "stats": {
        "usage_count": 42,
        "avg_rating": 4.5
      }
    },
    {
      "name": "test_evaluation",
      "paradigm": "TEST_EVAL",
      "definition_file": "definitions/test_evaluation.json",
      "enabled": true,
      "builtin": false,
      "generated_at": 1706012345.678,
      "stats": {
        "usage_count": 5,
        "avg_rating": 4.0
      }
    }
  ]
}
```

---

## 8. 非功能性需求

### 8.1 性能要求

| 指标 | 目标值 |
|------|--------|
| 范式识别延迟 | < 500ms (快速匹配) / < 3s (语义识别) |
| 元操作路由延迟 | < 100ms |
| 动态生成延迟 | < 30s |
| 并发会话支持 | ≥ 10 个同时执行 |

### 8.2 可扩展性

- 支持运行时动态注册新元操作
- 支持热更新元操作定义
- 支持用户自定义元操作

### 8.3 可观测性

- 每次元操作执行记录日志
- 支持追踪工具调用链
- 统计各范式使用频率
- 记录识别准确率

---

## 9. 与现有系统集成

### 9.1 集成点

| 现有组件 | 集成方式 |
|----------|----------|
| `ToolRegistry` | 元操作调用原子工具时通过ToolRegistry获取handler |
| `WorkflowManager` | `code_development`元操作内部使用WorkflowManager |
| `agent_loop()` | 在主循环中优先调用`meta_dispatch`，而非直接调用工具 |
| `TeammateManager` | 元操作可将子任务分发给队友 |

### 9.2 向后兼容

- 现有的`workflow_start/step/status`工具保持不变
- 现有代码可直接调用，不强制使用元操作层
- 元操作层作为可选的高级抽象

---

## 10. 实施计划

### 10.1 阶段划分

| 阶段 | 内容 | 预计工时 |
|------|------|----------|
| Phase 1 | 核心类和接口定义 | 2h |
| Phase 2 | 范式识别器实现 | 3h |
| Phase 3 | 元操作调度器实现 | 3h |
| Phase 4 | 内置元操作实现 (code_development) | 2h |
| Phase 5 | 动态生成功能实现 | 4h |
| Phase 6 | 工具注册和集成测试 | 2h |
| **总计** | | **16h** |

### 10.2 优先级

1. **P0**: 核心框架 (Phase 1-3)
2. **P1**: code_development元操作 (Phase 4)
3. **P2**: 动态生成 (Phase 5)
4. **P3**: 其他元操作 (feature_design, test_eval等)

---

## 11. 验收标准

### 11.1 功能验收

- [ ] `meta_dispatch` 能正确识别至少6种范式
- [ ] 范式识别准确率 ≥ 80% (基于测试集)
- [ ] 元操作执行不抛未捕获异常
- [ ] 动态生成的元操作可正常执行
- [ ] 支持会话状态持久化和恢复
- [ ] 支持元操作间移交

### 11.2 质量验收

- [ ] 代码覆盖率 ≥ 70%
- [ ] 无 P0/P1 级别 bug
- [ ] 文档完整 (docstring + README更新)
- [ ] AGENTS.md 已更新

---

## 12. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 范式识别不准确 | 用户体验差 | 提供force_paradigm参数让用户纠正 |
| 动态生成的元操作有bug | 任务失败 | 自动验证 + 降级到基础工具 |
| 元操作间移交丢失上下文 | 信息缺失 | 序列化完整上下文 + 校验 |

---

## 附录A: 范式关键词映射表

| 范式 | 关键词 |
|------|--------|
| CODE_DEV | 实现、开发、编写、修复、重构、代码、函数、类、模块 |
| FEATURE_DESIGN | 设计、架构、方案、接口、API、系统、模块化 |
| ENGINEERING | 部署、CI/CD、Docker、监控、性能、优化、配置 |
| TEST_EVAL | 测试、单元测试、覆盖率、断言、pytest、测试报告 |
| DOC_WRITING | 文档、README、说明、手册、API文档、注释 |
| DATA_ANALYSIS | 数据、分析、统计、可视化、报表、图表 |
| GENERAL | 是什么、为什么、怎么、如何、解释 |

---

## 附录B: 参考资料

1. **LangChain**: Agent工具链组织方式
2. **AutoGPT**: 任务分解和执行模式
3. **GitHub Copilot Workspace**: 开发工作流设计
4. **OpenAI Function Calling**: 工具定义规范

---

**文档结束**
