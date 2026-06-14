"""
conftest.py — pytest 全局配置和 fixtures

提供所有测试所需的共享 fixtures：
  - project_root: 项目根目录
  - message_list: 空的 MessageList 实例
  - skill_registry: 空的 SkillRegistry 实例
  - tool_registry: 空的 ToolRegistry 实例（懒加载）
  - task_manager: 空的 TaskContextManager 实例
  - sample_messages: 预填充的消息列表
"""

import sys
from pathlib import Path

import pytest

# 确保项目根在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Fixtures ──


@pytest.fixture(scope="session")
def project_root() -> Path:
    """项目根目录"""
    return PROJECT_ROOT


@pytest.fixture
def message_list():
    """空的 MessageList 实例"""
    from engine.message.message_list import MessageList
    return MessageList()


@pytest.fixture
def skill_registry():
    """空的 SkillRegistry 实例"""
    from engine.skill.registry import SkillRegistry
    return SkillRegistry()


@pytest.fixture
def tool_registry():
    """空的 ToolRegistry 实例（懒加载）"""
    from engine.tool.registry import ToolRegistry
    return ToolRegistry()


@pytest.fixture
def task_manager():
    """空的 TaskContextManager 实例"""
    from engine.context.task_manager import TaskContextManager
    return TaskContextManager()


@pytest.fixture
def sample_messages(message_list):
    """预填充 3 轮对话的消息列表"""
    message_list.add_user("审查这段代码")
    message_list.add_assistant("正在审查...")
    message_list.add_user("分析性能问题")
    message_list.add_assistant("分析完成")
    message_list.add_user("帮我优化")
    message_list.add_assistant("优化建议...")
    return message_list


@pytest.fixture
def complexity_router():
    """复杂度路由器"""
    from engine.prompt.complexity import ComplexityRouter
    return ComplexityRouter


@pytest.fixture
def state_store(tmp_path):
    """临时 SQLite 状态存储"""
    from engine.storage.state_store import StateStore
    db_path = str(tmp_path / "test_state.db")
    return StateStore(db_path=db_path)
