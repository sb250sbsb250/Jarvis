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


class ToolRetryExhaustedError(EngineError):
    """工具重试耗尽"""
    def __init__(self, tool_name: str, attempts: int, last_error: str):
        super().__init__(f"Tool '{tool_name}' failed after {attempts} attempts: {last_error}")
        self.tool_name = tool_name
        self.attempts = attempts
        self.last_error = last_error


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


# ═══════════════════════════════════════
#  Skill 层级异常
# ═══════════════════════════════════════

class SkillExecutionError(EngineError):
    """Skill 执行失败"""
    def __init__(self, skill_name: str, message: str, suggestion: str = None):
        super().__init__(f"Skill '{skill_name}': {message}")
        self.skill_name = skill_name
        self.suggestion = suggestion
