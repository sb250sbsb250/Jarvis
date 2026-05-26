"""
系统工具 — 导入时自动注册
"""

import os
import platform
import logging
from typing import List

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.system")


class SystemInfoTool(BaseTool):

    def __init__(self, **kwargs):
        pass

    """获取系统信息"""

    @property
    def name(self) -> str:
        return "system_info"

    @property
    def description(self) -> str:
        return "获取系统信息（OS、CPU、内存、磁盘）"

    @property
    def parameters(self) -> List[ToolParameter]:
        return []

    async def execute(self, call_id, **kwargs) -> ToolResult:
        info = {
            "os": platform.system(),
            "hostname": platform.node(),
            "python_version": platform.python_version(),
            "cwd": os.getcwd(),
        }
        try:
            import psutil
            info["cpu_count"] = psutil.cpu_count()
            mem = psutil.virtual_memory()
            info["memory_total_gb"] = round(mem.total / (1024**3), 1)
            info["memory_percent"] = mem.percent
            disk = psutil.disk_usage("/")
            info["disk_total_gb"] = round(disk.total / (1024**3), 1)
            info["disk_percent"] = disk.percent
        except ImportError:
            info["psutil"] = "not available"
        return ToolResult.success(call_id, self.name, info)


class GetTimeTool(BaseTool):


    @property
    def name(self) -> str:
        return "get_time"

    @property
    def description(self) -> str:
        return "获取当前日期和时间"

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="format", type="string", description="时间格式，如 %Y-%m-%d %H:%M:%S", required=False, default="%Y-%m-%d %H:%M:%S"),
        ]

    async def execute(self, call_id, **kwargs) -> ToolResult:
        import datetime
        fmt = kwargs.get("format", "%Y-%m-%d %H:%M:%S")
        now = datetime.datetime.now()
        return ToolResult.success(call_id, self.name, {"datetime": now.strftime(fmt), "timestamp": now.timestamp()})


