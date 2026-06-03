"""
tools/system_tool.py — 系统工具（合并版）

合并 system_info + get_time
"""

import os
import platform
import datetime
from typing import List

from engine.tool.base import BaseTool, ToolParameter, ToolResult


class SystemTool(BaseTool):
    """系统工具 — info + time"""

    @property
    def name(self) -> str:
        return "system"

    @property
    def description(self) -> str:
        return (
            "系统操作。action: info(系统信息)/time(当前时间)/cwd(当前目录)\n"
            "- info: 返回 OS、CPU、内存、磁盘\n"
            "- time: 返回当前日期时间\n"
            "- cwd: 返回当前工作目录"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("action", "string", "info/time/cwd", required=True,
                          enum=["info", "time", "cwd"]),
            ToolParameter("format", "string", "时间格式(time用)，默认 %Y-%m-%d %H:%M:%S", required=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        action = kwargs.get("action", "info")

        if action == "info":
            return await self._info(call_id)
        elif action == "time":
            return await self._time(call_id, kwargs)
        elif action == "cwd":
            return await self._cwd(call_id)
        else:
            return ToolResult.error(call_id, self.name, f"未知操作: {action}")

    async def _info(self, call_id):
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
            info["psutil"] = "未安装 (pip install psutil 获取详细硬件信息)"
        except Exception:
            pass

        return ToolResult.success(call_id, self.name, {
            **info,
            "_hint": f"当前在 {info['os']}，工作目录 {info['cwd']}",
        })

    async def _time(self, call_id, args):
        fmt = args.get("format", "%Y-%m-%d %H:%M:%S")
        now = datetime.datetime.now()
        return ToolResult.success(call_id, self.name, {
            "datetime": now.strftime(fmt),
            "timestamp": now.timestamp(),
            "weekday": now.strftime("%A"),
            "iso": now.isoformat(),
        })

    async def _cwd(self, call_id):
        cwd = os.getcwd()
        items = os.listdir(cwd)[:20]
        return ToolResult.success(call_id, self.name, {
            "cwd": cwd,
            "items": items,
            "count": len(os.listdir(cwd)),
        })
