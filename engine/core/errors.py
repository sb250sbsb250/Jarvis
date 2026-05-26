"""
core/errors.py — 自定义异常定义
"""


class EngineError(Exception):
    """引擎基础异常"""
    pass


class ToolNotFoundError(EngineError):
    """工具未找到"""
    pass


class ToolExecutionError(EngineError):
    """工具执行失败"""
    pass


class InvalidStateTransitionError(EngineError):
    """非法状态转换"""
    pass


class LoopTimeoutError(EngineError):
    """循环超时"""
    pass


class MaxRetriesExceededError(EngineError):
    """超过最大重试次数"""
    pass


class SessionNotFoundError(EngineError):
    """会话未找到"""
    pass


class MessageLimitExceededError(EngineError):
    """消息数量超限"""
    pass
