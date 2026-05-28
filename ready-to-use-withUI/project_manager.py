#!/usr/bin/env python3
"""
项目管理模块 (Project Manager)
解决核心问题：将用户项目与源代码分离，每个项目独立存储历史信息

设计理念：
1. 每个项目有独立的工作目录
2. 每个项目独立存储：对话历史、工具调用记录、ToDo、决策记录
3. 工具定义全局共享，不随项目迁移
4. 支持项目列表管理、创建、删除、切换

数据结构：
- projects.json: 项目注册表（存储在框架根目录）
- <project_path>/.pdm/: 项目专属数据目录
  - chat_history.json: 对话历史
  - tool_calls.json: 工具调用记录
  - todo.json: 待办事项
  - intent.json: 意图记录
  - decisions.json: 决策记录
"""

import json
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime
import shutil
import os


@dataclass
class ProjectInfo:
    name: str
    path: str
    description: str = ""
    created_at: str = ""
    last_accessed: str = ""
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "ProjectInfo":
        return cls(**data)


@dataclass
class ProjectData:
    messages: List[dict] = field(default_factory=list)
    tool_calls_history: List[dict] = field(default_factory=list)
    todo_items: List[dict] = field(default_factory=list)
    intent_data: dict = field(default_factory=dict)
    decisions: List[dict] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "ProjectData":
        if not data:
            return cls()
        return cls(
            messages=data.get("messages", []),
            tool_calls_history=data.get("tool_calls_history", []),
            todo_items=data.get("todo_items", []),
            intent_data=data.get("intent_data", {}),
            decisions=data.get("decisions", [])
        )


class ProjectManager:
    """
    项目管理器 - 管理多个项目及其数据
    
    核心功能：
    1. 项目注册/注销
    2. 项目切换
    3. 项目数据加载/保存
    4. 项目列表查询
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
        
        # 框架根目录（源代码所在目录的父目录）
        self._framework_root = Path(__file__).parent.parent
        
        # 项目注册表路径
        self._registry_path = self._framework_root / "projects.json"
        
        # 当前项目
        self._current_project: Optional[ProjectInfo] = None
        self._current_data: ProjectData = ProjectData()
        
        # 数据锁
        self._data_lock = threading.RLock()
        
        # 加载项目注册表
        self._projects: Dict[str, ProjectInfo] = {}
        self._load_registry()
        
        # 自动恢复上次项目
        self._restore_last_project()
    
    def _load_registry(self):
        """加载项目注册表"""
        if self._registry_path.exists():
            try:
                data = json.loads(self._registry_path.read_text(encoding="utf-8"))
                self._projects = {
                    path: ProjectInfo.from_dict(info)
                    for path, info in data.get("projects", {}).items()
                }
                self._last_project_path = data.get("last_project_path")
            except Exception as e:
                print(f"加载项目注册表失败: {e}")
                self._projects = {}
                self._last_project_path = None
        else:
            self._projects = {}
            self._last_project_path = None
    
    def _save_registry(self):
        """保存项目注册表"""
        data = {
            "projects": {
                path: info.to_dict()
                for path, info in self._projects.items()
            },
            "last_project_path": self._current_project.path if self._current_project else None
        }
        self._registry_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    
    def _restore_last_project(self):
        """恢复上次打开的项目"""
        if self._last_project_path and self._last_project_path in self._projects:
            self.switch_project(self._last_project_path)
    
    def _get_project_data_dir(self, project_path: str) -> Path:
        """获取项目数据目录"""
        return Path(project_path) / ".pdm"
    
    def _init_project_data_dir(self, project_path: str):
        """初始化项目数据目录"""
        data_dir = self._get_project_data_dir(project_path)
        data_dir.mkdir(parents=True, exist_ok=True)
        
        # 创建 gitignore（避免将 .pdm 目录提交到版本控制）
        gitignore_path = Path(project_path) / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text(".pdm/\n", encoding="utf-8")
        else:
            content = gitignore_path.read_text(encoding="utf-8")
            if ".pdm/" not in content:
                gitignore_path.write_text(content + "\n.pdm/\n", encoding="utf-8")
    
    def register_project(
        self,
        project_path: str,
        name: str = None,
        description: str = ""
    ) -> ProjectInfo:
        """
        注册一个新项目
        
        Args:
            project_path: 项目目录路径
            name: 项目名称（默认使用目录名）
            description: 项目描述
        
        Returns:
            ProjectInfo
        """
        path = Path(project_path).resolve()
        
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        
        path_str = str(path)
        name = name or path.name
        
        # 检查是否已注册
        if path_str in self._projects:
            return self._projects[path_str]
        
        # 创建项目信息
        project_info = ProjectInfo(
            name=name,
            path=path_str,
            description=description,
            created_at=datetime.now().isoformat(),
            last_accessed=datetime.now().isoformat()
        )
        
        # 初始化项目数据目录
        self._init_project_data_dir(path_str)
        
        # 注册
        with self._data_lock:
            self._projects[path_str] = project_info
        
        self._save_registry()
        
        return project_info
    
    def unregister_project(self, project_path: str) -> bool:
        """
        注销项目（不删除项目文件，只移除注册）
        
        Args:
            project_path: 项目路径
        
        Returns:
            是否成功
        """
        path_str = str(Path(project_path).resolve())
        
        if path_str not in self._projects:
            return False
        
        # 如果是当前项目，先保存
        if self._current_project and self._current_project.path == path_str:
            self.save_current_project()
            self._current_project = None
            self._current_data = ProjectData()
        
        with self._data_lock:
            del self._projects[path_str]
        
        self._save_registry()
        return True
    
    def switch_project(self, project_path: str) -> bool:
        """
        切换到指定项目
        
        Args:
            project_path: 项目路径
        
        Returns:
            是否成功
        """
        path_str = str(Path(project_path).resolve())
        
        # 检查项目是否已注册
        if path_str not in self._projects:
            # 自动注册
            self.register_project(path_str)
        
        # 保存当前项目数据
        if self._current_project:
            self.save_current_project()
        
        # 切换项目
        project_info = self._projects[path_str]
        
        # 确保数据目录存在
        self._init_project_data_dir(path_str)
        
        # 加载项目数据
        self._current_project = project_info
        self._current_data = self._load_project_data(path_str)
        
        # 更新访问时间
        project_info.last_accessed = datetime.now().isoformat()
        self._save_registry()
        
        return True
    
    def _load_project_data(self, project_path: str) -> ProjectData:
        """加载项目数据"""
        data_dir = self._get_project_data_dir(project_path)
        
        data = {}
        
        # 加载对话历史
        chat_file = data_dir / "chat_history.json"
        if chat_file.exists():
            try:
                data["messages"] = json.loads(chat_file.read_text(encoding="utf-8"))
            except Exception:
                data["messages"] = []
        
        # 加载工具调用记录
        tool_calls_file = data_dir / "tool_calls.json"
        if tool_calls_file.exists():
            try:
                data["tool_calls_history"] = json.loads(tool_calls_file.read_text(encoding="utf-8"))
            except Exception:
                data["tool_calls_history"] = []
        
        # 加载待办事项
        todo_file = data_dir / "todo.json"
        if todo_file.exists():
            try:
                data["todo_items"] = json.loads(todo_file.read_text(encoding="utf-8"))
            except Exception:
                data["todo_items"] = []
        
        # 加载意图数据
        intent_file = data_dir / "intent.json"
        if intent_file.exists():
            try:
                data["intent_data"] = json.loads(intent_file.read_text(encoding="utf-8"))
            except Exception:
                data["intent_data"] = {}
        
        # 加载决策记录
        decisions_file = data_dir / "decisions.json"
        if decisions_file.exists():
            try:
                data["decisions"] = json.loads(decisions_file.read_text(encoding="utf-8"))
            except Exception:
                data["decisions"] = []
        
        return ProjectData.from_dict(data)
    
    def save_current_project(self):
        """保存当前项目数据"""
        if not self._current_project:
            return
        
        data_dir = self._get_project_data_dir(self._current_project.path)
        data_dir.mkdir(parents=True, exist_ok=True)
        
        with self._data_lock:
            data = self._current_data.to_dict()
        
        # 保存各个数据文件
        (data_dir / "chat_history.json").write_text(
            json.dumps(data.get("messages", []), indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        
        (data_dir / "tool_calls.json").write_text(
            json.dumps(data.get("tool_calls_history", []), indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        
        (data_dir / "todo.json").write_text(
            json.dumps(data.get("todo_items", []), indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        
        (data_dir / "intent.json").write_text(
            json.dumps(data.get("intent_data", {}), indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        
        (data_dir / "decisions.json").write_text(
            json.dumps(data.get("decisions", []), indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    
    def get_current_project(self) -> Optional[ProjectInfo]:
        """获取当前项目信息"""
        return self._current_project
    
    def get_current_project_path(self) -> Optional[str]:
        """获取当前项目路径"""
        return self._current_project.path if self._current_project else None
    
    def get_current_data(self) -> ProjectData:
        """获取当前项目数据"""
        with self._data_lock:
            return self._current_data
    
    def update_current_data(self, **kwargs):
        """更新当前项目数据"""
        with self._data_lock:
            for key, value in kwargs.items():
                if hasattr(self._current_data, key):
                    setattr(self._current_data, key, value)
    
    def list_projects(self) -> List[ProjectInfo]:
        """列出所有已注册项目"""
        return list(self._projects.values())
    
    def get_project_info(self, project_path: str) -> Optional[ProjectInfo]:
        """获取项目信息"""
        path_str = str(Path(project_path).resolve())
        return self._projects.get(path_str)
    
    def clear_current_project_data(self):
        """清空当前项目数据"""
        with self._data_lock:
            self._current_data = ProjectData()
        self.save_current_project()
    
    def delete_project_data(self, project_path: str) -> bool:
        """
        删除项目数据（不删除项目本身，只删除 .pdm 目录）
        
        Args:
            project_path: 项目路径
        
        Returns:
            是否成功
        """
        data_dir = self._get_project_data_dir(project_path)
        if data_dir.exists():
            shutil.rmtree(data_dir)
            return True
        return False


# 全局单例
_project_manager: Optional[ProjectManager] = None
_pm_lock = threading.RLock()


def get_project_manager() -> ProjectManager:
    """获取项目管理器单例"""
    global _project_manager
    if _project_manager is None:
        with _pm_lock:
            if _project_manager is None:
                _project_manager = ProjectManager()
    return _project_manager


def get_current_workdir() -> Path:
    """获取当前工作目录（项目路径或默认路径）"""
    pm = get_project_manager()
    project_path = pm.get_current_project_path()
    if project_path:
        return Path(project_path)
    return Path.cwd()


def set_workdir(path: str) -> bool:
    """
    设置工作目录（切换或创建项目）
    
    Args:
        path: 项目路径
    
    Returns:
        是否成功
    """
    pm = get_project_manager()
    return pm.switch_project(path)
