#!/usr/bin/env python3
"""
元操作工具组 (Meta-Operation Tool Suite)
实现范式识别、元操作调度、动态生成等功能
"""

import os
import json
import time
import threading
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Callable
from dataclasses import dataclass, field, asdict
from abc import ABC, abstractmethod
from enum import Enum
import traceback

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


# ========== 类型定义 ==========

class ParadigmType(Enum):
    CODE_DEV = "code_development"
    FEATURE_DESIGN = "feature_design"
    ENGINEERING = "engineering_practice"
    TEST_EVAL = "test_evaluation"
    DOC_WRITING = "documentation"
    DATA_ANALYSIS = "data_analysis"
    GENERAL = "general_qa"


@dataclass
class RecognitionResult:
    paradigm: ParadigmType
    confidence: float
    reasoning: str
    keywords_matched: List[str] = field(default_factory=list)
    alternative_paradigms: List[Tuple[ParadigmType, float]] = field(default_factory=list)


@dataclass
class MetaOpResult:
    success: bool
    paradigm: ParadigmType
    meta_op_name: str
    output: str
    artifacts: Dict[str, str] = field(default_factory=dict)
    tool_calls: List[Dict] = field(default_factory=list)
    next_action: Optional[str] = None
    can_continue: bool = False
    error: Optional[str] = None


@dataclass
class MetaOperationMeta:
    name: str
    paradigm: ParadigmType
    version: str = "1.0.0"
    description: str = ""
    author: str = "system"
    tools_used: List[str] = field(default_factory=list)
    state_machine: Dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    usage_count: int = 0
    avg_rating: float = 0.0
    builtin: bool = False


@dataclass
class SessionState:
    session_id: str
    paradigm: ParadigmType
    meta_op_name: str
    status: str  # running, paused, completed, failed
    current_phase: str
    context: Dict
    artifacts: Dict[str, str]
    tool_calls_history: List[Dict]
    created_at: float
    updated_at: float


# ========== 范式识别器 ==========

class ParadigmRecognizer:
    """范式识别器：分析用户query，判断最合适的行动范式"""
    
    PARADIGM_KEYWORDS = {
        ParadigmType.CODE_DEV: [
            "实现", "开发", "编写", "修复", "重构", "代码", "函数", "类", "模块",
            "bug", "feature", "implement", "code", "function", "class", "module",
            "编程", "脚本", "程序"
        ],
        ParadigmType.FEATURE_DESIGN: [
            "设计", "架构", "方案", "接口", "API", "系统", "模块化",
            "design", "architecture", "scheme", "interface", "system",
            "如何实现", "怎么设计", "技术选型"
        ],
        ParadigmType.ENGINEERING: [
            "部署", "CI", "CD", "Docker", "K8s", "监控", "性能", "优化", "配置",
            "deploy", "devops", "pipeline", "monitor", "optimize", "config",
            "自动化", "流水线", "容器化"
        ],
        ParadigmType.TEST_EVAL: [
            "测试", "单元测试", "覆盖率", "断言", "pytest", "unittest",
            "test", "coverage", "assert", "testing",
            "测试报告", "功能测试", "集成测试"
        ],
        ParadigmType.DOC_WRITING: [
            "文档", "README", "说明", "手册", "API文档", "注释",
            "document", "readme", "manual", "doc",
            "写文档", "文档生成"
        ],
        ParadigmType.DATA_ANALYSIS: [
            "数据", "分析", "统计", "可视化", "报表", "图表",
            "data", "analysis", "statistics", "visualization", "report", "chart",
            "数据处理", "数据分析", "生成报表"
        ],
        ParadigmType.GENERAL: [
            "是什么", "为什么", "怎么", "如何", "解释", "什么是",
            "what", "why", "how", "explain",
            "帮我", "请问", "查询"
        ]
    }
    
    PARADIGM_PATTERNS = {
        ParadigmType.CODE_DEV: [
            r"帮我(实现|编写|开发)",
            r"(修复|fix).*bug",
            r"重构.*代码",
            r"新增.*功能",
        ],
        ParadigmType.FEATURE_DESIGN: [
            r"设计.*系统",
            r"架构(设计|方案)",
            r"如何(实现|设计)",
            r"技术(选型|方案)",
        ],
        ParadigmType.ENGINEERING: [
            r"部署.*应用",
            r"CI.*CD",
            r"(优化|提升).*性能",
            r"配置.*环境",
        ],
        ParadigmType.TEST_EVAL: [
            r"(编写|写).*测试",
            r"测试.*覆盖率",
            r"(单元|集成|功能).*测试",
        ],
        ParadigmType.DOC_WRITING: [
            r"(写|生成|编写).*文档",
            r"README",
            r"API.*文档",
        ],
        ParadigmType.DATA_ANALYSIS: [
            r"(分析|统计).*数据",
            r"生成.*报表",
            r"数据.*可视化",
        ],
    }
    
    def __init__(self, llm_client=None, use_llm_fallback: bool = True):
        self.llm_client = llm_client
        self.use_llm_fallback = use_llm_fallback
        self._history: List[Tuple[str, ParadigmType, bool]] = []
    
    def recognize(self, query: str, context: dict = None) -> RecognitionResult:
        """识别用户query的范式"""
        context = context or {}
        
        fast_result = self.fast_match(query)
        if fast_result and fast_result[1] > 0.8:
            return RecognitionResult(
                paradigm=fast_result[0],
                confidence=fast_result[1],
                reasoning=f"关键词快速匹配成功",
                keywords_matched=fast_result[2]
            )
        
        if self.use_llm_fallback and self.llm_client:
            return self.semantic_recognize(query, context)
        
        if fast_result:
            return RecognitionResult(
                paradigm=fast_result[0],
                confidence=fast_result[1] * 0.7,
                reasoning="关键词匹配（低置信度）",
                keywords_matched=fast_result[2]
            )
        
        return RecognitionResult(
            paradigm=ParadigmType.GENERAL,
            confidence=0.5,
            reasoning="无法识别具体范式，默认为通用问答",
            keywords_matched=[]
        )
    
    def fast_match(self, query: str) -> Optional[Tuple[ParadigmType, float, List[str]]]:
        """快速关键词匹配，返回 (范式, 置信度, 匹配的关键词)"""
        query_lower = query.lower()
        scores: Dict[ParadigmType, Tuple[float, List[str]]] = {}
        
        for paradigm, keywords in self.PARADIGM_KEYWORDS.items():
            matched = []
            for kw in keywords:
                if kw.lower() in query_lower:
                    matched.append(kw)
            if matched:
                confidence = min(1.0, len(matched) * 0.25 + 0.3)
                scores[paradigm] = (confidence, matched)
        
        for paradigm, patterns in self.PARADIGM_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, query_lower):
                    current = scores.get(paradigm, (0, []))
                    scores[paradigm] = (min(1.0, current[0] + 0.3), current[1])
        
        if not scores:
            return None
        
        sorted_scores = sorted(scores.items(), key=lambda x: x[1][0], reverse=True)
        best_paradigm, (best_conf, best_keywords) = sorted_scores[0]
        
        return (best_paradigm, best_conf, best_keywords)
    
    def semantic_recognize(self, query: str, context: dict = None) -> RecognitionResult:
        """基于LLM的语义识别"""
        if not self.llm_client:
            return RecognitionResult(
                paradigm=ParadigmType.GENERAL,
                confidence=0.5,
                reasoning="LLM客户端未配置",
                keywords_matched=[]
            )
        
        prompt = f"""分析以下用户查询，判断最合适的任务类型（范式）。

用户查询：{query}

可选范式：
1. CODE_DEV - 代码开发类（实现功能、修复bug、重构代码）
2. FEATURE_DESIGN - 功能设计类（系统设计、架构设计、接口设计）
3. ENGINEERING - 工程实践类（CI/CD、部署、监控、性能优化）
4. TEST_EVAL - 测试评估类（编写测试、覆盖率分析、性能测试）
5. DOC_WRITING - 文档编写类（README、API文档、用户手册）
6. DATA_ANALYSIS - 数据分析类（数据处理、可视化、报表生成）
7. GENERAL - 通用问答类（简单问答、信息查询）

请以JSON格式返回：
{{
  "paradigm": "CODE_DEV",
  "confidence": 0.95,
  "reasoning": "检测到关键词..."
}}

只返回JSON，不要其他内容。"""
        
        try:
            from agent_loop import MultiModelClient
            client = MultiModelClient() if self.llm_client is True else self.llm_client
            resp = client._chat_no_stream([{"role": "user", "content": prompt}], [])
            content = resp.get("content", "")
            
            json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                paradigm_str = data.get("paradigm", "GENERAL")
                paradigm = ParadigmType(paradigm_str.lower() if paradigm_str.lower() in [p.value for p in ParadigmType] else "general_qa")
                
                return RecognitionResult(
                    paradigm=paradigm,
                    confidence=float(data.get("confidence", 0.7)),
                    reasoning=data.get("reasoning", "LLM语义识别"),
                    keywords_matched=[]
                )
        except Exception as e:
            print(f"[范式识别] LLM识别失败: {e}")
        
        fast_result = self.fast_match(query)
        if fast_result:
            return RecognitionResult(
                paradigm=fast_result[0],
                confidence=fast_result[1] * 0.6,
                reasoning="LLM识别失败，回退到关键词匹配",
                keywords_matched=fast_result[2]
            )
        
        return RecognitionResult(
            paradigm=ParadigmType.GENERAL,
            confidence=0.5,
            reasoning="识别失败，默认通用问答",
            keywords_matched=[]
        )
    
    def update_history(self, query: str, paradigm: ParadigmType, correct: bool):
        """更新历史记录"""
        self._history.append((query[:100], paradigm, correct))
        if len(self._history) > 100:
            self._history = self._history[-100:]


# ========== 元操作基类 ==========

class MetaOperation(ABC):
    """元操作抽象基类"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        pass
    
    @property
    @abstractmethod
    def paradigm(self) -> ParadigmType:
        pass
    
    @property
    def description(self) -> str:
        return f"元操作: {self.name}"
    
    @property
    def version(self) -> str:
        return "1.0.0"
    
    def validate_input(self, query: str, context: dict) -> Tuple[bool, str]:
        """验证输入是否适用"""
        return True, ""
    
    @abstractmethod
    def execute(self, query: str, context: dict) -> MetaOpResult:
        """执行元操作"""
        pass
    
    def get_progress(self) -> float:
        """获取执行进度"""
        return 0.0
    
    def can_handover(self) -> bool:
        """是否支持移交"""
        return False
    
    def pause(self) -> None:
        """暂停执行"""
        pass
    
    def resume(self) -> None:
        """恢复执行"""
        pass


class CodeDevelopmentMetaOp(MetaOperation):
    """代码开发元操作：使用工作流进行结构化开发"""
    
    @property
    def name(self) -> str:
        return "code_development"
    
    @property
    def paradigm(self) -> ParadigmType:
        return ParadigmType.CODE_DEV
    
    @property
    def description(self) -> str:
        return "代码开发元操作：架构→需求→设计→固化→执行→验证→完成"
    
    def __init__(self, workflow_manager=None, tool_registry=None):
        self.workflow_manager = workflow_manager
        self.tool_registry = tool_registry
        self._current_session: Optional[str] = None
        self._phase: str = "ARCH"
    
    def execute(self, query: str, context: dict) -> MetaOpResult:
        """执行代码开发元操作"""
        context = context or {}
        
        instruction = self._get_phase_instruction()
        
        return MetaOpResult(
            success=True,
            paradigm=self.paradigm,
            meta_op_name=self.name,
            output=f"代码开发工作流已启动。\n\n当前阶段: {self._phase}\n\n{instruction}",
            artifacts={},
            tool_calls=[],
            next_action="请调用 workflow_start 启动工作流，然后按阶段调用 workflow_step 推进。",
            can_continue=True
        )
    
    def _get_phase_instruction(self) -> str:
        instructions = {
            "ARCH": "请输出架构设计（整体结构、技术选型、约束），然后询问用户确认。",
            "REQ": "请输出需求分析（验收标准、功能列表、模糊点），然后询问用户确认。",
            "DESIGN": "请输出详细设计（可执行的步骤、接口、数据结构），完成后询问用户固化。",
            "CONFIRM": "等待用户确认固化计划。",
            "EXEC": "计划已固化，请按设计逐步执行。",
            "VERIFY": "请验证执行结果是否符合需求。",
            "DONE": "工作流完成。"
        }
        return instructions.get(self._phase, "未知阶段")
    
    def get_progress(self) -> float:
        phases = ["ARCH", "REQ", "DESIGN", "CONFIRM", "EXEC", "VERIFY", "DONE"]
        if self._phase in phases:
            return phases.index(self._phase) / (len(phases) - 1)
        return 0.0
    
    def can_handover(self) -> bool:
        return True


class FeatureDesignMetaOp(MetaOperation):
    """功能设计元操作"""
    
    @property
    def name(self) -> str:
        return "feature_design"
    
    @property
    def paradigm(self) -> ParadigmType:
        return ParadigmType.FEATURE_DESIGN
    
    def execute(self, query: str, context: dict) -> MetaOpResult:
        return MetaOpResult(
            success=True,
            paradigm=self.paradigm,
            meta_op_name=self.name,
            output="功能设计元操作已启动。将进行：需求分析 → 方案设计 → 接口定义 → 技术评审",
            artifacts={},
            tool_calls=[],
            next_action="请描述你要设计的功能或系统。",
            can_continue=True
        )


class TestEvaluationMetaOp(MetaOperation):
    """测试评估元操作"""
    
    @property
    def name(self) -> str:
        return "test_evaluation"
    
    @property
    def paradigm(self) -> ParadigmType:
        return ParadigmType.TEST_EVAL
    
    def execute(self, query: str, context: dict) -> MetaOpResult:
        return MetaOpResult(
            success=True,
            paradigm=self.paradigm,
            meta_op_name=self.name,
            output="测试评估元操作已启动。将进行：测试计划 → 用例设计 → 执行测试 → 覆盖率分析 → 报告生成",
            artifacts={},
            tool_calls=[],
            next_action="请指定要测试的模块或功能。",
            can_continue=True
        )


class EngineeringMetaOp(MetaOperation):
    """工程实践元操作"""
    
    @property
    def name(self) -> str:
        return "engineering_practice"
    
    @property
    def paradigm(self) -> ParadigmType:
        return ParadigmType.ENGINEERING
    
    def execute(self, query: str, context: dict) -> MetaOpResult:
        return MetaOpResult(
            success=True,
            paradigm=self.paradigm,
            meta_op_name=self.name,
            output="工程实践元操作已启动。支持：CI/CD配置、部署脚本、性能优化、监控配置等",
            artifacts={},
            tool_calls=[],
            next_action="请描述你的工程需求。",
            can_continue=True
        )


class GeneralQAMetaOp(MetaOperation):
    """通用问答元操作"""
    
    @property
    def name(self) -> str:
        return "general_qa"
    
    @property
    def paradigm(self) -> ParadigmType:
        return ParadigmType.GENERAL
    
    def execute(self, query: str, context: dict) -> MetaOpResult:
        return MetaOpResult(
            success=True,
            paradigm=self.paradigm,
            meta_op_name=self.name,
            output="通用问答模式。将直接回答你的问题。",
            artifacts={},
            tool_calls=[],
            next_action="请提出你的问题。",
            can_continue=False
        )


# ========== 元操作注册表 ==========

class MetaOperationRegistry:
    """元操作注册表：管理所有元操作的注册、查询、统计"""
    
    def __init__(self, storage_path: Path = None):
        self.storage_path = storage_path or Path.cwd() / ".meta_operations"
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        self._meta_ops: Dict[ParadigmType, MetaOperation] = {}
        self._meta_data: Dict[ParadigmType, MetaOperationMeta] = {}
        self._lock = threading.RLock()
        
        self._register_builtin()
        self._load_from_storage()
    
    def _register_builtin(self):
        """注册内置元操作"""
        builtin_ops = [
            CodeDevelopmentMetaOp(),
            FeatureDesignMetaOp(),
            TestEvaluationMetaOp(),
            EngineeringMetaOp(),
            GeneralQAMetaOp(),
        ]
        
        for op in builtin_ops:
            self._meta_ops[op.paradigm] = op
            self._meta_data[op.paradigm] = MetaOperationMeta(
                name=op.name,
                paradigm=op.paradigm,
                description=op.description,
                author="system",
                builtin=True
            )
    
    def _load_from_storage(self):
        """从存储加载自定义元操作"""
        registry_file = self.storage_path / "registry.json"
        if not registry_file.exists():
            return
        
        try:
            data = json.loads(registry_file.read_text(encoding="utf-8"))
            for item in data.get("meta_operations", []):
                if item.get("builtin"):
                    continue
                paradigm = ParadigmType(item["paradigm"])
                self._meta_data[paradigm] = MetaOperationMeta(**item)
        except Exception as e:
            print(f"[元操作注册表] 加载失败: {e}")
    
    def register(self, meta_op: MetaOperation, meta_data: MetaOperationMeta = None) -> bool:
        """注册元操作"""
        with self._lock:
            self._meta_ops[meta_op.paradigm] = meta_op
            if meta_data:
                self._meta_data[meta_op.paradigm] = meta_data
            else:
                self._meta_data[meta_op.paradigm] = MetaOperationMeta(
                    name=meta_op.name,
                    paradigm=meta_op.paradigm,
                    description=meta_op.description,
                    author="dynamic"
                )
            self._save_registry()
            return True
    
    def get(self, paradigm: ParadigmType) -> Optional[MetaOperation]:
        """获取元操作"""
        with self._lock:
            return self._meta_ops.get(paradigm)
    
    def get_meta(self, paradigm: ParadigmType) -> Optional[MetaOperationMeta]:
        """获取元操作元数据"""
        with self._lock:
            return self._meta_data.get(paradigm)
    
    def exists(self, paradigm: ParadigmType) -> bool:
        """检查是否存在"""
        return paradigm in self._meta_ops
    
    def list_all(self) -> List[Dict]:
        """列出所有元操作"""
        with self._lock:
            return [
                {
                    "name": meta.name,
                    "paradigm": meta.paradigm.value,
                    "description": meta.description,
                    "author": meta.author,
                    "builtin": meta.author == "system",
                    "usage_count": meta.usage_count,
                    "avg_rating": meta.avg_rating
                }
                for meta in self._meta_data.values()
            ]
    
    def update_stats(self, paradigm: ParadigmType, success: bool, rating: Optional[float] = None):
        """更新统计信息"""
        with self._lock:
            if paradigm in self._meta_data:
                meta = self._meta_data[paradigm]
                meta.usage_count += 1
                if rating is not None:
                    total_rating = meta.avg_rating * (meta.usage_count - 1) + rating
                    meta.avg_rating = total_rating / meta.usage_count
                self._save_registry()
    
    def _save_registry(self):
        """保存注册表"""
        registry_file = self.storage_path / "registry.json"
        
        def convert_for_json(obj):
            """转换对象为JSON可序列化格式"""
            if isinstance(obj, ParadigmType):
                return obj.value
            elif isinstance(obj, dict):
                return {k: convert_for_json(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_for_json(v) for v in obj]
            return obj
        
        data = {
            "version": "1.0",
            "meta_operations": [convert_for_json(asdict(meta)) for meta in self._meta_data.values()]
        }
        registry_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ========== 元操作生成器 ==========

class MetaOpGenerator:
    """元操作生成器：动态生成缺失的元操作"""
    
    def __init__(self, llm_client=None, registry: MetaOperationRegistry = None):
        self.llm_client = llm_client
        self.registry = registry
    
    def generate(self, paradigm: ParadigmType, query: str, context: dict = None) -> Tuple[bool, MetaOperation, str]:
        """
        动态生成元操作
        返回: (成功?, 元操作实例, 错误信息)
        """
        if not self.llm_client:
            return False, None, "LLM客户端未配置"
        
        try:
            definition = self._generate_definition(paradigm, query)
            
            meta_op = self._instantiate_meta_op(paradigm, definition)
            
            if self.registry:
                meta_data = MetaOperationMeta(
                    name=definition.get("name", f"dynamic_{paradigm.value}"),
                    paradigm=paradigm,
                    description=definition.get("description", ""),
                    author="dynamic_generated",
                    tools_used=definition.get("tools_used", [])
                )
                self.registry.register(meta_op, meta_data)
            
            return True, meta_op, ""
            
        except Exception as e:
            return False, None, f"生成失败: {str(e)}"
    
    def _generate_definition(self, paradigm: ParadigmType, query: str) -> Dict:
        """生成元操作定义"""
        prompt = f"""根据以下信息，生成一个元操作的定义。

目标范式: {paradigm.value}
用户查询: {query}

请生成一个JSON格式的元操作定义，包含：
1. name: 元操作名称
2. description: 描述
3. tools_used: 将使用的原子工具列表
4. workflow: 工作流程步骤
5. state_machine: 状态机定义

返回JSON格式：
{{
  "name": "xxx",
  "description": "xxx",
  "tools_used": ["tool1", "tool2"],
  "workflow": ["step1", "step2"],
  "state_machine": {{}}
}}

只返回JSON，不要其他内容。"""

        try:
            from agent_loop import MultiModelClient
            client = MultiModelClient() if self.llm_client is True else self.llm_client
            resp = client._chat_no_stream([{"role": "user", "content": prompt}], [])
            content = resp.get("content", "")
            
            json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except Exception as e:
            print(f"[元操作生成器] 生成定义失败: {e}")
        
        return {
            "name": f"dynamic_{paradigm.value}",
            "description": f"动态生成的{paradigm.value}元操作",
            "tools_used": ["read_file", "write_file", "bash"],
            "workflow": ["分析", "执行", "验证"],
            "state_machine": {}
        }
    
    def _instantiate_meta_op(self, paradigm: ParadigmType, definition: Dict) -> MetaOperation:
        """实例化元操作"""
        class DynamicMetaOp(MetaOperation):
            def __init__(self, p, d):
                self._paradigm = p
                self._definition = d
            
            @property
            def name(self) -> str:
                return self._definition.get("name", "dynamic")
            
            @property
            def paradigm(self) -> ParadigmType:
                return self._paradigm
            
            @property
            def description(self) -> str:
                return self._definition.get("description", "")
            
            def execute(self, query: str, context: dict) -> MetaOpResult:
                workflow = self._definition.get("workflow", [])
                tools = self._definition.get("tools_used", [])
                
                return MetaOpResult(
                    success=True,
                    paradigm=self._paradigm,
                    meta_op_name=self.name,
                    output=f"动态元操作已启动。\n工作流程: {' → '.join(workflow)}\n使用工具: {', '.join(tools)}",
                    artifacts={},
                    tool_calls=[],
                    next_action="请提供具体任务详情。",
                    can_continue=True
                )
        
        return DynamicMetaOp(paradigm, definition)


# ========== 元操作调度器 ==========

class MetaOperationDispatcher:
    """元操作调度器：范式识别、路由、会话管理"""
    
    def __init__(
        self,
        recognizer: ParadigmRecognizer = None,
        registry: MetaOperationRegistry = None,
        generator: MetaOpGenerator = None
    ):
        self.recognizer = recognizer or ParadigmRecognizer(llm_client=True)
        self.registry = registry or MetaOperationRegistry()
        self.generator = generator or MetaOpGenerator(llm_client=True, registry=self.registry)
        
        self._sessions: Dict[str, SessionState] = {}
        self._current_session_id: Optional[str] = None
        self._lock = threading.RLock()
    
    def dispatch(
        self,
        query: str,
        context: dict = None,
        force_paradigm: ParadigmType = None
    ) -> MetaOpResult:
        """
        主调度入口
        1. 识别范式
        2. 获取/生成元操作
        3. 执行元操作
        """
        context = context or {}
        
        if force_paradigm:
            recognition = RecognitionResult(
                paradigm=force_paradigm,
                confidence=1.0,
                reasoning="用户强制指定范式",
                keywords_matched=[]
            )
        else:
            recognition = self.recognizer.recognize(query, context)
        
        paradigm = recognition.paradigm
        
        meta_op = self.registry.get(paradigm)
        
        if not meta_op:
            success, meta_op, error = self.generator.generate(paradigm, query, context)
            if not success:
                return MetaOpResult(
                    success=False,
                    paradigm=paradigm,
                    meta_op_name="",
                    output="",
                    error=f"无法获取或生成元操作: {error}"
                )
        
        session_id = self._create_session(paradigm, meta_op.name, context)
        
        valid, msg = meta_op.validate_input(query, context)
        if not valid:
            return MetaOpResult(
                success=False,
                paradigm=paradigm,
                meta_op_name=meta_op.name,
                output="",
                error=f"输入验证失败: {msg}"
            )
        
        result = meta_op.execute(query, context)
        
        self._update_session(session_id, result)
        
        self.registry.update_stats(paradigm, result.success)
        
        return result
    
    def recognize_paradigm(self, query: str) -> RecognitionResult:
        """范式识别"""
        return self.recognizer.recognize(query)
    
    def get_status(self, session_id: str = None) -> Dict:
        """获取会话状态"""
        sid = session_id or self._current_session_id
        if not sid or sid not in self._sessions:
            return {"error": "会话不存在"}
        
        session = self._sessions[sid]
        return {
            "session_id": session.session_id,
            "paradigm": session.paradigm.value,
            "meta_op_name": session.meta_op_name,
            "status": session.status,
            "current_phase": session.current_phase,
            "artifacts": session.artifacts,
            "tool_calls_count": len(session.tool_calls_history)
        }
    
    def handover(self, target_paradigm: ParadigmType, reason: str, carry_context: bool = True) -> MetaOpResult:
        """移交任务到其他元操作"""
        if not self._current_session_id:
            return MetaOpResult(
                success=False,
                paradigm=target_paradigm,
                meta_op_name="",
                output="",
                error="没有活动会话"
            )
        
        current_session = self._sessions.get(self._current_session_id)
        if not current_session:
            return MetaOpResult(
                success=False,
                paradigm=target_paradigm,
                meta_op_name="",
                output="",
                error="当前会话不存在"
            )
        
        context = current_session.context if carry_context else {}
        context["handover_from"] = current_session.paradigm.value
        context["handover_reason"] = reason
        
        return self.dispatch(
            query=f"[移交任务] {reason}",
            context=context,
            force_paradigm=target_paradigm
        )
    
    def record_feedback(self, session_id: str, rating: int, feedback: str = "") -> None:
        """记录用户反馈"""
        if session_id not in self._sessions:
            return
        
        session = self._sessions[session_id]
        self.registry.update_stats(session.paradigm, True, float(rating))
    
    def _create_session(self, paradigm: ParadigmType, meta_op_name: str, context: dict) -> str:
        """创建新会话"""
        import uuid
        session_id = str(uuid.uuid4())[:8]
        
        session = SessionState(
            session_id=session_id,
            paradigm=paradigm,
            meta_op_name=meta_op_name,
            status="running",
            current_phase="init",
            context=context,
            artifacts={},
            tool_calls_history=[],
            created_at=time.time(),
            updated_at=time.time()
        )
        
        with self._lock:
            self._sessions[session_id] = session
            self._current_session_id = session_id
        
        return session_id
    
    def _update_session(self, session_id: str, result: MetaOpResult):
        """更新会话状态"""
        with self._lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                session.status = "running" if result.can_continue else "completed"
                session.artifacts.update(result.artifacts)
                session.tool_calls_history.extend(result.tool_calls)
                session.updated_at = time.time()


# ========== 全局实例 ==========

DISPATCHER = None

def get_dispatcher() -> MetaOperationDispatcher:
    """获取全局调度器实例"""
    global DISPATCHER
    if DISPATCHER is None:
        DISPATCHER = MetaOperationDispatcher()
    return DISPATCHER


# ========== 工具函数（供ToolRegistry调用） ==========

def run_meta_dispatch(query: str, context: dict = None, force_paradigm: str = None) -> Tuple[str, Optional[str]]:
    """执行元操作调度"""
    try:
        dispatcher = get_dispatcher()
        
        force_p = None
        if force_paradigm:
            try:
                force_p = ParadigmType(force_paradigm.lower())
            except ValueError:
                pass
        
        result = dispatcher.dispatch(query, context or {}, force_p)
        
        output = {
            "paradigm": result.paradigm.value,
            "meta_operation": result.meta_op_name,
            "success": result.success,
            "output": result.output,
            "next_action": result.next_action,
            "can_continue": result.can_continue
        }
        
        if result.error:
            output["error"] = result.error
        
        return json.dumps(output, indent=2, ensure_ascii=False), None
        
    except Exception as e:
        return "", f"元操作调度失败: {str(e)}\n{traceback.format_exc()}"


def run_meta_status(session_id: str = None) -> Tuple[str, Optional[str]]:
    """查询元操作状态"""
    try:
        dispatcher = get_dispatcher()
        status = dispatcher.get_status(session_id)
        return json.dumps(status, indent=2, ensure_ascii=False), None
    except Exception as e:
        return "", f"查询状态失败: {str(e)}"


def run_meta_handover(target_paradigm: str, reason: str, carry_context: bool = True) -> Tuple[str, Optional[str]]:
    """移交任务到其他元操作"""
    try:
        dispatcher = get_dispatcher()
        target_p = ParadigmType(target_paradigm.lower())
        result = dispatcher.handover(target_p, reason, carry_context)
        
        output = {
            "success": result.success,
            "new_paradigm": result.paradigm.value,
            "meta_operation": result.meta_op_name,
            "output": result.output
        }
        
        return json.dumps(output, indent=2, ensure_ascii=False), None
    except Exception as e:
        return "", f"移交失败: {str(e)}"


def run_meta_feedback(session_id: str, rating: int, feedback_text: str = "", issue_type: str = None) -> Tuple[str, Optional[str]]:
    """记录用户反馈"""
    try:
        dispatcher = get_dispatcher()
        dispatcher.record_feedback(session_id, rating, feedback_text)
        return f"已记录反馈 (评分: {rating})", None
    except Exception as e:
        return "", f"记录反馈失败: {str(e)}"


def run_meta_improve(paradigm: str, improvement_request: str, auto_validate: bool = True) -> Tuple[str, Optional[str]]:
    """改进元操作"""
    try:
        dispatcher = get_dispatcher()
        target_p = ParadigmType(paradigm.lower())
        
        generator = dispatcher.generator
        success, meta_op, error = generator.generate(target_p, improvement_request)
        
        if success:
            return f"元操作 {paradigm} 已改进。新版本已生效。", None
        else:
            return "", f"改进失败: {error}"
    except Exception as e:
        return "", f"改进失败: {str(e)}"


def run_meta_list() -> Tuple[str, Optional[str]]:
    """列出所有元操作"""
    try:
        dispatcher = get_dispatcher()
        ops = dispatcher.registry.list_all()
        return json.dumps(ops, indent=2, ensure_ascii=False), None
    except Exception as e:
        return "", f"列表查询失败: {str(e)}"


# ========== 测试入口 ==========

if __name__ == "__main__":
    print("=== 元操作工具组测试 ===\n")
    
    dispatcher = MetaOperationDispatcher()
    
    test_queries = [
        "帮我实现一个用户登录功能",
        "设计一个电商系统架构",
        "编写单元测试覆盖核心模块",
        "配置CI/CD流水线",
        "什么是Python的GIL？"
    ]
    
    for query in test_queries:
        print(f"\nQuery: {query}")
        recognition = dispatcher.recognize_paradigm(query)
        print(f"识别结果: {recognition.paradigm.value} (置信度: {recognition.confidence:.2f})")
        print(f"推理: {recognition.reasoning}")
        
        result = dispatcher.dispatch(query)
        print(f"元操作: {result.meta_op_name}")
        print(f"输出: {result.output[:100]}...")
        print("-" * 50)
