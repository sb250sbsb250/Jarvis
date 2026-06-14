# engine/__init__.py
"""
Jarvis V3 Engine — 自主 Agent 引擎

核心设计：
  AgentLoop — 纯 LLM + 工具循环（Claude Code 模式）
  Skill — 纯配置驱动的 system prompt 提供者
  Tool — 统一的工具注册和执行系统
"""

from .agent_loop import AgentLoop
from .llm_client import LLMClient
from .session.session import Session
from .tool.registry import ToolRegistry
from .tool.base import BaseTool, ToolParameter
from .tool.base import ToolDefinition as ToolSchema  # 向后兼容
from .tool.executor import ToolExecutor
from .tool.policy import ToolPolicy, AccessLevel

__all__ = [
    "AgentLoop", "LLMClient",
    "Session",
    "ToolRegistry", "BaseTool", "ToolSchema", "ToolParameter",
    "ToolExecutor", "ToolPolicy", "AccessLevel",
]