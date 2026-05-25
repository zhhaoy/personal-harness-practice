# 元操作工具组 - 架构设计文档

**版本**: v1.0  
**日期**: 2025-01-23

---

## 1. 类UML图

### 1.1 核心类图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              <<enumeration>>                                 │
│                              ParadigmType                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│  CODE_DEV                                                                    │
│  FEATURE_DESIGN                                                              │
│  ENGINEERING                                                                 │
│  TEST_EVAL                                                                   │
│  DOC_WRITING                                                                 │
│  DATA_ANALYSIS                                                               │
│  GENERAL                                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      │ uses
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           <<dataclass>>                                      │
│                         RecognitionResult                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│  - paradigm: ParadigmType                                                    │
│  - confidence: float                                                         │
│  - reasoning: str                                                            │
│  - keywords_matched: List[str]                                               │
│  - alternative_paradigms: List[Tuple[ParadigmType, float]]                  │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                        <<dataclass>>                                         │
│                          MetaOpResult                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  - success: bool                                                             │
│  - paradigm: ParadigmType                                                    │
│  - meta_op_name: str                                                         │
│  - output: str                                                               │
│  - artifacts: Dict[str, str]                                                 │
│  - tool_calls: List[ToolCallRecord]                                          │
│  - next_action: Optional[str]                                                │
│  - can_continue: bool                                                        │
│  - error: Optional[str]                                                      │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                        <<dataclass>>                                         │
│                       MetaOperationMeta                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│  - name: str                                                                 │
│  - paradigm: ParadigmType                                                    │
│  - version: str                                                              │
│  - description: str                                                          │
│  - author: str                                                               │
│  - tools_used: List[str]                                                     │
│  - state_machine: Dict                                                       │
│  - created_at: float                                                         │
│  - updated_at: float                                                         │
│  - usage_count: int                                                          │
│  - avg_rating: float                                                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 核心类关系图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│                     MetaOperationDispatcher                                 │
│                                                                             │
│  - paradigm_recognizer: ParadigmRecognizer                                  │
│  - registry: MetaOperationRegistry                                          │
│  - generator: MetaOpGenerator                                               │
│  - current_session: Optional[str]                                           │
│  - sessions: Dict[str, SessionState]                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  + dispatch(query: str, context: dict) -> MetaOpResult                      │
│  + recognize_paradigm(query: str) -> RecognitionResult                      │
│  + route_to_meta_op(paradigm: ParadigmType) -> MetaOperation                │
│  + get_status(session_id: str) -> SessionState                              │
│  + handover(target: ParadigmType, reason: str) -> MetaOpResult              │
│  + record_feedback(session_id: str, feedback: Feedback) -> void             │
└────────────────────────┬────────────────────────────────────────────────────┘
                         │
                         │ owns
                         │
        ┌────────────────┼────────────────┐
        │                │                │
        ▼                ▼                ▼
┌───────────────┐ ┌───────────────┐ ┌───────────────────┐
│Paradigm       │ │MetaOperation  │ │MetaOpGenerator    │
│Recognizer     │ │Registry       │ │                   │
├───────────────┤ ├───────────────┤ ├───────────────────┤
│- keyword_map  │ │- meta_ops:    │ │- llm_client       │
│- llm_client   │ │  Dict         │ │- github_client    │
│- history      │ │- storage_path │ │- validator        │
├───────────────┤ ├───────────────┤ ├───────────────────┤
│+ recognize()  │ │+ register()   │ │+ generate()       │
│+ fast_match() │ │+ get()        │ │+ search_best_     │
│+ semantic_    │ │+ list_all()   │ │  practice()       │
│  recognize()  │ │+ generate_    │ │+ validate()       │
└───────────────┘ │  dynamic()    │ └───────────────────┘
                  └───────┬───────┘
                          │
                          │ manages
                          │
                          ▼
          ┌───────────────────────────────┐
          │     <<abstract>>              │
          │     MetaOperation             │
          ├───────────────────────────────┤
          │  # state: MetaOpState         │
          │  # context: dict              │
          │  # tool_registry: ToolRegistry│
          ├───────────────────────────────┤
          │  + name() -> str              │
          │  + paradigm() -> ParadigmType │
          │  + description() -> str       │
          │  + validate_input() -> bool   │
          │  + execute() -> MetaOpResult  │
          │  + get_progress() -> float    │
          │  + can_handover() -> bool     │
          │  + pause() -> void            │
          │  + resume() -> void           │
          └───────────────┬───────────────┘
                          │
                          │ inherits
                          │
          ┌───────────────┼───────────────┐
          │               │               │
          ▼               ▼               ▼
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│CodeDevelop  │   │FeatureDesign│   │TestEvalu-   │
│mentMetaOp   │   │MetaOp       │   │ationMetaOp  │
├─────────────┤   ├─────────────┤   ├─────────────┤
│- workflow:  │   │- design_    │   │- test_      │
│  Workflow   │   │  phase: str │   │  runner     │
│  Manager    │   │- templates  │   │- coverage_  │
│- phases:    │   │             │   │  tool       │
│  List[Phase]│   │             │   │             │
├─────────────┤   ├─────────────┤   ├─────────────┤
│+ execute()  │   │+ execute()  │   │+ execute()  │
└─────────────┘   └─────────────┘   └─────────────┘
```

### 1.3 与现有系统集成关系图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              agent_loop()                                    │
│                                                                             │
│  1. 用户输入 query                                                           │
│  2. 调用 meta_dispatch → 识别范式                                            │
│  3. 获取 MetaOperation 并执行                                                │
│  4. 返回 MetaOpResult                                                        │
│                                                                             │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               │ uses
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       MetaOperationDispatcher                                │
│                                                                             │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐         │
│  │ParadigmRecognizer│  │MetaOperationReg  │  │MetaOpGenerator   │         │
│  │                  │  │istry             │  │                  │         │
│  └──────────────────┘  └────────┬─────────┘  └──────────────────┘         │
└─────────────────────────────────┼───────────────────────────────────────────┘
                                  │
                                  │ dispatches to
                                  │
                                  ▼
          ┌───────────────────────────────────────────────┐
          │              MetaOperation                    │
          │                                               │
          │  内部调用原子工具 (via ToolRegistry)           │
          │                                               │
          └───────────────────────┬───────────────────────┘
                                  │
                                  │ calls
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              ToolRegistry                                    │
│                                                                             │
│  现有原子工具:                                                                │
│  - read_file, write_file, edit_file, bash                                  │
│  - task_create, task_list, worktree_*                                       │
│  - spawn_teammate, send_message, ...                                        │
│  - workflow_start, workflow_step, workflow_status                           │
│                                                                             │
│  新增元操作工具:                                                              │
│  - meta_dispatch, meta_status, meta_handover, meta_feedback, meta_improve  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  │ uses
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          现有基础设施                                         │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │Workflow      │  │TaskManager   │  │Teammate      │  │MessageBus    │   │
│  │Manager       │  │              │  │Manager       │  │              │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 序列图

### 2.1 标准调度流程

```
User          agent_loop    Dispatcher     Recognizer    Registry      MetaOp
  │               │             │             │             │            │
  │  query        │             │             │             │            │
  │──────────────>│             │             │             │            │
  │               │             │             │             │            │
  │               │ dispatch()  │             │             │            │
  │               │────────────>│             │             │            │
  │               │             │             │             │            │
  │               │             │ recognize() │             │            │
  │               │             │────────────>│             │            │
  │               │             │             │             │            │
  │               │             │ Recognition │             │            │
  │               │             │<────────────│             │            │
  │               │             │   Result    │             │            │
  │               │             │             │             │            │
  │               │             │ get(paradigm)             │            │
  │               │             │───────────────────────────>│            │
  │               │             │             │             │            │
  │               │             │             │    MetaOperation         │
  │               │             │<───────────────────────────│            │
  │               │             │             │             │            │
  │               │             │ execute(query, context)   │            │
  │               │             │─────────────────────────────────────────>│
  │               │             │             │             │            │
  │               │             │             │             │  tool calls│
  │               │             │             │             │  (internal)│
  │               │             │             │             │            │
  │               │             │ MetaOpResult             │            │
  │               │             │<─────────────────────────────────────────│
  │               │             │             │             │            │
  │               │ MetaOpResult│             │             │            │
  │               │<────────────│             │             │            │
  │               │             │             │             │            │
  │  response     │             │             │             │            │
  │<──────────────│             │             │             │            │
  │               │             │             │             │            │
```

### 2.2 动态生成流程

```
Dispatcher     Registry      Generator     LLM        GitHub API    Validator
    │             │             │           │              │            │
    │ get(paradigm)             │           │              │            │
    │────────────>│             │           │              │            │
    │             │             │           │              │            │
    │ None        │             │           │              │            │
    │<────────────│             │           │              │            │
    │             │             │           │              │            │
    │ generate_dynamic(paradigm)│           │              │            │
    │───────────────────────────>│           │              │            │
    │             │             │           │              │            │
    │             │             │ search_best_practice     │            │
    │             │             │──────────────────────────────────>    │
    │             │             │           │              │            │
    │             │             │ examples  │              │            │
    │             │             │<──────────────────────────────────    │
    │             │             │           │              │            │
    │             │             │ generate_definition       │            │
    │             │             │────────────>│              │            │
    │             │             │           │              │            │
    │             │             │ definition│              │            │
    │             │             │<────────────│              │            │
    │             │             │           │              │            │
    │             │             │ validate(meta_op)         │            │
    │             │             │─────────────────────────────────────────>│
    │             │             │           │              │            │
    │             │             │ valid/invalid            │            │
    │             │             │<─────────────────────────────────────────│
    │             │             │           │              │            │
    │ MetaOpResult│             │           │              │            │
    │<───────────────────────────│           │              │            │
    │             │             │           │              │            │
```

### 2.3 元操作移交流程

```
User      agent_loop    Dispatcher    CodeDevMetaOp    TestEvalMetaOp
  │           │             │              │                 │
  │ "测试代码" │             │              │                 │
  │──────────>│             │              │                 │
  │           │             │              │                 │
  │           │ dispatch()  │              │                 │
  │           │────────────>│              │                 │
  │           │             │              │                 │
  │           │             │ execute()    │                 │
  │           │             │─────────────>│                 │
  │           │             │              │                 │
  │           │             │ code written │                 │
  │           │             │<─────────────│                 │
  │           │             │              │                 │
  │           │             │ handover(TEST_EVAL)            │
  │           │             │──────────────────────────────────>│
  │           │             │              │                 │
  │           │             │              │   execute()     │
  │           │             │              │<────────────────│
  │           │             │              │                 │
  │           │             │              │   test results  │
  │           │             │              │────────────────>│
  │           │             │              │                 │
  │           │ MetaOpResult│              │                 │
  │           │<────────────│              │                 │
  │           │             │              │                 │
  │  result   │             │              │                 │
  │<──────────│             │              │                 │
  │           │             │              │                 │
```

---

## 3. 状态机图

### 3.1 调度器状态机

```
                         ┌─────────────┐
                         │    IDLE     │
                         └──────┬──────┘
                                │
                                │ receive query
                                ▼
                         ┌─────────────┐
                         │ RECOGNIZING │
                         └──────┬──────┘
                                │
                    ┌───────────┼───────────┐
                    │           │           │
                    ▼           ▼           ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │FOUND_META│ │NOT_FOUND │ │ UNCERTAIN│
              │    _OP   │ │   _OP    │ │          │
              └────┬─────┘ └────┬─────┘ └────┬─────┘
                   │            │            │
                   │            │ generate   │ ask user
                   │            │            │
                   ▼            ▼            ▼
              ┌──────────────────────────────────┐
              │           EXECUTING               │
              └──────────────┬───────────────────┘
                             │
                    ┌────────┼────────┐
                    │        │        │
                    ▼        ▼        ▼
              ┌─────────┐ ┌─────────┐ ┌──────────┐
              │SUCCESS  │ │PARTIAL  │ │ HANDOVER │
              └────┬────┘ └────┬────┘ └────┬─────┘
                   │          │            │
                   │          │ continue   │ switch
                   │          │            │
                   ▼          ▼            ▼
              ┌──────────────────────────────────┐
              │            COMPLETED              │
              └──────────────┬───────────────────┘
                             │
                             │ feedback
                             ▼
                         ┌─────────────┐
                         │    IDLE     │
                         └─────────────┘
```

### 3.2 元操作执行状态机 (以 CodeDevelopment 为例)

```
┌──────────┐    confirm    ┌──────────┐    confirm    ┌──────────┐
│   ARCH   │───────────────>│   REQ    │───────────────>│  DESIGN  │
└──────────┘                └──────────┘                └──────────┘
                                                              │
                                                              │ confirm
                                                              ▼
                                                         ┌──────────┐
                                                         │ CONFIRM  │
                                                         └──────────┘
                                                              │
                                                              │ confirm (固化)
                                                              ▼
┌──────────┐   verify_pass   ┌──────────┐  execute_done  ┌──────────┐
│   DONE   │<────────────────│  VERIFY  │<───────────────│   EXEC   │
└──────────┘                 └──────────┘                └──────────┘
                                 │                            │
                                 │ verify_fail                │ error
                                 ▼                            │
                            ┌──────────┐                      │
                            │  REFINE  │<─────────────────────┘
                            └──────────┘
                                 │
                                 │ refine_done
                                 └──────────────> [回到 VERIFY]
```

---

## 4. 数据流图

### 4.1 元操作数据流

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│                         User Query + Context                                │
│                                                                             │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                    Paradigm Recognition                               │  │
│  │                                                                       │  │
│  │  Input: query, context                                                │  │
│  │  Output: RecognitionResult(paradigm, confidence, reasoning)          │  │
│  │                                                                       │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                    MetaOperation Routing                              │  │
│  │                                                                       │  │
│  │  Input: paradigm                                                      │  │
│  │  Output: MetaOperation instance                                       │  │
│  │                                                                       │  │
│  │  if not found → generate_dynamic()                                    │  │
│  │                                                                       │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                    MetaOperation Execution                            │  │
│  │                                                                       │  │
│  │  Input: query, context                                                │  │
│  │  Internal: state machine transitions, tool calls                      │  │
│  │  Output: MetaOpResult                                                 │  │
│  │                                                                       │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                    Result Aggregation                                 │  │
│  │                                                                       │  │
│  │  MetaOpResult:                                                        │  │
│  │    - success: bool                                                    │  │
│  │    - output: str                                                      │  │
│  │    - artifacts: Dict[str, str]                                        │  │
│  │    - tool_calls: List[ToolCallRecord]                                 │  │
│  │    - next_action: Optional[str]                                       │  │
│  │                                                                       │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │
                                ▼
                         Response to User
```

---

## 5. 包结构设计

```
meta_operation/
├── __init__.py
├── types/
│   ├── __init__.py
│   ├── paradigm.py          # ParadigmType enum
│   ├── result.py            # RecognitionResult, MetaOpResult, etc.
│   └── state.py             # Session state definitions
│
├── recognizer/
│   ├── __init__.py
│   ├── base.py              # ParadigmRecognizer base class
│   ├── keyword_matcher.py   # Fast keyword matching
│   ├── semantic.py          # LLM-based semantic recognition
│   └── history.py           # Historical data management
│
├── registry/
│   ├── __init__.py
│   ├── base.py              # MetaOperationRegistry
│   ├── storage.py           # Persistence layer
│   └── loader.py            # Load definitions from files
│
├── operations/
│   ├── __init__.py
│   ├── base.py              # MetaOperation abstract base
│   ├── code_development.py  # CODE_DEV implementation
│   ├── feature_design.py    # FEATURE_DESIGN implementation
│   ├── test_evaluation.py   # TEST_EVAL implementation
│   └── ...
│
├── generator/
│   ├── __init__.py
│   ├── base.py              # MetaOpGenerator
│   ├── best_practice.py     # Search GitHub for best practices
│   ├── llm_generator.py     # Generate definition using LLM
│   └── validator.py         # Validate generated operations
│
├── dispatcher/
│   ├── __init__.py
│   ├── core.py              # MetaOperationDispatcher
│   └── session.py           # Session management
│
└── tools/
    ├── __init__.py
    ├── meta_dispatch.py     # Tool: meta_dispatch
    ├── meta_status.py       # Tool: meta_status
    ├── meta_handover.py     # Tool: meta_handover
    ├── meta_feedback.py     # Tool: meta_feedback
    └── meta_improve.py      # Tool: meta_improve
```

---

## 6. 接口定义

### 6.1 核心接口

```python
# === types/paradigm.py ===
from enum import Enum

class ParadigmType(Enum):
    CODE_DEV = "code_development"
    FEATURE_DESIGN = "feature_design"
    ENGINEERING = "engineering_practice"
    TEST_EVAL = "test_evaluation"
    DOC_WRITING = "documentation"
    DATA_ANALYSIS = "data_analysis"
    GENERAL = "general_qa"


# === types/result.py ===
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional

@dataclass
class RecognitionResult:
    paradigm: ParadigmType
    confidence: float
    reasoning: str
    keywords_matched: List[str]
    alternative_paradigms: List[Tuple[ParadigmType, float]]


@dataclass
class MetaOpResult:
    success: bool
    paradigm: ParadigmType
    meta_op_name: str
    output: str
    artifacts: Dict[str, str]
    tool_calls: List[Dict]
    next_action: Optional[str]
    can_continue: bool
    error: Optional[str]


# === recognizer/base.py ===
from abc import ABC, abstractmethod

class ParadigmRecognizer(ABC):
    @abstractmethod
    def recognize(self, query: str, context: dict) -> RecognitionResult:
        """识别用户query的范式"""
        pass
    
    @abstractmethod
    def fast_match(self, query: str) -> Optional[ParadigmType]:
        """快速关键词匹配"""
        pass
    
    def update_history(self, query: str, paradigm: ParadigmType, correct: bool):
        """更新历史记录（用于学习）"""
        pass


# === operations/base.py ===
from abc import ABC, abstractmethod

class MetaOperation(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass
    
    @property
    @abstractmethod
    def paradigm(self) -> ParadigmType:
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        pass
    
    @abstractmethod
    def validate_input(self, query: str, context: dict) -> Tuple[bool, str]:
        """验证输入"""
        pass
    
    @abstractmethod
    def execute(self, query: str, context: dict) -> MetaOpResult:
        """执行元操作"""
        pass
    
    @abstractmethod
    def get_progress(self) -> float:
        """获取进度 0.0-1.0"""
        pass
    
    @abstractmethod
    def can_handover(self) -> bool:
        """是否支持移交"""
        pass
    
    def pause(self) -> None:
        """暂停执行"""
        pass
    
    def resume(self) -> None:
        """恢复执行"""
        pass


# === registry/base.py ===
from typing import Optional, List

class MetaOperationRegistry:
    def register(self, meta_op: MetaOperation) -> bool:
        """注册元操作"""
        pass
    
    def get(self, paradigm: ParadigmType) -> Optional[MetaOperation]:
        """获取元操作"""
        pass
    
    def list_all(self) -> List[Dict]:
        """列出所有元操作"""
        pass
    
    def exists(self, paradigm: ParadigmType) -> bool:
        """检查是否存在"""
        pass
    
    def update_stats(self, paradigm: ParadigmType, success: bool, rating: Optional[float] = None):
        """更新统计信息"""
        pass


# === dispatcher/core.py ===
class MetaOperationDispatcher:
    def __init__(
        self,
        recognizer: ParadigmRecognizer,
        registry: MetaOperationRegistry,
        generator: Optional['MetaOpGenerator'] = None
    ):
        pass
    
    def dispatch(self, query: str, context: dict) -> MetaOpResult:
        """主调度入口"""
        pass
    
    def recognize_paradigm(self, query: str) -> RecognitionResult:
        """范式识别"""
        pass
    
    def route_to_meta_op(self, paradigm: ParadigmType) -> MetaOperation:
        """路由到元操作"""
        pass
    
    def get_status(self, session_id: str) -> Dict:
        """获取会话状态"""
        pass
    
    def handover(self, target: ParadigmType, reason: str) -> MetaOpResult:
        """移交任务"""
        pass
    
    def record_feedback(self, session_id: str, rating: int, feedback: str) -> None:
        """记录反馈"""
        pass
```

---

## 7. 设计决策记录

### D1: 为什么使用范式识别而非直接意图识别？

**决策**: 采用"范式"而非通用的"意图"概念。

**理由**:
1. 范式更具体，与元操作一一对应
2. 范式数量有限，便于管理和优化
3. 避免无限分类导致的复杂性
4. 便于用户理解和纠正

### D2: 动态生成是同步还是异步？

**决策**: 同步生成，但提供超时和降级机制。

**理由**:
1. 用户期望即时反馈
2. 生成延迟可接受（<30s）
3. 异步增加复杂度，收益不大
4. 提供降级方案保证可用性

### D3: 元操作间如何传递上下文？

**决策**: 使用序列化的上下文对象，在移交时携带。

**理由**:
1. 避免全局状态污染
2. 便于调试和追踪
3. 支持跨会话恢复
4. 安全可控

---

## 8. 部署架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         Runtime Layer                            │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                   agent_loop.py                           │  │
│  │                                                           │  │
│  │  ┌─────────────────────────────────────────────────────┐ │  │
│  │  │           MetaOperationDispatcher                    │ │  │
│  │  │                                                      │ │  │
│  │  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │ │  │
│  │  │  │Recognizer   │  │Registry     │  │Generator    │ │ │  │
│  │  │  └─────────────┘  └─────────────┘  └─────────────┘ │ │  │
│  │  └─────────────────────────────────────────────────────┘ │  │
│  │                           │                               │  │
│  │                           ▼                               │  │
│  │  ┌─────────────────────────────────────────────────────┐ │  │
│  │  │              ToolRegistry                            │ │  │
│  │  └─────────────────────────────────────────────────────┘ │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
                               │
                               │ persists to
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Storage Layer                              │
│                                                                  │
│  .meta_operations/                                               │
│  ├── registry.json                                               │
│  ├── sessions/                                                   │
│  ├── definitions/                                                │
│  └── feedback/                                                   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

**文档结束**
