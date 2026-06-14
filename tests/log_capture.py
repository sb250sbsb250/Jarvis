"""
log_capture.py — 日志捕获器

运行测试并捕获所有日志，用于发现隐藏的警告、错误、异常。
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple
from dataclasses import dataclass, field

# 确保项目根在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass
class LogEntry:
    """单条日志记录"""
    level: str
    message: str
    module: str
    lineno: int = 0


class LogCapture:
    """
    日志捕获器 — 捕获运行期间的日志用于分析。

    用法：
        capture = LogCapture()
        capture.start()
        # ... 运行测试 ...
        capture.stop()
        capture.print_report()
    """

    def __init__(self):
        self.logs: List[LogEntry] = []
        self._handler: Optional[logging.Handler] = None
        self._original_level = logging.WARNING

    def start(self, level: int = logging.DEBUG):
        """开始捕获日志"""
        self.logs.clear()
        self._original_level = logging.getLogger().level

        class _CaptureHandler(logging.Handler):
            def __init__(parent):
                super().__init__()
                self_ref = self
                self_ref._handler = self

            def emit(record):
                self.logs.append(
                    LogEntry(
                        level=record.levelname,
                        message=record.getMessage(),
                        module=record.name,
                        lineno=record.lineno,
                    )
                )

        self._handler = _CaptureHandler()
        self._handler.setLevel(level)
        logging.root.addHandler(self._handler)

    def stop(self):
        """停止捕获日志"""
        if self._handler:
            logging.root.removeHandler(self._handler)
            self._handler = None

    def get_errors(self) -> List[LogEntry]:
        """获取所有 ERROR 级别日志"""
        return [l for l in self.logs if l.level == "ERROR"]

    def get_warnings(self) -> List[LogEntry]:
        """获取所有 WARNING 级别日志"""
        return [l for l in self.logs if l.level == "WARNING"]

    def get_critical(self) -> List[LogEntry]:
        """获取所有 CRITICAL 级别日志"""
        return [l for l in self.logs if l.level == "CRITICAL"]

    def get_by_module(self, module_name: str) -> List[LogEntry]:
        """按模块名过滤日志"""
        return [l for l in self.logs if module_name in l.module]

    def print_report(self) -> bool:
        """
        打印日志分析报告。

        Returns:
            是否无错误
        """
        errors = self.get_errors()
        criticals = self.get_critical()
        warnings = self.get_warnings()

        print(f"\n📋  日志捕获报告")
        print(f"     总日志: {len(self.logs)}")
        print(f"     警告:   {len(warnings)}")
        print(f"     错误:   {len(errors)}")
        print(f"     严重:   {len(criticals)}")

        if warnings:
            print(f"\n⚠️  警告列表 (前 {min(10, len(warnings))} 条):")
            for w in warnings[:10]:
                print(f"     [{w.module}:{w.lineno}] {w.message[:120]}")

        if errors:
            print(f"\n❌  错误列表:")
            for e in errors:
                print(f"     [{e.module}:{e.lineno}] {e.message[:200]}")

        if criticals:
            print(f"\n💀  严重错误:")
            for c in criticals:
                print(f"     [{c.module}:{c.lineno}] {c.message[:200]}")

        has_issues = len(errors) > 0 or len(criticals) > 0
        if has_issues:
            print(f"\n⚠️  发现 {len(errors) + len(criticals)} 个错误")
        else:
            print(f"\n✅  无错误日志")

        return not has_issues

    def clear(self):
        """清空捕获的日志"""
        self.logs.clear()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()


async def run_with_capture(
    func: Callable, *args, **kwargs
) -> Tuple[Any, LogCapture]:
    """
    在日志捕获下运行函数/协程。

    Args:
        func: 同步或异步函数
        *args, **kwargs: 传给 func 的参数

    Returns:
        (func_result, LogCapture)
    """
    capture = LogCapture()
    capture.start()

    try:
        if asyncio.iscoroutinefunction(func):
            result = await func(*args, **kwargs)
        else:
            result = func(*args, **kwargs)
        return result, capture
    finally:
        capture.stop()


def main():
    """演示用法"""
    capture = LogCapture()
    capture.start()

    import logging

    logger = logging.getLogger("demo")
    logger.info("这是一条 info 日志")
    logger.warning("这是一条警告")
    logger.error("这是一条错误")

    capture.stop()
    capture.print_report()


if __name__ == "__main__":
    main()
