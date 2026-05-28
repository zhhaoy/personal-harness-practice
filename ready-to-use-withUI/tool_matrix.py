#!/usr/bin/env python3
"""
工具矩阵层 (Tool Matrix Layer)
管理所有底层工具，提供权限控制和分组管理
用户不可直接访问，仅被流程内部调用
"""

import json
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Callable, Set
from dataclasses import dataclass, field
from enum import Enum


class ToolGroup(Enum):
    """工具分组"""
    FILE = "file"
    TASK = "task"
    TEAM = "team"
    WORKFLOW = "workflow"
    CONTEXT = "context"
    BACKGROUND = "background"
    INTENT = "intent"


@dataclass
class ToolInfo:
    """工具信息"""
    name: str
    group: ToolGroup
    description: str
    handler: Callable
    enabled: bool = True
    user_visible: bool = False  # 用户是否可见（默认不可见）


@dataclass
class PermissionMatrix:
    """权限矩阵：定义每个范式可访问的工具组"""
    permissions: Dict[str, Set[ToolGroup]] = field(default_factory=dict)
    
    def __post_init__(self):
        # 默认权限配置
        if not self.permissions:
            self.permissions = {
                "CODE_DEV": {ToolGroup.FILE, ToolGroup.TASK, ToolGroup.TEAM, 
                            ToolGroup.WORKFLOW, ToolGroup.CONTEXT, ToolGroup.BACKGROUND,
                            ToolGroup.INTENT},
                "TEST_EVAL": {ToolGroup.FILE, ToolGroup.TASK, ToolGroup.CONTEXT, ToolGroup.BACKGROUND,
                             ToolGroup.INTENT},
                "FEATURE_DESIGN": {ToolGroup.FILE, ToolGroup.CONTEXT, ToolGroup.INTENT},
                "ENGINEERING": {ToolGroup.FILE, ToolGroup.TASK, ToolGroup.TEAM, 
                               ToolGroup.CONTEXT, ToolGroup.BACKGROUND, ToolGroup.INTENT},
                "DOC_WRITING": {ToolGroup.FILE, ToolGroup.CONTEXT, ToolGroup.INTENT},
                "DATA_ANALYSIS": {ToolGroup.FILE, ToolGroup.CONTEXT, ToolGroup.BACKGROUND, ToolGroup.INTENT},
                "GENERAL": {ToolGroup.FILE, ToolGroup.CONTEXT, ToolGroup.INTENT},
            }
    
    def has_permission(self, paradigm: str, tool_group: ToolGroup) -> bool:
        """检查范式是否有权限访问工具组"""
        return tool_group in self.permissions.get(paradigm, set())
    
    def get_allowed_groups(self, paradigm: str) -> Set[ToolGroup]:
        """获取范式允许访问的所有工具组"""
        return self.permissions.get(paradigm, set())
    
    def grant(self, paradigm: str, tool_group: ToolGroup):
        """授权"""
        if paradigm not in self.permissions:
            self.permissions[paradigm] = set()
        self.permissions[paradigm].add(tool_group)
    
    def revoke(self, paradigm: str, tool_group: ToolGroup):
        """撤销权限"""
        if paradigm in self.permissions:
            self.permissions[paradigm].discard(tool_group)


class ToolMatrix:
    """
    工具矩阵：底层工具管理
    
    特性：
    1. 工具分组管理
    2. 权限控制（流程只能调用授权的工具）
    3. 对用户不可见（用户层屏蔽）
    """
    
    # 工具分组映射
    TOOL_GROUPS = {
        ToolGroup.FILE: {
            "bash", "read_file", "write_file", "edit_file"
        },
        ToolGroup.TASK: {
            "task_create", "task_list", "task_get", "task_update", "task_bind_worktree",
            "worktree_create", "worktree_list", "worktree_status", "worktree_run",
            "worktree_keep", "worktree_remove", "worktree_events",
            "worktree_sync", "worktree_list_files", "worktree_copy_files", "worktree_read_file"
        },
        ToolGroup.TEAM: {
            "spawn_teammate", "activate_teammate", "list_teammates",
            "send_message", "read_inbox", "broadcast",
            "shutdown_request", "plan_approval"
        },
        ToolGroup.WORKFLOW: {
            "workflow_start", "workflow_step", "workflow_status"
        },
        ToolGroup.CONTEXT: {
            "compact", "load_skill", "todo", "task"
        },
        ToolGroup.BACKGROUND: {
            "background_run", "check_background"
        },
        ToolGroup.INTENT: {
            "register_intent", "clarify_intent", "verify_action", 
            "track_decision", "get_intent_status"
        },
    }
    
    def __init__(self, tool_registry=None):
        """
        初始化工具矩阵
        
        Args:
            tool_registry: 原有的 ToolRegistry 实例
        """
        self.registry = tool_registry
        self.permission_matrix = PermissionMatrix()
        self._tools: Dict[str, ToolInfo] = {}
        self._lock = threading.RLock()
        
        self._init_tools()
    
    def _init_tools(self):
        """初始化工具信息"""
        # 构建反向映射：工具名 -> 工具组
        tool_to_group = {}
        for group, tools in self.TOOL_GROUPS.items():
            for tool in tools:
                tool_to_group[tool] = group
        
        # 注册所有工具
        for tool_name, group in tool_to_group.items():
            self._tools[tool_name] = ToolInfo(
                name=tool_name,
                group=group,
                description=f"Tool: {tool_name}",
                handler=None,  # 从 registry 获取
                enabled=True,
                user_visible=False  # 默认用户不可见
            )
    
    def bind_registry(self, registry):
        """绑定工具注册表"""
        self.registry = registry
    
    def get_tool_group(self, tool_name: str) -> Optional[ToolGroup]:
        """获取工具所属的分组"""
        tool_info = self._tools.get(tool_name)
        return tool_info.group if tool_info else None
    
    def list_tools_by_group(self, group: ToolGroup) -> List[str]:
        """列出某组的所有工具"""
        return list(self.TOOL_GROUPS.get(group, set()))
    
    def list_all_tools(self) -> List[str]:
        """列出所有工具"""
        return list(self._tools.keys())
    
    def list_user_visible_tools(self) -> List[str]:
        """列出用户可见的工具（仅 meta_dispatch）"""
        return ["meta_dispatch"]  # 用户唯一可见的工具
    
    def call(
        self,
        tool_name: str,
        params: dict,
        paradigm: str,
        check_permission: bool = True
    ) -> Tuple[Any, Optional[str]]:
        """
        调用工具（带权限检查）
        
        Args:
            tool_name: 工具名称
            params: 工具参数
            paradigm: 调用方范式（用于权限检查）
            check_permission: 是否检查权限
            
        Returns:
            (result, error) 元组
        """
        # 检查工具是否存在
        if tool_name not in self._tools:
            return None, f"工具 '{tool_name}' 不存在"
        
        tool_info = self._tools[tool_name]
        
        # 检查工具是否启用
        if not tool_info.enabled:
            return None, f"工具 '{tool_name}' 已禁用"
        
        # 权限检查
        if check_permission:
            if not self.permission_matrix.has_permission(paradigm, tool_info.group):
                return None, f"范式 '{paradigm}' 无权调用工具 '{tool_name}' (组: {tool_info.group.value})"
        
        # 执行工具
        if self.registry is None:
            return None, "工具注册表未绑定"
        
        handler = self.registry.get_handler(tool_name)
        if handler is None:
            return None, f"工具 '{tool_name}' 的处理器未找到"
        
        try:
            result, error = handler(**params)
            return result, error
        except Exception as e:
            import traceback
            return None, f"工具执行异常: {str(e)}\n{traceback.format_exc()}"
    
    def check_permission(self, tool_name: str, paradigm: str) -> bool:
        """检查范式是否有权限调用工具"""
        tool_info = self._tools.get(tool_name)
        if not tool_info:
            return False
        return self.permission_matrix.has_permission(paradigm, tool_info.group)
    
    def get_allowed_tools(self, paradigm: str) -> List[str]:
        """获取范式允许调用的所有工具"""
        allowed_groups = self.permission_matrix.get_allowed_groups(paradigm)
        tools = []
        for group in allowed_groups:
            tools.extend(self.TOOL_GROUPS.get(group, set()))
        return tools
    
    def enable_tool(self, tool_name: str):
        """启用工具"""
        if tool_name in self._tools:
            self._tools[tool_name].enabled = True
    
    def disable_tool(self, tool_name: str):
        """禁用工具"""
        if tool_name in self._tools:
            self._tools[tool_name].enabled = False
    
    def get_tool_info(self, tool_name: str) -> Optional[ToolInfo]:
        """获取工具信息"""
        return self._tools.get(tool_name)
    
    def to_dict(self) -> dict:
        """导出为字典"""
        return {
            "tools": {
                name: {
                    "group": info.group.value,
                    "enabled": info.enabled,
                    "user_visible": info.user_visible
                }
                for name, info in self._tools.items()
            },
            "permissions": {
                paradigm: [g.value for g in groups]
                for paradigm, groups in self.permission_matrix.permissions.items()
            }
        }


# 全局工具矩阵实例
_tool_matrix: Optional[ToolMatrix] = None


def get_tool_matrix() -> ToolMatrix:
    """获取全局工具矩阵实例"""
    global _tool_matrix
    if _tool_matrix is None:
        _tool_matrix = ToolMatrix()
    return _tool_matrix


def init_tool_matrix(registry) -> ToolMatrix:
    """初始化并绑定工具注册表"""
    global _tool_matrix
    _tool_matrix = ToolMatrix(registry)
    return _tool_matrix


# ========== 工具函数（供内部使用，不对用户暴露） ==========

def internal_call_tool(tool_name: str, params: dict, paradigm: str) -> Tuple[Any, Optional[str]]:
    """
    内部工具调用接口
    仅供流程内部使用，不对用户暴露
    """
    matrix = get_tool_matrix()
    return matrix.call(tool_name, params, paradigm, check_permission=True)


def internal_list_allowed_tools(paradigm: str) -> List[str]:
    """
    列出范式允许的工具
    仅供流程内部使用
    """
    matrix = get_tool_matrix()
    return matrix.get_allowed_tools(paradigm)


# ========== 测试 ==========

if __name__ == "__main__":
    print("=== 工具矩阵测试 ===\n")
    
    matrix = ToolMatrix()
    
    # 测试工具分组
    print("文件工具组:", matrix.list_tools_by_group(ToolGroup.FILE))
    print("任务工具组:", matrix.list_tools_by_group(ToolGroup.TASK))
    print("团队工具组:", matrix.list_tools_by_group(ToolGroup.TEAM))
    
    # 测试权限
    print("\n权限测试:")
    print(f"CODE_DEV 可调用 bash:", matrix.check_permission("bash", "CODE_DEV"))
    print(f"CODE_DEV 可调用 spawn_teammate:", matrix.check_permission("spawn_teammate", "CODE_DEV"))
    print(f"TEST_EVAL 可调用 spawn_teammate:", matrix.check_permission("spawn_teammate", "TEST_EVAL"))
    
    # 测试允许的工具列表
    print("\nCODE_DEV 允许的工具:", matrix.get_allowed_tools("CODE_DEV")[:5], "...")
    print("GENERAL 允许的工具:", matrix.get_allowed_tools("GENERAL"))
    
    # 测试用户可见工具
    print("\n用户可见工具:", matrix.list_user_visible_tools())
    
    print("\n=== 测试完成 ===")
