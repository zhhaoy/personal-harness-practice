#!/usr/bin/env python3
"""
元操作总管家 (Meta Dispatcher - The Grand Steward)
用户唯一入口，负责范式识别、流程调度、执行控制

架构说明：
- 用户层：仅 meta_dispatch 可见
- 流程层：各种标准流程（章程+方法+机制）
- 工具矩阵层：30+ 底层工具（用户不可见）
"""

import os
import json
import time
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field, asdict
from enum import Enum
import traceback

# 导入工具矩阵和流程定义
try:
    from tool_matrix import (
        ToolMatrix, ToolGroup, get_tool_matrix, init_tool_matrix,
        internal_call_tool, internal_list_allowed_tools
    )
    from process_definition import (
        ProcessDefinition, Phase, PhaseStatus,
        create_code_development_process,
        create_test_evaluation_process,
        create_general_qa_process,
        create_feature_design_process,
        create_engineering_process,
        create_documentation_process,
    )
    TOOL_MATRIX_AVAILABLE = True
except ImportError as e:
    print(f"警告: 工具矩阵或流程定义模块导入失败: {e}")
    TOOL_MATRIX_AVAILABLE = False


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


@dataclass
class PhaseResult:
    phase: str
    status: PhaseStatus
    output: str
    tools_called: List[Dict] = field(default_factory=list)
    artifacts: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class ProcessResult:
    success: bool
    paradigm: str
    process_name: str
    phases_completed: List[str]
    phases_results: List[PhaseResult]
    final_output: str
    all_artifacts: Dict[str, str]
    all_tool_calls: List[Dict]
    error: Optional[str] = None


@dataclass
class DispatchResult:
    success: bool
    paradigm: ParadigmType
    process_name: str
    output: str
    artifacts: Dict[str, str] = field(default_factory=dict)
    tool_calls: List[Dict] = field(default_factory=list)
    session_id: Optional[str] = None
    next_action: Optional[str] = None
    can_continue: bool = False
    error: Optional[str] = None


@dataclass
class SessionState:
    session_id: str
    paradigm: ParadigmType
    process_name: str
    current_phase: str
    phase_status: Dict[str, PhaseStatus]
    context: Dict
    artifacts: Dict[str, str]
    tool_calls_history: List[Dict]
    created_at: float
    updated_at: float


# ========== 范式识别器 ==========

class ParadigmRecognizer:
    """范式识别器"""
    
    KEYWORDS = {
        ParadigmType.CODE_DEV: [
            "实现", "开发", "编写", "修复", "重构", "代码", "函数", "类", "模块",
            "bug", "feature", "implement", "code"
        ],
        ParadigmType.FEATURE_DESIGN: [
            "设计", "架构", "方案", "接口", "API", "系统", "模块化",
            "design", "architecture", "scheme"
        ],
        ParadigmType.ENGINEERING: [
            "部署", "CI", "CD", "Docker", "监控", "性能", "优化", "配置",
            "deploy", "devops", "pipeline"
        ],
        ParadigmType.TEST_EVAL: [
            "测试", "单元测试", "覆盖率", "pytest", "unittest",
            "test", "coverage", "testing"
        ],
        ParadigmType.DOC_WRITING: [
            "文档", "README", "说明", "手册", "API文档",
            "document", "readme", "manual"
        ],
        ParadigmType.GENERAL: [
            "是什么", "为什么", "怎么", "如何", "解释", "什么是",
            "what", "why", "how", "explain"
        ],
    }
    
    def recognize(self, query: str) -> RecognitionResult:
        """识别范式"""
        query_lower = query.lower()
        scores = {}
        matched = {}
        
        for paradigm, keywords in self.KEYWORDS.items():
            matches = [kw for kw in keywords if kw.lower() in query_lower]
            if matches:
                scores[paradigm] = len(matches)
                matched[paradigm] = matches
        
        if not scores:
            return RecognitionResult(
                paradigm=ParadigmType.GENERAL,
                confidence=0.5,
                reasoning="无法识别具体范式，默认为通用问答"
            )
        
        best = max(scores, key=scores.get)
        total = sum(scores.values())
        confidence = scores[best] / total if total > 0 else 0.5
        
        return RecognitionResult(
            paradigm=best,
            confidence=confidence,
            reasoning=f"关键词匹配: {', '.join(matched[best])}",
            keywords_matched=matched[best]
        )


# ========== 流程注册表 ==========

class ProcessRegistry:
    """流程注册表"""
    
    def __init__(self, storage_path: Path = None):
        self.storage_path = storage_path or Path.cwd() / ".processes"
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        self._processes: Dict[str, ProcessDefinition] = {}
        self._paradigm_map: Dict[ParadigmType, str] = {}
        
        self._register_builtin()
    
    def _register_builtin(self):
        """注册内置流程"""
        builtin = [
            ("code_development", create_code_development_process()),
            ("test_evaluation", create_test_evaluation_process()),
            ("general_qa", create_general_qa_process()),
            ("feature_design", create_feature_design_process()),
            ("engineering_practice", create_engineering_process()),
            ("documentation", create_documentation_process()),
        ]
        
        for name, process in builtin:
            self._processes[name] = process
            # 映射范式（处理 paradigm 字符串到 ParadigmType 的映射）
            paradigm_str = process.paradigm.upper() if process.paradigm else "GENERAL"
            # 尝试匹配 ParadigmType
            for pt in ParadigmType:
                if pt.value == process.paradigm or pt.name == paradigm_str:
                    self._paradigm_map[pt] = name
                    break
    
    def register(self, process: ProcessDefinition) -> bool:
        """注册流程"""
        self._processes[process.name] = process
        self._paradigm_map[ParadigmType(process.paradigm)] = process.name
        return True
    
    def get(self, name: str) -> Optional[ProcessDefinition]:
        """按名称获取流程"""
        return self._processes.get(name)
    
    def get_by_paradigm(self, paradigm: ParadigmType) -> Optional[ProcessDefinition]:
        """按范式获取流程"""
        name = self._paradigm_map.get(paradigm)
        if name:
            return self._processes.get(name)
        return None
    
    def list_all(self) -> List[Dict]:
        """列出所有流程"""
        return [
            {
                "name": p.name,
                "paradigm": p.paradigm,
                "objective": p.charter.objective,
                "phases": list(p.method.phases.keys()),
                "enabled": p.enabled,
            }
            for p in self._processes.values()
        ]


# ========== 流程编排器 ==========

class ProcessOrchestrator:
    """流程编排器：执行流程、管理状态"""
    
    def __init__(self, tool_matrix: ToolMatrix = None):
        self.tool_matrix = tool_matrix or get_tool_matrix()
    
    def execute(
        self,
        process: ProcessDefinition,
        query: str,
        context: dict = None,
        llm_client = None
    ) -> ProcessResult:
        """
        执行流程
        
        Args:
            process: 流程定义
            query: 用户查询
            context: 上下文
            llm_client: LLM客户端（用于决策）
        
        Returns:
            ProcessResult
        """
        context = context or {}
        paradigm = process.paradigm
        
        phases_results = []
        all_tool_calls = []
        all_artifacts = {}
        
        current_phase = process.get_initial_phase()
        
        while current_phase:
            phase_def = process.get_phase(current_phase)
            if not phase_def:
                break
            
            # 执行阶段
            phase_result = self._execute_phase(
                phase_def,
                paradigm,
                query,
                context,
                llm_client
            )
            
            phases_results.append(phase_result)
            all_tool_calls.extend(phase_result.tools_called)
            all_artifacts.update(phase_result.artifacts)
            
            # 检查是否失败
            if phase_result.status == PhaseStatus.FAILED:
                # 查找异常处理器
                handler = process.get_exception_handler("phase_fail")
                if handler:
                    # 尝试恢复
                    pass
                else:
                    return ProcessResult(
                        success=False,
                        paradigm=paradigm,
                        process_name=process.name,
                        phases_completed=[r.phase for r in phases_results],
                        phases_results=phases_results,
                        final_output=phase_result.output,
                        all_artifacts=all_artifacts,
                        all_tool_calls=all_tool_calls,
                        error=phase_result.error
                    )
            
            # 确定下一阶段
            if current_phase == process.get_final_phase():
                break
            
            # 简化的事件判定
            event = "confirm"  # 默认事件
            next_phase = process.transition(current_phase, event)
            
            if not next_phase:
                # 尝试其他事件
                for evt in ["execute_done", "verify_pass", "understood", "answered"]:
                    next_phase = process.transition(current_phase, evt)
                    if next_phase:
                        break
            
            current_phase = next_phase
        
        return ProcessResult(
            success=True,
            paradigm=paradigm,
            process_name=process.name,
            phases_completed=[r.phase for r in phases_results],
            phases_results=phases_results,
            final_output=phases_results[-1].output if phases_results else "",
            all_artifacts=all_artifacts,
            all_tool_calls=all_tool_calls
        )
    
    def _execute_phase(
        self,
        phase: Phase,
        paradigm: str,
        query: str,
        context: dict,
        llm_client
    ) -> PhaseResult:
        """执行单个阶段"""
        tools_called = []
        artifacts = {}
        
        # 获取可用工具
        allowed_tools = internal_list_allowed_tools(paradigm)
        phase_tools = [t for t in phase.tools if t in allowed_tools]
        
        # 构建阶段输出
        output = f"阶段 [{phase.name}] 执行完成\n"
        output += f"描述: {phase.description}\n"
        output += f"可用工具: {', '.join(phase_tools) if phase_tools else '无'}\n"
        
        if phase_tools:
            output += f"\n建议工具调用序列:\n"
            for i, tool in enumerate(phase_tools, 1):
                output += f"  {i}. {tool}\n"
        
        return PhaseResult(
            phase=phase.name,
            status=PhaseStatus.COMPLETED,
            output=output,
            tools_called=tools_called,
            artifacts=artifacts
        )


# ========== 元操作总管家 ==========

class MetaDispatcher:
    """
    元操作总管家
    
    用户唯一入口，职责：
    1. 范式识别
    2. 流程匹配
    3. 执行控制
    4. 结果聚合
    """
    
    def __init__(self, tool_registry=None):
        self.recognizer = ParadigmRecognizer()
        self.process_registry = ProcessRegistry()
        
        # 初始化工具矩阵
        if TOOL_MATRIX_AVAILABLE:
            self.tool_matrix = init_tool_matrix(tool_registry) if tool_registry else get_tool_matrix()
        else:
            self.tool_matrix = None
        
        self.orchestrator = ProcessOrchestrator(self.tool_matrix)
        
        self._sessions: Dict[str, SessionState] = {}
        self._current_session: Optional[str] = None
        self._lock = threading.RLock()
    
    def dispatch(
        self,
        query: str,
        context: dict = None,
        force_paradigm: ParadigmType = None
    ) -> DispatchResult:
        """
        主调度入口（用户唯一可调用）
        
        流程：
        1. 识别范式
        2. 匹配流程
        3. 编排执行
        4. 聚合结果
        """
        context = context or {}
        
        try:
            # 1. 范式识别
            if force_paradigm:
                recognition = RecognitionResult(
                    paradigm=force_paradigm,
                    confidence=1.0,
                    reasoning="用户强制指定"
                )
            else:
                recognition = self.recognizer.recognize(query)
            
            paradigm = recognition.paradigm
            
            # 2. 流程匹配
            process = self.process_registry.get_by_paradigm(paradigm)
            if not process:
                return DispatchResult(
                    success=False,
                    paradigm=paradigm,
                    process_name="",
                    output="",
                    error=f"未找到范式 '{paradigm.value}' 对应的流程"
                )
            
            # 3. 创建会话
            session_id = self._create_session(paradigm, process.name, context)
            
            # 4. 执行流程
            process_result = self.orchestrator.execute(process, query, context)
            
            # 5. 聚合结果
            result = DispatchResult(
                success=process_result.success,
                paradigm=paradigm,
                process_name=process.name,
                output=process_result.final_output,
                artifacts=process_result.all_artifacts,
                tool_calls=process_result.all_tool_calls,
                session_id=session_id,
                next_action=self._get_next_action(process_result),
                can_continue=not process_result.success,
                error=process_result.error
            )
            
            # 更新会话
            self._update_session(session_id, result)
            
            return result
            
        except Exception as e:
            return DispatchResult(
                success=False,
                paradigm=ParadigmType.GENERAL,
                process_name="",
                output="",
                error=f"调度异常: {str(e)}\n{traceback.format_exc()}"
            )
    
    def get_status(self, session_id: str = None) -> Dict:
        """获取会话状态"""
        sid = session_id or self._current_session
        if not sid or sid not in self._sessions:
            return {"error": "会话不存在"}
        
        session = self._sessions[sid]
        return {
            "session_id": session.session_id,
            "paradigm": session.paradigm.value,
            "process_name": session.process_name,
            "current_phase": session.current_phase,
            "artifacts": session.artifacts,
            "tool_calls_count": len(session.tool_calls_history)
        }
    
    def handover(
        self,
        target_paradigm: ParadigmType,
        reason: str,
        carry_context: bool = True
    ) -> DispatchResult:
        """移交到其他范式"""
        if not self._current_session:
            return DispatchResult(
                success=False,
                paradigm=target_paradigm,
                process_name="",
                output="",
                error="没有活动会话"
            )
        
        current = self._sessions.get(self._current_session)
        if not current:
            return DispatchResult(
                success=False,
                paradigm=target_paradigm,
                process_name="",
                output="",
                error="当前会话不存在"
            )
        
        context = current.context if carry_context else {}
        context["handover_from"] = current.paradigm.value
        context["handover_reason"] = reason
        
        return self.dispatch(
            query=f"[移交任务] {reason}",
            context=context,
            force_paradigm=target_paradigm
        )
    
    def list_processes(self) -> List[Dict]:
        """列出所有可用流程"""
        return self.process_registry.list_all()
    
    def list_tools(self, paradigm: str = None) -> List[str]:
        """列出可用工具"""
        if self.tool_matrix:
            if paradigm:
                return self.tool_matrix.get_allowed_tools(paradigm)
            return self.tool_matrix.list_all_tools()
        return []
    
    def _create_session(
        self,
        paradigm: ParadigmType,
        process_name: str,
        context: dict
    ) -> str:
        """创建会话"""
        session_id = str(uuid.uuid4())[:8]
        
        session = SessionState(
            session_id=session_id,
            paradigm=paradigm,
            process_name=process_name,
            current_phase="init",
            phase_status={},
            context=context,
            artifacts={},
            tool_calls_history=[],
            created_at=time.time(),
            updated_at=time.time()
        )
        
        with self._lock:
            self._sessions[session_id] = session
            self._current_session = session_id
        
        return session_id
    
    def _update_session(self, session_id: str, result: DispatchResult):
        """更新会话"""
        with self._lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                session.artifacts.update(result.artifacts)
                session.tool_calls_history.extend(result.tool_calls)
                session.updated_at = time.time()
    
    def _get_next_action(self, result: ProcessResult) -> Optional[str]:
        """获取下一步建议"""
        if result.success:
            return "流程已完成"
        elif result.error:
            return f"需要处理错误: {result.error[:100]}"
        return None


# ========== 全局实例 ==========

_dispatcher: Optional[MetaDispatcher] = None


def get_dispatcher() -> MetaDispatcher:
    """获取全局调度器"""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = MetaDispatcher()
    return _dispatcher


def init_dispatcher(tool_registry) -> MetaDispatcher:
    """初始化调度器"""
    global _dispatcher
    _dispatcher = MetaDispatcher(tool_registry)
    return _dispatcher


# ========== 工具函数（用户层唯一接口） ==========

def run_meta_dispatch(
    query: str,
    context: dict = None,
    force_paradigm: str = None
) -> Tuple[str, Optional[str]]:
    """
    元操作总调度（用户唯一入口）
    
    这是用户可以调用的唯一工具！
    其他所有工具都通过此接口间接调用。
    """
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
            "success": result.success,
            "paradigm": result.paradigm.value,
            "process": result.process_name,
            "output": result.output,
            "artifacts": result.artifacts,
            "tool_calls_count": len(result.tool_calls),
            "session_id": result.session_id,
            "next_action": result.next_action,
        }
        
        if result.error:
            output["error"] = result.error
        
        return json.dumps(output, indent=2, ensure_ascii=False), None
        
    except Exception as e:
        return "", f"调度失败: {str(e)}\n{traceback.format_exc()}"


def run_meta_status(session_id: str = None) -> Tuple[str, Optional[str]]:
    """查询会话状态"""
    try:
        dispatcher = get_dispatcher()
        status = dispatcher.get_status(session_id)
        return json.dumps(status, indent=2, ensure_ascii=False), None
    except Exception as e:
        return "", f"查询失败: {str(e)}"


def run_meta_handover(
    target_paradigm: str,
    reason: str,
    carry_context: bool = True
) -> Tuple[str, Optional[str]]:
    """移交到其他范式"""
    try:
        dispatcher = get_dispatcher()
        target_p = ParadigmType(target_paradigm.lower())
        result = dispatcher.handover(target_p, reason, carry_context)
        
        return json.dumps({
            "success": result.success,
            "new_paradigm": result.paradigm.value,
            "output": result.output
        }, indent=2, ensure_ascii=False), None
    except Exception as e:
        return "", f"移交失败: {str(e)}"


def run_meta_list() -> Tuple[str, Optional[str]]:
    """列出所有可用流程和工具"""
    try:
        dispatcher = get_dispatcher()
        processes = dispatcher.list_processes()
        tools = dispatcher.list_tools()
        
        return json.dumps({
            "processes": processes,
            "total_tools": len(tools),
            "user_visible_tools": ["meta_dispatch"]  # 用户唯一可见的工具
        }, indent=2, ensure_ascii=False), None
    except Exception as e:
        return "", f"列表失败: {str(e)}"


# ========== 测试 ==========

if __name__ == "__main__":
    print("=== 元操作总管家测试 ===\n")
    
    dispatcher = MetaDispatcher()
    
    # 测试查询
    test_queries = [
        "帮我实现一个用户登录功能",
        "设计一个电商系统架构",
        "编写单元测试",
        "什么是Python的GIL？"
    ]
    
    for query in test_queries:
        print(f"\nQuery: {query}")
        result = dispatcher.dispatch(query)
        print(f"范式: {result.paradigm.value}")
        print(f"流程: {result.process_name}")
        print(f"输出预览: {result.output[:100]}...")
        print("-" * 50)
    
    # 列出流程
    print("\n可用流程:")
    for p in dispatcher.list_processes():
        print(f"  - {p['name']} ({p['paradigm']}): {p['phases']}")
    
    print("\n=== 测试完成 ===")
