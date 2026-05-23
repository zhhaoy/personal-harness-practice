#!/usr/bin/env python3
"""
流程定义模块 (Process Definition)
定义流程的章程、方法、机制
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import copy

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    yaml = None
    YAML_AVAILABLE = False


class PhaseStatus(Enum):
    """阶段状态"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Charter:
    """章程：流程的目标、输入输出、约束"""
    objective: str = ""
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)


@dataclass
class Phase:
    """阶段定义"""
    name: str
    description: str = ""
    tools: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    required: bool = True


@dataclass
class StateTransition:
    """状态转移"""
    from_phase: str
    to_phase: str
    event: str
    condition: Optional[str] = None


@dataclass
class QualityGate:
    """质量门禁"""
    phase: str
    description: str
    checker: str = ""  # 检查函数名或条件表达式


@dataclass
class ExceptionHandler:
    """异常处理器"""
    exception_type: str
    action: str
    max_retries: int = 3


@dataclass
class Method:
    """方法：阶段、工具序列、输出"""
    phases: Dict[str, Phase] = field(default_factory=dict)
    tool_sequences: Dict[str, List[str]] = field(default_factory=dict)
    phase_outputs: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class Mechanism:
    """机制：状态机、质量门禁、异常处理"""
    transitions: List[StateTransition] = field(default_factory=list)
    quality_gates: List[QualityGate] = field(default_factory=list)
    exception_handlers: List[ExceptionHandler] = field(default_factory=list)


class ProcessDefinition:
    """
    流程定义：章程 + 方法 + 机制
    
    一个完整的流程定义包含：
    1. Charter（章程）：目标、输入输出、约束
    2. Method（方法）：阶段、工具序列、输出
    3. Mechanism（机制）：状态机、质量门禁、异常处理
    """
    
    def __init__(
        self,
        name: str,
        paradigm: str,
        charter: Charter = None,
        method: Method = None,
        mechanism: Mechanism = None,
        version: str = "1.0.0",
        enabled: bool = True,
        author: str = "system"
    ):
        self.name = name
        self.paradigm = paradigm
        self.charter = charter or Charter()
        self.method = method or Method()
        self.mechanism = mechanism or Mechanism()
        self.version = version
        self.enabled = enabled
        self.author = author
        self.created_at = None
        self.updated_at = None
        
        # 构建状态机映射
        self._state_map: Dict[str, Dict[str, str]] = {}
        self._build_state_map()
    
    def _build_state_map(self):
        """构建状态转移映射: {from_phase: {event: to_phase}}"""
        for transition in self.mechanism.transitions:
            if transition.from_phase not in self._state_map:
                self._state_map[transition.from_phase] = {}
            self._state_map[transition.from_phase][transition.event] = transition.to_phase
    
    def get_tools_for_phase(self, phase: str) -> List[str]:
        """获取某阶段可用的工具列表"""
        phase_info = self.method.phases.get(phase)
        if phase_info:
            return phase_info.tools
        return self.method.tool_sequences.get(phase, [])
    
    def get_phase(self, phase_name: str) -> Optional[Phase]:
        """获取阶段定义"""
        return self.method.phases.get(phase_name)
    
    def get_all_phases(self) -> List[str]:
        """获取所有阶段名称"""
        return list(self.method.phases.keys())
    
    def get_initial_phase(self) -> Optional[str]:
        """获取初始阶段"""
        phases = self.get_all_phases()
        if phases:
            return phases[0]
        return None
    
    def get_final_phase(self) -> Optional[str]:
        """获取最终阶段"""
        phases = self.get_all_phases()
        if phases:
            return phases[-1]
        return None
    
    def can_transition(self, from_phase: str, event: str) -> bool:
        """检查是否可以转移"""
        return from_phase in self._state_map and event in self._state_map[from_phase]
    
    def transition(self, from_phase: str, event: str) -> Optional[str]:
        """
        状态转移
        
        Args:
            from_phase: 当前阶段
            event: 触发事件
            
        Returns:
            目标阶段，如果转移无效则返回 None
        """
        if from_phase in self._state_map:
            return self._state_map[from_phase].get(event)
        return None
    
    def get_quality_gates(self, phase: str) -> List[QualityGate]:
        """获取某阶段的质量门禁"""
        return [g for g in self.mechanism.quality_gates if g.phase == phase]
    
    def get_exception_handler(self, exception_type: str) -> Optional[ExceptionHandler]:
        """获取异常处理器"""
        for handler in self.mechanism.exception_handlers:
            if handler.exception_type == exception_type:
                return handler
        return None
    
    def validate(self) -> Tuple[bool, List[str]]:
        """
        验证流程定义的完整性
        
        Returns:
            (是否有效, 错误列表)
        """
        errors = []
        
        # 检查基本字段
        if not self.name:
            errors.append("流程名称不能为空")
        if not self.paradigm:
            errors.append("范式不能为空")
        
        # 检查阶段
        if not self.method.phases:
            errors.append("流程必须至少有一个阶段")
        
        # 检查状态机完整性
        phases = set(self.method.phases.keys())
        for transition in self.mechanism.transitions:
            if transition.from_phase not in phases:
                errors.append(f"状态转移的源阶段 '{transition.from_phase}' 不存在")
            if transition.to_phase not in phases:
                errors.append(f"状态转移的目标阶段 '{transition.to_phase}' 不存在")
        
        # 检查工具序列
        for phase, tools in self.method.tool_sequences.items():
            if phase not in phases:
                errors.append(f"工具序列定义的阶段 '{phase}' 不存在")
        
        return len(errors) == 0, errors
    
    def to_dict(self) -> dict:
        """导出为字典"""
        return {
            "name": self.name,
            "paradigm": self.paradigm,
            "version": self.version,
            "enabled": self.enabled,
            "author": self.author,
            "charter": {
                "objective": self.charter.objective,
                "inputs": self.charter.inputs,
                "outputs": self.charter.outputs,
                "constraints": self.charter.constraints,
            },
            "method": {
                "phases": {
                    name: {
                        "description": phase.description,
                        "tools": phase.tools,
                        "outputs": phase.outputs,
                        "required": phase.required,
                    }
                    for name, phase in self.method.phases.items()
                },
                "tool_sequences": self.method.tool_sequences,
                "phase_outputs": self.method.phase_outputs,
            },
            "mechanism": {
                "transitions": [
                    {
                        "from": t.from_phase,
                        "to": t.to_phase,
                        "event": t.event,
                        "condition": t.condition,
                    }
                    for t in self.mechanism.transitions
                ],
                "quality_gates": [
                    {
                        "phase": g.phase,
                        "description": g.description,
                        "checker": g.checker,
                    }
                    for g in self.mechanism.quality_gates
                ],
                "exception_handlers": [
                    {
                        "exception_type": h.exception_type,
                        "action": h.action,
                        "max_retries": h.max_retries,
                    }
                    for h in self.mechanism.exception_handlers
                ],
            },
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ProcessDefinition":
        """从字典创建"""
        # 解析 charter
        charter_data = data.get("charter", {})
        charter = Charter(
            objective=charter_data.get("objective", ""),
            inputs=charter_data.get("inputs", []),
            outputs=charter_data.get("outputs", []),
            constraints=charter_data.get("constraints", []),
        )
        
        # 解析 method
        method_data = data.get("method", {})
        phases = {}
        for name, phase_data in method_data.get("phases", {}).items():
            phases[name] = Phase(
                name=name,
                description=phase_data.get("description", ""),
                tools=phase_data.get("tools", []),
                outputs=phase_data.get("outputs", []),
                required=phase_data.get("required", True),
            )
        
        method = Method(
            phases=phases,
            tool_sequences=method_data.get("tool_sequences", {}),
            phase_outputs=method_data.get("phase_outputs", {}),
        )
        
        # 解析 mechanism
        mechanism_data = data.get("mechanism", {})
        transitions = []
        for t in mechanism_data.get("transitions", []):
            transitions.append(StateTransition(
                from_phase=t["from"],
                to_phase=t["to"],
                event=t["event"],
                condition=t.get("condition"),
            ))
        
        quality_gates = []
        for g in mechanism_data.get("quality_gates", []):
            quality_gates.append(QualityGate(
                phase=g["phase"],
                description=g["description"],
                checker=g.get("checker", ""),
            ))
        
        exception_handlers = []
        for h in mechanism_data.get("exception_handlers", []):
            exception_handlers.append(ExceptionHandler(
                exception_type=h["exception_type"],
                action=h["action"],
                max_retries=h.get("max_retries", 3),
            ))
        
        mechanism = Mechanism(
            transitions=transitions,
            quality_gates=quality_gates,
            exception_handlers=exception_handlers,
        )
        
        return cls(
            name=data["name"],
            paradigm=data["paradigm"],
            charter=charter,
            method=method,
            mechanism=mechanism,
            version=data.get("version", "1.0.0"),
            enabled=data.get("enabled", True),
            author=data.get("author", "system"),
        )
    
    @classmethod
    def from_yaml(cls, yaml_path: Path) -> "ProcessDefinition":
        """从 YAML 文件加载"""
        if not YAML_AVAILABLE:
            raise ImportError("PyYAML 未安装，无法加载 YAML 文件")
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)
    
    def to_yaml(self, yaml_path: Path):
        """保存为 YAML 文件"""
        if not YAML_AVAILABLE:
            raise ImportError("PyYAML 未安装，无法保存为 YAML 文件")
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(self.to_dict(), f, allow_unicode=True, default_flow_style=False)


# ========== 内置流程定义 ==========

def create_code_development_process() -> ProcessDefinition:
    """创建代码开发流程"""
    return ProcessDefinition(
        name="code_development",
        paradigm="CODE_DEV",
        charter=Charter(
            objective="完成代码开发任务，输出高质量代码、测试、文档",
            inputs=["用户需求描述", "可选：现有代码库"],
            outputs=["可运行的代码", "测试用例", "技术文档"],
            constraints=["遵循编码规范", "测试覆盖率 >= 80%", "文档完整"]
        ),
        method=Method(
            phases={
                "ARCH": Phase("ARCH", "架构设计", 
                             tools=["read_file", "bash", "workflow_step"],
                             outputs=["架构文档"]),
                "REQ": Phase("REQ", "需求分析",
                            tools=["read_file", "write_file", "todo"],
                            outputs=["需求文档"]),
                "DESIGN": Phase("DESIGN", "详细设计",
                               tools=["write_file", "workflow_step"],
                               outputs=["设计文档"]),
                "EXEC": Phase("EXEC", "执行开发",
                             tools=["bash", "write_file", "edit_file", 
                                   "worktree_create", "worktree_run"],
                             outputs=["代码"]),
                "VERIFY": Phase("VERIFY", "验证测试",
                               tools=["bash", "background_run"],
                               outputs=["测试报告"]),
                "REFINE": Phase("REFINE", "修正问题",
                               tools=["edit_file", "bash"],
                               outputs=["修正后代码"]),
                "DONE": Phase("DONE", "完成", tools=[], outputs=[]),
            },
            tool_sequences={
                "ARCH": ["read_file", "bash", "workflow_step"],
                "REQ": ["read_file", "write_file", "todo"],
                "DESIGN": ["write_file", "workflow_step"],
                "EXEC": ["bash", "write_file", "edit_file", "worktree_create", "worktree_run"],
                "VERIFY": ["bash", "background_run"],
                "REFINE": ["edit_file", "bash"],
            }
        ),
        mechanism=Mechanism(
            transitions=[
                StateTransition("ARCH", "REQ", "confirm"),
                StateTransition("REQ", "DESIGN", "confirm"),
                StateTransition("DESIGN", "EXEC", "confirm"),
                StateTransition("EXEC", "VERIFY", "execute_done"),
                StateTransition("VERIFY", "DONE", "verify_pass"),
                StateTransition("VERIFY", "REFINE", "verify_fail"),
                StateTransition("REFINE", "VERIFY", "refine_done"),
            ],
            quality_gates=[
                QualityGate("DESIGN", "用户必须明确固化确认", "user_confirm"),
                QualityGate("VERIFY", "测试必须通过", "test_pass"),
            ],
            exception_handlers=[
                ExceptionHandler("verify_fail", "进入REFINE阶段", 3),
                ExceptionHandler("tool_error", "记录错误，尝试替代工具", 1),
            ]
        )
    )


def create_test_evaluation_process() -> ProcessDefinition:
    """创建测试评估流程"""
    return ProcessDefinition(
        name="test_evaluation",
        paradigm="TEST_EVAL",
        charter=Charter(
            objective="完成测试任务，输出测试报告和覆盖率数据",
            inputs=["测试目标", "测试类型要求"],
            outputs=["测试用例", "测试报告", "覆盖率报告"]
        ),
        method=Method(
            phases={
                "PLAN": Phase("PLAN", "测试计划",
                             tools=["read_file", "write_file", "todo"],
                             outputs=["测试计划"]),
                "DESIGN": Phase("DESIGN", "用例设计",
                               tools=["write_file"],
                               outputs=["测试用例"]),
                "EXEC": Phase("EXEC", "执行测试",
                             tools=["bash", "background_run"],
                             outputs=["测试结果"]),
                "REPORT": Phase("REPORT", "生成报告",
                               tools=["write_file"],
                               outputs=["测试报告"]),
                "DONE": Phase("DONE", "完成", tools=[], outputs=[]),
            },
            tool_sequences={
                "PLAN": ["read_file", "write_file", "todo"],
                "DESIGN": ["write_file"],
                "EXEC": ["bash", "background_run"],
                "REPORT": ["write_file"],
            }
        ),
        mechanism=Mechanism(
            transitions=[
                StateTransition("PLAN", "DESIGN", "confirm"),
                StateTransition("DESIGN", "EXEC", "confirm"),
                StateTransition("EXEC", "REPORT", "execute_done"),
                StateTransition("REPORT", "DONE", "confirm"),
            ]
        )
    )


def create_general_qa_process() -> ProcessDefinition:
    """创建通用问答流程"""
    return ProcessDefinition(
        name="general_qa",
        paradigm="GENERAL",
        charter=Charter(
            objective="回答用户问题",
            inputs=["用户问题"],
            outputs=["回答内容"]
        ),
        method=Method(
            phases={
                "UNDERSTAND": Phase("UNDERSTAND", "理解问题",
                                   tools=["read_file"],
                                   outputs=["问题理解"]),
                "ANSWER": Phase("ANSWER", "生成回答",
                               tools=[],
                               outputs=["回答内容"]),
                "DONE": Phase("DONE", "完成", tools=[], outputs=[]),
            },
            tool_sequences={
                "UNDERSTAND": ["read_file"],
                "ANSWER": [],
            }
        ),
        mechanism=Mechanism(
            transitions=[
                StateTransition("UNDERSTAND", "ANSWER", "understood"),
                StateTransition("ANSWER", "DONE", "answered"),
            ]
        )
    )


def create_feature_design_process() -> ProcessDefinition:
    """创建功能设计流程"""
    return ProcessDefinition(
        name="feature_design",
        paradigm="FEATURE_DESIGN",
        charter=Charter(
            objective="完成系统/功能设计，输出设计文档",
            inputs=["功能需求"],
            outputs=["设计方案", "架构图", "接口定义"]
        ),
        method=Method(
            phases={
                "ANALYZE": Phase("ANALYZE", "需求分析",
                                tools=["read_file", "write_file"],
                                outputs=["需求分析文档"]),
                "DESIGN": Phase("DESIGN", "方案设计",
                               tools=["write_file"],
                               outputs=["设计方案"]),
                "REVIEW": Phase("REVIEW", "设计评审",
                               tools=["write_file"],
                               outputs=["评审意见"]),
                "DONE": Phase("DONE", "完成", tools=[], outputs=[]),
            },
            tool_sequences={
                "ANALYZE": ["read_file", "write_file"],
                "DESIGN": ["write_file"],
                "REVIEW": ["write_file"],
            }
        ),
        mechanism=Mechanism(
            transitions=[
                StateTransition("ANALYZE", "DESIGN", "confirm"),
                StateTransition("DESIGN", "REVIEW", "confirm"),
                StateTransition("REVIEW", "DONE", "approve"),
                StateTransition("REVIEW", "DESIGN", "reject"),
            ]
        )
    )


def create_engineering_process() -> ProcessDefinition:
    """创建工程实践流程"""
    return ProcessDefinition(
        name="engineering_practice",
        paradigm="ENGINEERING",
        charter=Charter(
            objective="完成工程化任务（CI/CD、部署、监控等）",
            inputs=["工程需求"],
            outputs=["配置文件", "部署脚本", "监控配置"]
        ),
        method=Method(
            phases={
                "CONFIG": Phase("CONFIG", "配置准备",
                               tools=["read_file", "write_file", "bash"],
                               outputs=["配置文件"]),
                "DEPLOY": Phase("DEPLOY", "部署执行",
                               tools=["bash", "background_run"],
                               outputs=["部署结果"]),
                "VERIFY": Phase("VERIFY", "验证检查",
                               tools=["bash"],
                               outputs=["验证报告"]),
                "DONE": Phase("DONE", "完成", tools=[], outputs=[]),
            },
            tool_sequences={
                "CONFIG": ["read_file", "write_file", "bash"],
                "DEPLOY": ["bash", "background_run"],
                "VERIFY": ["bash"],
            }
        ),
        mechanism=Mechanism(
            transitions=[
                StateTransition("CONFIG", "DEPLOY", "confirm"),
                StateTransition("DEPLOY", "VERIFY", "deploy_done"),
                StateTransition("VERIFY", "DONE", "verify_pass"),
                StateTransition("VERIFY", "CONFIG", "verify_fail"),
            ]
        )
    )


def create_documentation_process() -> ProcessDefinition:
    """创建文档编写流程"""
    return ProcessDefinition(
        name="documentation",
        paradigm="DOC_WRITING",
        charter=Charter(
            objective="完成文档编写任务",
            inputs=["文档需求", "可选：代码/系统信息"],
            outputs=["文档文件"]
        ),
        method=Method(
            phases={
                "PLAN": Phase("PLAN", "文档规划",
                             tools=["read_file", "write_file", "todo"],
                             outputs=["文档大纲"]),
                "WRITE": Phase("WRITE", "内容编写",
                              tools=["read_file", "write_file"],
                              outputs=["文档内容"]),
                "REVIEW": Phase("REVIEW", "审阅修改",
                               tools=["write_file", "edit_file"],
                               outputs=["最终文档"]),
                "DONE": Phase("DONE", "完成", tools=[], outputs=[]),
            },
            tool_sequences={
                "PLAN": ["read_file", "write_file", "todo"],
                "WRITE": ["read_file", "write_file"],
                "REVIEW": ["write_file", "edit_file"],
            }
        ),
        mechanism=Mechanism(
            transitions=[
                StateTransition("PLAN", "WRITE", "confirm"),
                StateTransition("WRITE", "REVIEW", "draft_done"),
                StateTransition("REVIEW", "DONE", "approve"),
                StateTransition("REVIEW", "WRITE", "revise"),
            ]
        )
    )


# ========== 测试 ==========

if __name__ == "__main__":
    print("=== 流程定义测试 ===\n")
    
    # 创建代码开发流程
    process = create_code_development_process()
    
    print(f"流程名称: {process.name}")
    print(f"范式: {process.paradigm}")
    print(f"目标: {process.charter.objective}")
    
    print("\n阶段:")
    for phase_name, phase in process.method.phases.items():
        print(f"  {phase_name}: {phase.description}")
        print(f"    工具: {phase.tools}")
    
    print("\n状态转移测试:")
    print(f"  ARCH + confirm -> {process.transition('ARCH', 'confirm')}")
    print(f"  EXEC + execute_done -> {process.transition('EXEC', 'execute_done')}")
    print(f"  VERIFY + verify_pass -> {process.transition('VERIFY', 'verify_pass')}")
    print(f"  VERIFY + verify_fail -> {process.transition('VERIFY', 'verify_fail')}")
    
    print("\n质量门禁:")
    for gate in process.mechanism.quality_gates:
        print(f"  {gate.phase}: {gate.description}")
    
    # 验证
    valid, errors = process.validate()
    print(f"\n验证结果: {'有效' if valid else '无效'}")
    if errors:
        for err in errors:
            print(f"  - {err}")
    
    # 导出为字典
    print("\n导出为字典（部分）:")
    data = process.to_dict()
    print(f"  name: {data['name']}")
    print(f"  paradigm: {data['paradigm']}")
    print(f"  phases: {list(data['method']['phases'].keys())}")
    
    print("\n=== 测试完成 ===")
