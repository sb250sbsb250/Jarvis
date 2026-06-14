"""
core/logging_config.py — 统一结构化日志配置

用法:
    from engine.core.logging_config import setup_logging
    setup_logging(level="INFO")
"""
import logging
import sys
from typing import Optional


def setup_logging(
    level: str = "INFO",
    json_output: bool = False,
    component_filter: Optional[str] = None,
):
    """
    统一日志配置

    Args:
        level: 日志级别 (DEBUG/INFO/WARNING/ERROR)
        json_output: 是否输出 JSON 格式（生产环境用）
        component_filter: 仅显示特定组件的日志（如 "dag"、"llm"）
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 清除已有 handler
    root.handlers.clear()

    if json_output:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JSONFormatter())
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_ColoredFormatter())

    root.addHandler(handler)

    # 第三方库日志降噪
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


class _ColoredFormatter(logging.Formatter):
    """带颜色的控制台日志格式"""

    COLORS = {
        "DEBUG": "\033[36m",     # 青色
        "INFO": "\033[32m",      # 绿色
        "WARNING": "\033[33m",   # 黄色
        "ERROR": "\033[31m",     # 红色
        "CRITICAL": "\033[41m",  # 红底
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        level = f"{color}{record.levelname:8s}{self.RESET}"
        name = f"{record.name:<20s}"
        msg = record.getMessage()
        return f"{level} {name} {msg}"


class _JSONFormatter(logging.Formatter):
    """JSON 格式日志（生产环境）"""

    def format(self, record: logging.LogRecord) -> str:
        import json
        import datetime

        log_entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, ensure_ascii=False)
