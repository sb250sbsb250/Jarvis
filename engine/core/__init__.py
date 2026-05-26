"""核心类型和错误定义"""
from .types import Message, ToolCall, ToolResult, Role
from .errors import (
    EngineError, ToolNotFoundError, ToolExecutionError,
    LoopTimeoutError, MaxRetriesExceededError,
)
