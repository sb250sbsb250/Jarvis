"""
Jarvis V3 Engine — DAG 执行图架构

核心设计：
  状态机 → 执行图 (WorkflowGraph)
  隐式行为 → 显式节点 (Node + 端口)
  全局 context → 显式数据流
  线性循环 → 任意 DAG 拓扑 + 并行
"""

# ── DAG 核心 ──
from .dag import (
    # 节点
    Node, NodeInput, NodeOutput,
    LLMNode, ToolNode, RouterNode, ParallelNode,
    HumanInLoopNode, MapNode, ToolDispatchNode,
    # 边
    Edge, ConditionalEdge,
    # 图
    WorkflowGraph,
    # 上下文
    ExecutionContext, NodeTrace,
    # 执行器
    GraphExecutor, HumanInterruptError,
    # 构建器
    AgentGraphBuilder,
)

# ── 基础设施（保留） ──
from .llm_client import LLMClient
from .tool.registry import ToolRegistry
from .tool.base import BaseTool, ToolSchema, ToolParameter
from .tool.executor import ToolExecutor
from .tool.policy import ToolPolicy, AccessLevel
from .message.message_list import MessageList
from .session.session import Session
from .core.types import Message, ToolCall, ToolResult, Role
from .core.errors import (
    EngineError, ToolNotFoundError, ToolExecutionError,
    LoopTimeoutError, MaxRetriesExceededError,
)
from .storage.file_store import FileMessageStore
from .storage.store import MessageStore

__all__ = [
    # DAG
    "Node", "NodeInput", "NodeOutput",
    "LLMNode", "ToolNode", "RouterNode", "ParallelNode",
    "HumanInLoopNode", "MapNode", "ToolDispatchNode",
    "Edge", "ConditionalEdge",
    "WorkflowGraph",
    "ExecutionContext", "NodeTrace",
    "GraphExecutor", "HumanInterruptError",
    "AgentGraphBuilder",
    # 基础设施
    "LLMClient", "ToolRegistry",
    "BaseTool", "ToolSchema", "ToolParameter",
    "ToolExecutor", "ToolPolicy", "AccessLevel",
    "MessageList", "Session",
    "Message", "ToolCall", "ToolResult", "Role",
    "EngineError", "ToolNotFoundError", "ToolExecutionError",
]
