"""核心类型和错误定义"""
from .types import Message, ToolCall, ToolResult, Role
from .errors import (
    EngineError, ToolNotFoundError, ToolExecutionError,
    LoopTimeoutError, MaxRetriesExceededError,
)
from .guard import GuardState, ENABLE_RESULT_CACHE, ENABLE_HARD_INTERRUPT

__all__ = [
    "Message", "ToolCall", "ToolResult", "Role",
    "EngineError", "ToolNotFoundError", "ToolExecutionError",
    "LoopTimeoutError", "MaxRetriesExceededError",
    "GuardState", "ENABLE_RESULT_CACHE", "ENABLE_HARD_INTERRUPT",
]
