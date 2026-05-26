"""
进程工具 — 管理运行的进程
"""

import logging
import asyncio
from typing import List

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.process")


class ProcessListTool(BaseTool):

    def __init__(self, **kwargs):
        pass

    """列出进程"""

    @property
    def name(self) -> str:
        return "process_list"

    @property
    def description(self) -> str:
        return "列出系统进程"

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="filter", type="string", description="过滤关键词", required=False, default=""),
            ToolParameter(name="max_results", type="number", description="最大返回数", required=False, default=20),
        ]

    async def execute(self, call_id, **kwargs) -> ToolResult:
        filter_str = kwargs.get("filter", "")
        max_results = kwargs.get("max_results", 20)
        try:
            import psutil
            processes = []
            for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
                try:
                    pinfo = proc.info
                    if filter_str and filter_str.lower() not in pinfo["name"].lower():
                        continue
                    processes.append({
                        "pid": pinfo["pid"],
                        "name": pinfo["name"],
                        "cpu": pinfo["cpu_percent"],
                        "memory": round(pinfo["memory_percent"], 1),
                    })
                    if len(processes) >= max_results:
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return ToolResult.success(call_id, self.name, {"processes": processes, "count": len(processes)})
        except ImportError:
            return ToolResult.error(call_id, self.name, "需要安装 psutil: pip install psutil")
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))


