#!/usr/bin/env python3
"""
意图管理工具模块 (Intent Management Tools)
解决的核心问题：确保LLM准确理解并遵循用户的最终目的

设计理念：
1. 万事万物工具化 - 意图澄清、注册、验证都是工具
2. 在执行任何操作前，必须明确用户最终目的
3. 执行特定/非通用操作时，必须验证是否符合用户初衷
4. 通过交互消除歧义，避免写死特例

工具清单：
- register_intent: 注册用户最终目的（必须先调用）
- clarify_intent: 消除歧义（不确定时调用）
- verify_action: 验证操作是否符合初衷（关键操作前调用）
- track_decision: 记录设计决策（做出重要决策时调用）
"""

import json
import threading
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any, Tuple
from pathlib import Path
from datetime import datetime
from enum import Enum


class IntentStatus(Enum):
    PENDING = "pending"
    CLARIFYING = "clarifying"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"


@dataclass
class ClarificationRecord:
    question: str
    answer: str
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


@dataclass 
class DecisionRecord:
    decision: str
    reason: str
    alternatives: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


@dataclass
class UserIntent:
    primary_goals: List[str] = field(default_factory=list)
    secondary_goals: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    status: IntentStatus = IntentStatus.PENDING
    clarifications: List[ClarificationRecord] = field(default_factory=list)
    decisions: List[DecisionRecord] = field(default_factory=list)
    original_query: str = ""
    paradigm: str = ""
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    updated_at: float = field(default_factory=lambda: datetime.now().timestamp())
    
    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data
    
    @classmethod
    def from_dict(cls, data: dict) -> "UserIntent":
        data = data.copy()
        data["status"] = IntentStatus(data.get("status", "pending"))
        data["clarifications"] = [
            ClarificationRecord(**c) if isinstance(c, dict) else c 
            for c in data.get("clarifications", [])
        ]
        data["decisions"] = [
            DecisionRecord(**d) if isinstance(d, dict) else d
            for d in data.get("decisions", [])
        ]
        return cls(**data)


class IntentManager:
    """
    意图管理器 - 管理所有会话的意图
    
    核心职责：
    1. 存储和管理每个会话的用户意图
    2. 检查操作是否符合意图
    3. 追踪决策历史
    """
    
    _instance = None
    _lock = threading.RLock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        self._intents: Dict[str, UserIntent] = {}
        self._storage_path: Optional[Path] = None
        self._data_lock = threading.RLock()
    
    def set_storage_path(self, path: Path):
        self._storage_path = path
        self._load_from_storage()
    
    def _load_from_storage(self):
        if self._storage_path and self._storage_path.exists():
            try:
                data = json.loads(self._storage_path.read_text(encoding="utf-8"))
                with self._data_lock:
                    self._intents = {
                        sid: UserIntent.from_dict(intent_data)
                        for sid, intent_data in data.items()
                    }
            except Exception:
                pass
    
    def _save_to_storage(self):
        if self._storage_path:
            try:
                with self._data_lock:
                    data = {
                        sid: intent.to_dict() 
                        for sid, intent in self._intents.items()
                    }
                self._storage_path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8"
                )
            except Exception:
                pass
    
    def register_intent(
        self,
        session_id: str,
        primary_goals: List[str],
        secondary_goals: List[str] = None,
        constraints: List[str] = None,
        original_query: str = "",
        paradigm: str = ""
    ) -> UserIntent:
        intent = UserIntent(
            primary_goals=primary_goals,
            secondary_goals=secondary_goals or [],
            constraints=constraints or [],
            status=IntentStatus.CONFIRMED,
            original_query=original_query,
            paradigm=paradigm
        )
        with self._data_lock:
            self._intents[session_id] = intent
        self._save_to_storage()
        return intent
    
    def get_intent(self, session_id: str) -> Optional[UserIntent]:
        with self._data_lock:
            return self._intents.get(session_id)
    
    def add_clarification(
        self, 
        session_id: str, 
        question: str, 
        answer: str
    ) -> bool:
        with self._data_lock:
            intent = self._intents.get(session_id)
            if not intent:
                return False
            intent.clarifications.append(ClarificationRecord(
                question=question,
                answer=answer
            ))
            intent.updated_at = datetime.now().timestamp()
        self._save_to_storage()
        return True
    
    def set_status(self, session_id: str, status: IntentStatus):
        with self._data_lock:
            intent = self._intents.get(session_id)
            if intent:
                intent.status = status
                intent.updated_at = datetime.now().timestamp()
        self._save_to_storage()
    
    def track_decision(
        self,
        session_id: str,
        decision: str,
        reason: str,
        alternatives: List[str] = None
    ) -> bool:
        with self._data_lock:
            intent = self._intents.get(session_id)
            if not intent:
                return False
            intent.decisions.append(DecisionRecord(
                decision=decision,
                reason=reason,
                alternatives=alternatives or []
            ))
            intent.updated_at = datetime.now().timestamp()
        self._save_to_storage()
        return True
    
    def check_alignment(
        self,
        session_id: str,
        action: str,
        action_type: str = "tool_call"
    ) -> Tuple[bool, Optional[str]]:
        """
        检查操作是否符合用户意图
        
        Returns:
            (is_aligned, warning_message)
        """
        with self._data_lock:
            intent = self._intents.get(session_id)
        
        if not intent:
            return True, None
        
        if intent.status != IntentStatus.CONFIRMED:
            return False, f"意图尚未确认（当前状态: {intent.status.value}），请先确认用户意图"
        
        action_lower = action.lower()
        
        for constraint in intent.constraints:
            constraint_lower = constraint.lower()
            if self._action_violates_constraint(action_lower, constraint_lower):
                return False, f"该操作可能违反用户约束: '{constraint}'"
        
        action_keywords = self._extract_action_keywords(action_lower)
        goal_keywords = self._extract_goal_keywords(intent)
        
        relevance_score = self._calculate_relevance(action_keywords, goal_keywords)
        
        if relevance_score < 0.3:
            return True, f"[警告] 该操作与用户主要目标关联度较低（{relevance_score:.0%}），请确认是否为必要步骤"
        
        return True, None
    
    def _action_violates_constraint(self, action: str, constraint: str) -> bool:
        violation_patterns = [
            ("不要", ["不要", "禁止", "不能", "不可"]),
            ("不", ["不"]),
            ("禁止", ["禁止"]),
        ]
        
        for pattern, keywords in violation_patterns:
            if pattern in constraint:
                for kw in keywords:
                    if kw in action and kw in constraint:
                        return True
        return False
    
    def _extract_action_keywords(self, action: str) -> set:
        common_words = {"的", "了", "是", "在", "和", "与", "或", "the", "a", "an", "is", "are", "to", "for"}
        words = action.replace("_", " ").split()
        return {w for w in words if w not in common_words and len(w) > 1}
    
    def _extract_goal_keywords(self, intent: UserIntent) -> set:
        keywords = set()
        for goal in intent.primary_goals:
            keywords.update(self._extract_action_keywords(goal.lower()))
        for goal in intent.secondary_goals:
            keywords.update(self._extract_action_keywords(goal.lower()))
        return keywords
    
    def _calculate_relevance(self, action_keywords: set, goal_keywords: set) -> float:
        if not goal_keywords:
            return 1.0
        if not action_keywords:
            return 0.0
        
        common = action_keywords & goal_keywords
        return len(common) / len(goal_keywords)
    
    def get_decision_history(self, session_id: str) -> List[DecisionRecord]:
        with self._data_lock:
            intent = self._intents.get(session_id)
            return intent.decisions.copy() if intent else []
    
    def clear_session(self, session_id: str):
        with self._data_lock:
            self._intents.pop(session_id, None)
        self._save_to_storage()


_intent_manager: Optional[IntentManager] = None
_intent_manager_lock = threading.RLock()


def get_intent_manager() -> IntentManager:
    global _intent_manager
    if _intent_manager is None:
        with _intent_manager_lock:
            if _intent_manager is None:
                _intent_manager = IntentManager()
    return _intent_manager


def run_register_intent(
    session_id: str,
    primary_goals: List[str],
    secondary_goals: List[str] = None,
    constraints: List[str] = None,
    original_query: str = "",
    paradigm: str = ""
) -> Tuple[str, Optional[str]]:
    """
    注册用户最终目的
    
    Args:
        session_id: 会话ID
        primary_goals: 主要目标列表（必须达成）
        secondary_goals: 次要目标列表（可选）
        constraints: 约束条件列表（不可违背）
        original_query: 用户原始query
        paradigm: 范式类型
    
    Returns:
        (result_message, error)
    """
    if not session_id:
        return "", "session_id 不能为空"
    
    if not primary_goals:
        return "", "primary_goals 不能为空"
    
    manager = get_intent_manager()
    
    try:
        intent = manager.register_intent(
            session_id=session_id,
            primary_goals=primary_goals,
            secondary_goals=secondary_goals or [],
            constraints=constraints or [],
            original_query=original_query,
            paradigm=paradigm
        )
        
        result = f"""已成功注册用户意图：

【主要目标】
{chr(10).join(f'• {g}' for g in intent.primary_goals)}

【次要目标】
{chr(10).join(f'• {g}' for g in intent.secondary_goals) if intent.secondary_goals else '无'}

【约束条件】
{chr(10).join(f'• {c}' for c in intent.constraints) if intent.constraints else '无'}

后续所有操作都将以此意图为准。如需执行与主要目标关联度较低的操作，系统会发出警告。
"""
        return result, None
        
    except Exception as e:
        return "", f"注册意图失败: {str(e)}"


def run_clarify_intent(
    session_id: str,
    question: str,
    options: List[str] = None
) -> Tuple[str, Optional[str]]:
    """
    请求用户澄清意图（当LLM不确定时调用）
    
    Args:
        session_id: 会话ID
        question: 需要澄清的问题
        options: 可选的答案选项列表
    
    Returns:
        (result_message, error)
    """
    if not session_id:
        return "", "session_id 不能为空"
    
    if not question:
        return "", "question 不能为空"
    
    manager = get_intent_manager()
    intent = manager.get_intent(session_id)
    
    if not intent:
        manager.register_intent(
            session_id=session_id,
            primary_goals=["待澄清"],
            status=IntentStatus.CLARIFYING
        )
    else:
        manager.set_status(session_id, IntentStatus.CLARIFYING)
    
    options_text = ""
    if options:
        options_text = f"""

【可选答案】
{chr(10).join(f'{i+1}. {opt}' for i, opt in enumerate(options))}

请回复选项编号或直接回答问题。"""
    
    result = f"""[需要用户澄清]

{question}{options_text}

---
提示：请用户回答后，调用 register_intent 更新意图，或直接继续对话。
"""
    return result, None


def run_verify_action(
    session_id: str,
    action: str,
    action_description: str = "",
    action_type: str = "tool_call"
) -> Tuple[str, Optional[str]]:
    """
    验证操作是否符合用户初衷
    
    Args:
        session_id: 会话ID
        action: 要执行的操作名称/内容
        action_description: 操作的详细描述
        action_type: 操作类型 (tool_call, design_decision, code_change, etc.)
    
    Returns:
        (result_message, error)
    """
    if not session_id:
        return "", "session_id 不能为空"
    
    if not action:
        return "", "action 不能为空"
    
    manager = get_intent_manager()
    intent = manager.get_intent(session_id)
    
    if not intent:
        return "[警告] 未找到已注册的用户意图，请先调用 register_intent 注册用户目标", None
    
    is_aligned, warning = manager.check_alignment(session_id, action, action_type)
    
    if not is_aligned:
        result = f"""[意图验证失败]

操作: {action}
描述: {action_description or '无'}

问题: {warning}

建议:
1. 重新评估此操作是否必要
2. 如果必要，向用户解释原因并获取确认
3. 考虑是否有更符合用户目标的替代方案

用户主要目标:
{chr(10).join(f'• {g}' for g in intent.primary_goals)}
"""
        return result, None
    
    if warning:
        result = f"""[意图验证通过，但有警告]

操作: {action}
描述: {action_description or '无'}

{warning}

用户主要目标:
{chr(10).join(f'• {g}' for g in intent.primary_goals)}

建议: 如果此操作是必要的步骤，请继续；否则请重新考虑。
"""
        return result, None
    
    result = f"""[意图验证通过]

操作: {action}
描述: {action_description or '无'}

该操作与用户主要目标一致，可以继续执行。
"""
    return result, None


def run_track_decision(
    session_id: str,
    decision: str,
    reason: str,
    alternatives: List[str] = None
) -> Tuple[str, Optional[str]]:
    """
    记录重要的设计决策
    
    Args:
        session_id: 会话ID
        decision: 做出的决策
        reason: 决策原因
        alternatives: 考虑过的其他方案
    
    Returns:
        (result_message, error)
    """
    if not session_id:
        return "", "session_id 不能为空"
    
    if not decision:
        return "", "decision 不能为空"
    
    if not reason:
        return "", "reason 不能为空"
    
    manager = get_intent_manager()
    intent = manager.get_intent(session_id)
    
    if not intent:
        return "", f"会话 {session_id} 未注册意图，请先调用 register_intent"
    
    success = manager.track_decision(
        session_id=session_id,
        decision=decision,
        reason=reason,
        alternatives=alternatives or []
    )
    
    if not success:
        return "", "记录决策失败"
    
    history = manager.get_decision_history(session_id)
    
    result = f"""已记录设计决策：

【决策】{decision}

【原因】{reason}

【备选方案】
{chr(10).join(f'• {a}' for a in alternatives) if alternatives else '无'}

【历史决策数】{len(history)}

此记录将用于后续意图验证和决策回溯。
"""
    return result, None


def run_get_intent_status(session_id: str) -> Tuple[str, Optional[str]]:
    """
    获取会话的意图状态
    
    Args:
        session_id: 会话ID
    
    Returns:
        (result_message, error)
    """
    if not session_id:
        return "", "session_id 不能为空"
    
    manager = get_intent_manager()
    intent = manager.get_intent(session_id)
    
    if not intent:
        return f"会话 {session_id} 尚未注册意图", None
    
    result = f"""意图状态: {intent.status.value}

【用户原始请求】
{intent.original_query or '未记录'}

【主要目标】
{chr(10).join(f'• {g}' for g in intent.primary_goals)}

【次要目标】
{chr(10).join(f'• {g}' for g in intent.secondary_goals) if intent.secondary_goals else '无'}

【约束条件】
{chr(10).join(f'• {c}' for c in intent.constraints) if intent.constraints else '无'}

【澄清记录】{len(intent.clarifications)} 条
【决策记录】{len(intent.decisions)} 条
"""
    return result, None
