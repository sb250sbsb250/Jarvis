"""
tools/system_tool.py — 系统工具（原子工具版）

原子工具:
  system_info  — 系统信息
  system_time  — 当前时间
  system_cwd   — 当前目录
"""

import os
import platform
import datetime
from typing import List

from engine.tool.base import (
    BaseTool, ToolDefinition, ToolParameter, ToolResult,
    CATEGORY_SYSTEM,
)


class SystemTool(BaseTool):
    """系统工具集"""

    def __init__(self):
        self._handlers = {
            "system_info": self._handle_info,
            "system_time": self._handle_time,
            "system_cwd": self._handle_cwd,
        }
        for t in self.tools:
            t.handler = self._handlers.get(t.name)

    @property
    def name(self) -> str:
        return "system"

    @property
    def category(self) -> str:
        return CATEGORY_SYSTEM

    @property
    def tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="system_info",
                description="""获取系统信息：操作系统类型和版本、CPU 核心数、内存大小和使用率、
磁盘空间和使用率、Python 版本、当前工作目录。
需要安装 psutil 获取更详细的硬件信息。

使用场景：
- 了解当前运行环境
- 确认可用资源（内存/磁盘）""",
                parameters=[],
                is_read=True,
                examples=["system_info()"],
                constraints=[
                    "硬件信息（CPU/内存/磁盘）需要安装 psutil：pip install psutil",
                    "没有 psutil 时只返回操作系统和 Python 版本",
                ],
            ),
            ToolDefinition(
                name="system_time",
                description="""获取当前系统的日期和时间。

使用场景：
- 记录操作时间
- 生成带时间戳的文件名""",
                parameters=[
                    ToolParameter("format", "string", "时间格式字符串，默认 '%Y-%m-%d %H:%M:%S'。参考 Python strftime 格式", required=False),
                ],
                is_read=True,
                examples=[
                    'system_time()',
                    'system_time(format="%Y年%m月%d日 %H时%M分")',
                    'system_time(format="%Y%m%d_%H%M%S")  # 用于文件名的时间戳',
                ],
            ),
            ToolDefinition(
                name="system_cwd",
                description="""获取当前工作目录的绝对路径。

使用场景：
- 确认当前文件操作的基础路径
- 需要构建绝对路径时参考""",
                parameters=[],
                is_read=True,
                examples=["system_cwd()"],
            ),
        ]

    async def execute(self, call_id: str, tool_name: str, **kwargs) -> ToolResult:
        handler = self._handlers.get(tool_name)
        if not handler:
            return ToolResult.fail(call_id, tool_name, f"未知工具: {tool_name}")
        try:
            return await handler(call_id, **kwargs)
        except Exception as e:
            return ToolResult.fail(call_id, tool_name, str(e))

    async def _handle_info(self, call_id: str) -> ToolResult:
        info = {
            "os": platform.system(),
            "os_version": platform.version(),
            "hostname": platform.node(),
            "python": platform.python_version(),
            "cwd": os.getcwd(),
        }
        try:
            import psutil
            info["cpu_count"] = psutil.cpu_count()
            mem = psutil.virtual_memory()
            info["memory_total_gb"] = round(mem.total / (1024**3), 1)
            info["memory_used_percent"] = mem.percent
            disk = psutil.disk_usage(os.getcwd())
            info["disk_total_gb"] = round(disk.total / (1024**3), 1)
            info["disk_used_percent"] = disk.percent
        except ImportError:
            pass
        except Exception:
            pass

        return ToolResult.ok(call_id, "system_info", info)

    async def _handle_time(self, call_id: str, format: str = "%Y-%m-%d %H:%M:%S") -> ToolResult:
        now = datetime.datetime.now()
        return ToolResult.ok(call_id, "system_time", {
            "now": now.strftime(format),
            "timestamp": now.timestamp(),
        })

    async def _handle_cwd(self, call_id: str) -> ToolResult:
        return ToolResult.ok(call_id, "system_cwd", {
            "cwd": os.getcwd(),
        })
