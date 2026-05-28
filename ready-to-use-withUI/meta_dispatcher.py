#!/usr/bin/env python3
"""
元操作总管家 v3.0 - 真正驱动 Workflow 执行

核心改进：
1. meta_dispatch 不仅返回信息，还创建 workflow session 并返回第一步行指令
2. 新增 meta_step 推进 workflow 执行
3. 与现有 WorkflowManager 深度集成
4. 每个阶段都有具体的工具调用指令

执行流程：
1. meta_dispatch → 识别范式，创建 session，返回第一阶段指令
2. LLM 根据指令调用具体工具
3. meta_step → 推进到下一阶段，返回下一阶段指令
4. 重复 2-3 直到 DONE
"""

import os
import json
import time
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Callable
from dataclasses import dataclass, field, asdict
from enum import Enum
import traceback

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
class ToolCallInstruction:
    """工具调用指令"""
    tool_name: str
    parameters: Dict[str, Any]
    description: str
    required: bool = True


@dataclass
class PhaseInstruction:
    """阶段执行指令"""
    phase: str
    description: str
    objective: str
    tools_to_call: List[ToolCallInstruction]
    expected_output: str
    quality_gate: Optional[str] = None


@dataclass 
class DispatchResult:
    """调度结果"""
    success: bool
    paradigm: ParadigmType
    process_name: str
    session_id: str
    
    # 当前阶段信息
    current_phase: str
    phase_instruction: Optional[PhaseInstruction]
    
    # 执行状态
    phases_completed: List[str]
    phases_pending: List[str]
    
    # 输出
    output: str
    artifacts: Dict[str, str]
    tool_calls_history: List[Dict]
    
    # 控制标志
    can_continue: bool
    is_done: bool
    next_action: Optional[str]
    
    error: Optional[str] = None


@dataclass
class StepResult:
    """步骤推进结果"""
    success: bool
    session_id: str
    
    # 阶段信息
    previous_phase: str
    current_phase: str
    phase_instruction: Optional[PhaseInstruction]
    
    # 执行结果
    tool_calls_made: List[Dict]
    output: str
    artifacts: Dict[str, str]
    
    # 控制标志
    can_continue: bool
    is_done: bool
    next_action: Optional[str]
    
    error: Optional[str] = None


@dataclass
class SessionState:
    """会话状态"""
    session_id: str
    paradigm: ParadigmType
    process_name: str
    
    # workflow 状态
    current_phase: str
    phase_artifacts: Dict[str, Dict[str, str]]
    phase_status: Dict[str, str]
    
    # 执行历史
    tool_calls_history: List[Dict]
    context: Dict
    
    # 时间戳
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
    
    def recognize(self, query: str) -> Tuple[ParadigmType, float, str]:
        """识别范式，返回 (范式, 置信度, 推理)"""
        query_lower = query.lower()
        scores = {}
        matched = {}
        
        for paradigm, keywords in self.KEYWORDS.items():
            matches = [kw for kw in keywords if kw.lower() in query_lower]
            if matches:
                scores[paradigm] = len(matches)
                matched[paradigm] = matches
        
        if not scores:
            return ParadigmType.GENERAL, 0.5, "无法识别具体范式，默认为通用问答"
        
        best = max(scores, key=scores.get)
        total = sum(scores.values())
        confidence = scores[best] / total if total > 0 else 0.5
        
        return best, confidence, f"关键词匹配: {', '.join(matched[best])}"


# ========== 流程注册表 ==========

class ProcessRegistry:
    """流程注册表"""
    
    def __init__(self):
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
            for pt in ParadigmType:
                if pt.value == process.paradigm or pt.name == process.paradigm.upper():
                    self._paradigm_map[pt] = name
                    break
    
    def get_by_paradigm(self, paradigm: ParadigmType) -> Optional[ProcessDefinition]:
        """按范式获取流程"""
        name = self._paradigm_map.get(paradigm)
        return self._processes.get(name) if name else None
    
    def get(self, name: str) -> Optional[ProcessDefinition]:
        """按名称获取流程"""
        return self._processes.get(name)


# ========== 阶段指令生成器 ==========

class PhaseInstructionGenerator:
    """阶段指令生成器：为每个阶段生成具体的工具调用指令"""
    
    @staticmethod
    def generate(
        phase: Phase,
        paradigm: str,
        query: str,
        context: dict,
        allowed_tools: List[str]
    ) -> PhaseInstruction:
        """生成阶段执行指令"""
        
        # 根据阶段类型生成具体指令
        instructions = {
            # CODE_DEV 流程
            "ARCH": PhaseInstruction(
                phase="ARCH",
                description="架构设计阶段",
                objective="分析需求，设计整体架构和技术选型",
                tools_to_call=[
                    ToolCallInstruction("read_file", {"path": "<项目主文件>"}, "阅读现有代码了解项目结构", False),
                    ToolCallInstruction("bash", {"command": "ls -la"}, "查看项目目录结构", False),
                ],
                expected_output="架构设计文档，包括模块划分、技术选型、接口定义",
                quality_gate="用户确认架构方案"
            ),
            "REQ": PhaseInstruction(
                phase="REQ",
                description="需求分析阶段",
                objective="详细分析需求，输出验收标准和功能列表",
                tools_to_call=[
                    ToolCallInstruction("write_file", {"path": "requirements.md", "content": "<需求文档>"}, "编写需求文档", True),
                ],
                expected_output="需求文档，包括功能列表、验收标准、边界条件",
                quality_gate="用户确认需求"
            ),
            "DESIGN": PhaseInstruction(
                phase="DESIGN",
                description="详细设计阶段",
                objective="输出可执行的详细设计和实现计划",
                tools_to_call=[
                    ToolCallInstruction("write_file", {"path": "design.md", "content": "<设计文档>"}, "编写设计文档", True),
                    ToolCallInstruction("todo", {"items": "<任务列表>"}, "创建实现任务清单", True),
                ],
                expected_output="设计文档和实现计划，等待用户固化确认",
                quality_gate="用户输入'固化'确认"
            ),
            "EXEC": PhaseInstruction(
                phase="EXEC",
                description="执行开发阶段",
                objective="按照设计实现代码",
                tools_to_call=[
                    ToolCallInstruction("worktree_create", {"name": "feature-<name>", "task_id": None}, "创建独立工作树", False),
                    ToolCallInstruction("write_file", {"path": "<代码文件>", "content": "<代码>"}, "编写代码文件", True),
                    ToolCallInstruction("edit_file", {"path": "<文件>", "old_text": "<旧内容>", "new_text": "<新内容>"}, "修改现有代码", False),
                ],
                expected_output="实现的代码文件",
                quality_gate="代码编写完成"
            ),
            "VERIFY": PhaseInstruction(
                phase="VERIFY",
                description="验证测试阶段",
                objective="运行测试，验证实现是否正确",
                tools_to_call=[
                    ToolCallInstruction("bash", {"command": "python -m pytest"}, "运行测试", True),
                    ToolCallInstruction("background_run", {"command": "<长时间测试>"}, "后台运行测试", False),
                ],
                expected_output="测试通过，覆盖率达标",
                quality_gate="测试通过且覆盖率 >= 80%"
            ),
            "REFINE": PhaseInstruction(
                phase="REFINE",
                description="修正问题阶段",
                objective="修复测试发现的问题",
                tools_to_call=[
                    ToolCallInstruction("edit_file", {"path": "<文件>", "old_text": "<问题代码>", "new_text": "<修正代码>"}, "修正代码", True),
                ],
                expected_output="问题修正后重新通过测试",
                quality_gate="修正完成"
            ),
            
            # TEST_EVAL 流程
            "PLAN": PhaseInstruction(
                phase="PLAN",
                description="测试计划阶段",
                objective="制定测试计划和策略",
                tools_to_call=[
                    ToolCallInstruction("read_file", {"path": "<被测模块>"}, "阅读被测代码", True),
                    ToolCallInstruction("write_file", {"path": "test_plan.md", "content": "<测试计划>"}, "编写测试计划", True),
                ],
                expected_output="测试计划文档",
                quality_gate="用户确认测试计划"
            ),
            "DESIGN_TEST": PhaseInstruction(
                phase="DESIGN_TEST",
                description="测试用例设计阶段",
                objective="设计详细的测试用例",
                tools_to_call=[
                    ToolCallInstruction("write_file", {"path": "test_<module>.py", "content": "<测试代码>"}, "编写测试用例", True),
                ],
                expected_output="测试用例文件",
                quality_gate="用例设计完成"
            ),
            "EXEC_TEST": PhaseInstruction(
                phase="EXEC_TEST",
                description="执行测试阶段",
                objective="运行测试并收集结果",
                tools_to_call=[
                    ToolCallInstruction("bash", {"command": "python -m pytest test_<module>.py -v"}, "执行测试", True),
                ],
                expected_output="测试执行结果",
                quality_gate="测试执行完成"
            ),
            "REPORT": PhaseInstruction(
                phase="REPORT",
                description="报告生成阶段",
                objective="生成测试报告",
                tools_to_call=[
                    ToolCallInstruction("write_file", {"path": "test_report.md", "content": "<测试报告>"}, "编写测试报告", True),
                ],
                expected_output="测试报告",
                quality_gate="报告完成"
            ),
            
            # GENERAL 流程
            "UNDERSTAND": PhaseInstruction(
                phase="UNDERSTAND",
                description="理解问题阶段",
                objective="理解用户问题并收集相关信息",
                tools_to_call=[
                    ToolCallInstruction("read_file", {"path": "<相关文件>"}, "阅读相关文件了解背景", False),
                ],
                expected_output="问题理解完成",
                quality_gate="问题理解"
            ),
            "ANSWER": PhaseInstruction(
                phase="ANSWER",
                description="回答问题阶段",
                objective="生成答案",
                tools_to_call=[],
                expected_output="回答内容",
                quality_gate="回答完成"
            ),
            
            # 意图管理阶段
            "INTENT_CLARIFICATION": PhaseInstruction(
                phase="INTENT_CLARIFICATION",
                description="意图澄清阶段",
                objective="明确用户的最终目的，确保后续执行方向正确",
                tools_to_call=[
                    ToolCallInstruction("register_intent", {
                        "session_id": "<session_id>",
                        "primary_goals": ["<主要目标>"],
                        "secondary_goals": ["<次要目标>"],
                        "constraints": ["<约束条件>"]
                    }, "【必须】注册用户的最终目的", True),
                    ToolCallInstruction("clarify_intent", {
                        "session_id": "<session_id>",
                        "question": "<需要澄清的问题>"
                    }, "当不确定用户意图时，向用户提问", False),
                ],
                expected_output="用户的明确目标、次要目标和约束条件",
                quality_gate="用户意图已确认"
            ),
            
            # 通用阶段
            "DONE": PhaseInstruction(
                phase="DONE",
                description="完成阶段",
                objective="任务完成",
                tools_to_call=[],
                expected_output="任务已完成",
                quality_gate=None
            ),
        }
        
        # 获取阶段指令，如果未定义则生成通用指令
        instruction = instructions.get(phase.name)
        if not instruction:
            instruction = PhaseInstruction(
                phase=phase.name,
                description=phase.description or f"{phase.name}阶段",
                objective="执行阶段任务",
                tools_to_call=[
                    ToolCallInstruction(tool, {}, f"调用 {tool} 工具", False)
                    for tool in phase.tools[:3] if tool in allowed_tools
                ],
                expected_output="阶段输出",
                quality_gate=None
            )
        
        return instruction


# ========== 会话管理器 ==========

class SessionManager:
    """会话状态管理器"""
    
    def __init__(self, storage_path: Path = None):
        self.storage_path = storage_path or Path.cwd() / ".meta_sessions"
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        self._sessions: Dict[str, SessionState] = {}
        self._lock = threading.RLock()
    
    def create(
        self,
        paradigm: ParadigmType,
        process_name: str,
        initial_phase: str,
        context: dict = None
    ) -> str:
        """创建新会话"""
        session_id = str(uuid.uuid4())[:8]
        
        session = SessionState(
            session_id=session_id,
            paradigm=paradigm,
            process_name=process_name,
            current_phase=initial_phase,
            phase_artifacts={},
            phase_status={},
            tool_calls_history=[],
            context=context or {},
            created_at=time.time(),
            updated_at=time.time()
        )
        
        with self._lock:
            self._sessions[session_id] = session
        
        self._save_session(session)
        return session_id
    
    def get(self, session_id: str) -> Optional[SessionState]:
        """获取会话"""
        with self._lock:
            return self._sessions.get(session_id)
    
    def update(self, session_id: str, **updates):
        """更新会话"""
        with self._lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                for key, value in updates.items():
                    if hasattr(session, key):
                        setattr(session, key, value)
                session.updated_at = time.time()
                self._save_session(session)
    
    def add_tool_call(self, session_id: str, tool_call: dict):
        """添加工具调用记录"""
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].tool_calls_history.append(tool_call)
                self._sessions[session_id].updated_at = time.time()
    
    def _save_session(self, session: SessionState):
        """保存会话到文件"""
        session_file = self.storage_path / f"{session.session_id}.json"
        data = {
            "session_id": session.session_id,
            "paradigm": session.paradigm.value,
            "process_name": session.process_name,
            "current_phase": session.current_phase,
            "phase_artifacts": session.phase_artifacts,
            "phase_status": session.phase_status,
            "tool_calls_history": session.tool_calls_history,
            "context": session.context,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }
        session_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ========== 元操作总管家核心 ==========

class MetaDispatcherCore:
    """
    元操作总管家核心
    
    关键改进：
    1. dispatch 返回第一阶段指令，引导 LLM 调用具体工具
    2. step 推进 workflow，返回下一阶段指令
    3. 与 LLM 形成闭环：指令 → 工具调用 → step推进 → 新指令
    """
    
    def __init__(self, tool_registry=None):
        self.recognizer = ParadigmRecognizer()
        self.process_registry = ProcessRegistry()
        self.session_manager = SessionManager()
        self.instruction_generator = PhaseInstructionGenerator()
        
        if TOOL_MATRIX_AVAILABLE:
            self.tool_matrix = init_tool_matrix(tool_registry) if tool_registry else get_tool_matrix()
        else:
            self.tool_matrix = None
    
    def dispatch(
        self,
        query: str,
        context: dict = None,
        force_paradigm: ParadigmType = None
    ) -> DispatchResult:
        """
        主调度入口
        
        返回第一阶段指令，引导 LLM 开始执行 workflow
        """
        context = context or {}
        
        try:
            # 1. 识别范式
            if force_paradigm:
                paradigm = force_paradigm
                confidence = 1.0
                reasoning = "用户强制指定"
            else:
                paradigm, confidence, reasoning = self.recognizer.recognize(query)
            
            # 2. 获取流程定义
            process = self.process_registry.get_by_paradigm(paradigm)
            if not process:
                return DispatchResult(
                    success=False,
                    paradigm=paradigm,
                    process_name="",
                    session_id="",
                    current_phase="",
                    phase_instruction=None,
                    phases_completed=[],
                    phases_pending=[],
                    output="",
                    artifacts={},
                    tool_calls_history=[],
                    can_continue=False,
                    is_done=False,
                    next_action=None,
                    error=f"未找到范式 '{paradigm.value}' 对应的流程"
                )
            
            # 3. 获取初始阶段
            initial_phase = process.get_initial_phase()
            if not initial_phase:
                return DispatchResult(
                    success=False,
                    paradigm=paradigm,
                    process_name=process.name,
                    session_id="",
                    current_phase="",
                    phase_instruction=None,
                    phases_completed=[],
                    phases_pending=[],
                    output="",
                    artifacts={},
                    tool_calls_history=[],
                    can_continue=False,
                    is_done=False,
                    next_action=None,
                    error="流程没有定义阶段"
                )
            
            # 4. 创建会话
            session_id = self.session_manager.create(
                paradigm=paradigm,
                process_name=process.name,
                initial_phase=initial_phase,
                context={"query": query, **context}
            )
            
            # 5. 检查置信度，决定是否需要意图澄清
            CONFIDENCE_THRESHOLD = 0.6
            needs_intent_clarification = confidence < CONFIDENCE_THRESHOLD
            
            if needs_intent_clarification:
                # 低置信度：返回意图澄清引导
                phase_instruction = PhaseInstruction(
                    phase="INTENT_CLARIFICATION",
                    description="意图澄清阶段",
                    objective="明确用户的最终目的，消除歧义",
                    tools_to_call=[
                        ToolCallInstruction("clarify_intent", {
                            "session_id": session_id,
                            "question": "<需要澄清的问题>"
                        }, "当不确定用户意图时，生成澄清问题", False),
                        ToolCallInstruction("register_intent", {
                            "session_id": session_id,
                            "primary_goals": ["<主要目标>"]
                        }, "【必须】注册用户最终目的", True),
                    ],
                    expected_output="用户的明确目标和约束条件",
                    quality_gate="用户意图已确认并注册"
                )
                
                output = f"""[意图澄清引导]

范式识别置信度较低 ({confidence:.0%})，建议先明确用户意图。

{reasoning}

【推荐操作】
1. 先调用 register_intent 注册您理解的用户目标
2. 如果不确定，调用 clarify_intent 向用户提问
3. 确认意图后再执行具体任务

会话ID: {session_id}
识别的范式: {paradigm.value}

提示：准确的意图注册能确保后续所有操作都符合用户初衷。
"""
                return DispatchResult(
                    success=True,
                    paradigm=paradigm,
                    process_name=process.name,
                    session_id=session_id,
                    current_phase="INTENT_CLARIFICATION",
                    phase_instruction=phase_instruction,
                    phases_completed=[],
                    phases_pending=["INTENT_CLARIFICATION"] + process.get_all_phases(),
                    output=output,
                    artifacts={},
                    tool_calls_history=[],
                    can_continue=True,
                    is_done=False,
                    next_action="请先调用 register_intent 注册用户意图，然后调用 meta_step 推进到下一阶段"
                )
            
            # 6. 生成第一阶段指令（高置信度路径）
            phase_def = process.get_phase(initial_phase)
            allowed_tools = self.tool_matrix.get_allowed_tools(paradigm.value) if self.tool_matrix else []
            
            phase_instruction = self.instruction_generator.generate(
                phase=phase_def,
                paradigm=paradigm.value,
                query=query,
                context=context,
                allowed_tools=allowed_tools
            )
            
            # 7. 在指令中添加意图注册提示
            intent_hint = """
【重要提示】在开始执行前，请先调用 register_intent 注册用户意图：
- primary_goals: 用户的主要目标
- constraints: 用户的约束条件（如有）

这能确保后续操作符合用户初衷，避免做无用功。
"""
            phase_instruction.tools_to_call.insert(0, ToolCallInstruction(
                "register_intent",
                {"session_id": session_id, "primary_goals": ["<用户主要目标>"]},
                "【推荐】注册用户意图，确保执行方向正确",
                False  # 非必须，但强烈推荐
            ))
            
            # 8. 构建输出
            output = self._format_phase_output(phase_instruction, reasoning, session_id)
            output = intent_hint + output
            
            return DispatchResult(
                success=True,
                paradigm=paradigm,
                process_name=process.name,
                session_id=session_id,
                current_phase=initial_phase,
                phase_instruction=phase_instruction,
                phases_completed=[],
                phases_pending=process.get_all_phases(),
                output=output,
                artifacts={},
                tool_calls_history=[],
                can_continue=True,
                is_done=False,
                next_action=f"请按照上述指令执行 {initial_phase} 阶段任务，完成后调用 meta_step 推进"
            )
            
        except Exception as e:
            return DispatchResult(
                success=False,
                paradigm=ParadigmType.GENERAL,
                process_name="",
                session_id="",
                current_phase="",
                phase_instruction=None,
                phases_completed=[],
                phases_pending=[],
                output="",
                artifacts={},
                tool_calls_history=[],
                can_continue=False,
                is_done=False,
                next_action=None,
                error=f"调度异常: {str(e)}\n{traceback.format_exc()}"
            )
    
    def step(
        self,
        session_id: str,
        event: str = "confirm",
        artifact: str = "",
        tool_calls_made: List[Dict] = None
    ) -> StepResult:
        """
        推进 workflow 到下一阶段
        
        Args:
            session_id: 会话ID
            event: 触发事件 (confirm, execute_done, verify_pass, verify_fail 等)
            artifact: 当前阶段的产出
            tool_calls_made: 当前阶段已执行的工具调用
        
        Returns:
            StepResult 包含下一阶段指令
        """
        tool_calls_made = tool_calls_made or []
        
        try:
            # 1. 获取会话
            session = self.session_manager.get(session_id)
            if not session:
                return StepResult(
                    success=False,
                    session_id=session_id,
                    previous_phase="",
                    current_phase="",
                    phase_instruction=None,
                    tool_calls_made=[],
                    output="",
                    artifacts={},
                    can_continue=False,
                    is_done=False,
                    next_action=None,
                    error=f"会话 {session_id} 不存在"
                )
            
            # 2. 获取流程定义
            process = self.process_registry.get(session.process_name)
            if not process:
                return StepResult(
                    success=False,
                    session_id=session_id,
                    previous_phase=session.current_phase,
                    current_phase="",
                    phase_instruction=None,
                    tool_calls_made=[],
                    output="",
                    artifacts={},
                    can_continue=False,
                    is_done=False,
                    next_action=None,
                    error="流程定义不存在"
                )
            
            previous_phase = session.current_phase
            
            # 3. 记录工具调用
            for tc in tool_calls_made:
                self.session_manager.add_tool_call(session_id, tc)
            
            # 4. 执行状态转移
            next_phase = process.transition(previous_phase, event)
            
            # 如果事件无效，尝试智能匹配
            if not next_phase:
                # 获取该阶段所有可能的事件
                possible_events = []
                if previous_phase in process._state_map:
                    possible_events = list(process._state_map[previous_phase].keys())
                
                # 智能事件推断
                if event == "confirm" and possible_events:
                    # confirm 是通用确认事件，尝试匹配最合适的
                    if "approve" in possible_events:
                        # REVIEW 阶段通常用 approve
                        next_phase = process.transition(previous_phase, "approve")
                        event_used = "approve"
                    elif possible_events:
                        # 使用第一个可用事件
                        first_event = possible_events[0]
                        next_phase = process.transition(previous_phase, first_event)
                        event_used = first_event
                    else:
                        event_used = None
                else:
                    event_used = None
                
                # 如果成功推断，记录日志
                if next_phase and event != event_used:
                    import warnings
                    warnings.warn(f"[智能推断] 阶段 {previous_phase} 不接受事件 '{event}'，自动使用 '{event_used}'")
            
            if not next_phase:
                # 无法转移，提供可用事件列表
                available_events = list(process._state_map.get(previous_phase, {}).keys())
                available_hint = f"可用事件: {', '.join(available_events)}" if available_events else "该阶段无法转移"
                
                return StepResult(
                    success=False,
                    session_id=session_id,
                    previous_phase=previous_phase,
                    current_phase=previous_phase,
                    phase_instruction=None,
                    tool_calls_made=tool_calls_made,
                    output=f"事件 '{event}' 在阶段 '{previous_phase}' 无效。\n{available_hint}\n\n提示：如果是 REVIEW 阶段，请使用 'approve' 表示通过。",
                    artifacts={},
                    can_continue=True,
                    is_done=False,
                    next_action=f"可用事件: {available_events}"
                )
            
            # 5. 更新会话
            self.session_manager.update(session_id, current_phase=next_phase)
            
            # 6. 检查是否完成
            is_done = (next_phase == process.get_final_phase())
            
            # 7. 生成下一阶段指令
            phase_def = process.get_phase(next_phase)
            allowed_tools = self.tool_matrix.get_allowed_tools(session.paradigm.value) if self.tool_matrix else []
            
            phase_instruction = self.instruction_generator.generate(
                phase=phase_def,
                paradigm=session.paradigm.value,
                query=session.context.get("query", ""),
                context=session.context,
                allowed_tools=allowed_tools
            )
            
            # 8. 构建输出
            output = self._format_step_output(
                previous_phase, next_phase, phase_instruction, is_done
            )
            
            return StepResult(
                success=True,
                session_id=session_id,
                previous_phase=previous_phase,
                current_phase=next_phase,
                phase_instruction=phase_instruction,
                tool_calls_made=tool_calls_made,
                output=output,
                artifacts={},
                can_continue=not is_done,
                is_done=is_done,
                next_action=None if is_done else f"请执行 {next_phase} 阶段任务，完成后调用 meta_step"
            )
            
        except Exception as e:
            return StepResult(
                success=False,
                session_id=session_id,
                previous_phase="",
                current_phase="",
                phase_instruction=None,
                tool_calls_made=[],
                output="",
                artifacts={},
                can_continue=False,
                is_done=False,
                next_action=None,
                error=f"推进异常: {str(e)}\n{traceback.format_exc()}"
            )
    
    def get_status(self, session_id: str) -> Dict:
        """获取会话状态"""
        session = self.session_manager.get(session_id)
        if not session:
            return {"error": f"会话 {session_id} 不存在"}
        
        process = self.process_registry.get(session.process_name)
        
        return {
            "session_id": session.session_id,
            "paradigm": session.paradigm.value,
            "process": session.process_name,
            "current_phase": session.current_phase,
            "phases_completed": [],  # 可以从 phase_status 计算
            "phases_pending": process.get_all_phases() if process else [],
            "tool_calls_count": len(session.tool_calls_history),
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }
    
    def list_processes(self) -> List[Dict]:
        """列出所有流程"""
        return [
            {"name": p.name, "paradigm": p.paradigm, "phases": list(p.method.phases.keys())}
            for p in [self.process_registry.get_by_paradigm(pt) for pt in ParadigmType]
            if p
        ]
    
    def _format_phase_output(
        self,
        instruction: PhaseInstruction,
        reasoning: str,
        session_id: str
    ) -> str:
        """格式化阶段输出"""
        lines = [
            f"📋 **Workflow 已启动**",
            f"",
            f"**会话ID**: `{session_id}`",
            f"**当前阶段**: {instruction.phase}",
            f"**阶段目标**: {instruction.objective}",
            f"",
            f"**阶段描述**: {instruction.description}",
            f"",
            f"**预期输出**: {instruction.expected_output}",
        ]
        
        if instruction.tools_to_call:
            lines.append("")
            lines.append("**建议调用的工具**:")
            for i, tool in enumerate(instruction.tools_to_call, 1):
                required = "必需" if tool.required else "可选"
                lines.append(f"  {i}. `{tool.tool_name}` ({required}) - {tool.description}")
        
        if instruction.quality_gate:
            lines.append("")
            lines.append(f"**质量门禁**: {instruction.quality_gate}")
        
        lines.append("")
        lines.append("---")
        lines.append(f"💡 **下一步**: 按照上述指令执行，完成后调用 `meta_step` 推进到下一阶段")
        
        return "\n".join(lines)
    
    def _format_step_output(
        self,
        previous_phase: str,
        current_phase: str,
        instruction: PhaseInstruction,
        is_done: bool
    ) -> str:
        """格式化步骤输出"""
        if is_done:
            return f"✅ **Workflow 完成**\n\n所有阶段已执行完毕。"
        
        lines = [
            f"✅ **阶段完成**: {previous_phase} → {current_phase}",
            f"",
            f"**当前阶段**: {instruction.phase}",
            f"**阶段目标**: {instruction.objective}",
            f"",
            f"**预期输出**: {instruction.expected_output}",
        ]
        
        if instruction.tools_to_call:
            lines.append("")
            lines.append("**建议调用的工具**:")
            for i, tool in enumerate(instruction.tools_to_call, 1):
                required = "必需" if tool.required else "可选"
                lines.append(f"  {i}. `{tool.tool_name}` ({required}) - {tool.description}")
        
        if instruction.quality_gate:
            lines.append("")
            lines.append(f"**质量门禁**: {instruction.quality_gate}")
        
        lines.append("")
        lines.append("---")
        lines.append(f"💡 **下一步**: 执行当前阶段任务，完成后调用 `meta_step` 推进")
        
        return "\n".join(lines)


# ========== 全局实例 ==========

_dispatcher: Optional[MetaDispatcherCore] = None


def get_dispatcher() -> MetaDispatcherCore:
    """获取全局调度器"""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = MetaDispatcherCore()
    return _dispatcher


def init_dispatcher(tool_registry) -> MetaDispatcherCore:
    """初始化调度器"""
    global _dispatcher
    _dispatcher = MetaDispatcherCore(tool_registry)
    return _dispatcher


# ========== 工具函数（用户层接口） ==========

def run_meta_dispatch(
    query: str,
    context: dict = None,
    force_paradigm: str = None
) -> Tuple[str, Optional[str]]:
    """
    元操作总调度 - 用户唯一入口
    
    启动 workflow 并返回第一阶段执行指令
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
        
        # 转换 phase_instruction 为可序列化格式
        phase_inst = None
        if result.phase_instruction:
            phase_inst = {
                "phase": result.phase_instruction.phase,
                "description": result.phase_instruction.description,
                "objective": result.phase_instruction.objective,
                "tools_to_call": [
                    {
                        "tool_name": tc.tool_name,
                        "parameters": tc.parameters,
                        "description": tc.description,
                        "required": tc.required,
                    }
                    for tc in result.phase_instruction.tools_to_call
                ],
                "expected_output": result.phase_instruction.expected_output,
                "quality_gate": result.phase_instruction.quality_gate,
            }
        
        output = {
            "success": result.success,
            "session_id": result.session_id,
            "paradigm": result.paradigm.value,
            "process": result.process_name,
            "current_phase": result.current_phase,
            "phase_instruction": phase_inst,
            "output": result.output,
            "can_continue": result.can_continue,
            "is_done": result.is_done,
            "next_action": result.next_action,
        }
        
        if result.error:
            output["error"] = result.error
        
        return json.dumps(output, indent=2, ensure_ascii=False), None
        
    except Exception as e:
        return "", f"调度失败: {str(e)}\n{traceback.format_exc()}"


def run_meta_step(
    session_id: str,
    event: str = "confirm",
    artifact: str = "",
    tool_calls: List[Dict] = None
) -> Tuple[str, Optional[str]]:
    """
    推进 workflow 到下一阶段
    
    在执行完当前阶段的工具调用后，调用此函数推进
    """
    try:
        dispatcher = get_dispatcher()
        result = dispatcher.step(session_id, event, artifact, tool_calls or [])
        
        phase_inst = None
        if result.phase_instruction:
            phase_inst = {
                "phase": result.phase_instruction.phase,
                "description": result.phase_instruction.description,
                "objective": result.phase_instruction.objective,
                "tools_to_call": [
                    {
                        "tool_name": tc.tool_name,
                        "parameters": tc.parameters,
                        "description": tc.description,
                        "required": tc.required,
                    }
                    for tc in result.phase_instruction.tools_to_call
                ],
            }
        
        output = {
            "success": result.success,
            "session_id": result.session_id,
            "previous_phase": result.previous_phase,
            "current_phase": result.current_phase,
            "phase_instruction": phase_inst,
            "output": result.output,
            "can_continue": result.can_continue,
            "is_done": result.is_done,
            "next_action": result.next_action,
        }
        
        if result.error:
            output["error"] = result.error
        
        return json.dumps(output, indent=2, ensure_ascii=False), None
        
    except Exception as e:
        return "", f"推进失败: {str(e)}\n{traceback.format_exc()}"


def run_meta_status(session_id: str = None) -> Tuple[str, Optional[str]]:
    """查询会话状态"""
    try:
        dispatcher = get_dispatcher()
        status = dispatcher.get_status(session_id)
        return json.dumps(status, indent=2, ensure_ascii=False), None
    except Exception as e:
        return "", f"查询失败: {str(e)}"


def run_meta_list() -> Tuple[str, Optional[str]]:
    """列出所有流程"""
    try:
        dispatcher = get_dispatcher()
        processes = dispatcher.list_processes()
        return json.dumps({
            "processes": processes,
            "user_tools": ["meta_dispatch", "meta_step", "meta_status", "meta_list"]
        }, indent=2, ensure_ascii=False), None
    except Exception as e:
        return "", f"列表失败: {str(e)}"


# ========== 测试 ==========

if __name__ == "__main__":
    print("=== 元操作总管家 v3.0 测试 ===\n")
    
    dispatcher = MetaDispatcherCore()
    
    # 测试1: 启动 workflow
    print("【测试1】启动代码开发 workflow")
    result = dispatcher.dispatch("帮我实现一个用户登录功能")
    print(f"会话ID: {result.session_id}")
    print(f"范式: {result.paradigm.value}")
    print(f"当前阶段: {result.current_phase}")
    print(f"输出预览:\n{result.output[:300]}...\n")
    
    # 测试2: 推进 workflow
    if result.success:
        print("【测试2】推进到下一阶段")
        step_result = dispatcher.step(result.session_id, "confirm")
        print(f"上一阶段: {step_result.previous_phase}")
        print(f"当前阶段: {step_result.current_phase}")
        print(f"是否完成: {step_result.is_done}")
        print()
    
    # 测试3: 继续推进
    if step_result.success and not step_result.is_done:
        print("【测试3】继续推进")
        step_result2 = dispatcher.step(result.session_id, "confirm")
        print(f"当前阶段: {step_result2.current_phase}")
        print()
    
    print("=== 测试完成 ===")
