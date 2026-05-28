"""
Shell 命令工具 — 导入时自动注册
"""

import asyncio
import logging
from typing import List, Optional

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.shell")


class ShellExecuteTool(BaseTool):

    def __init__(self, **kwargs):
        self._default_timeout = kwargs.get("timeout", 30)

    """执行 Shell 命令"""


    @property
    def name(self) -> str:
        return "shell_execute"

    @property
    def description(self) -> str:
        return (
            "在服务器上执行 Shell 命令。Windows 用 dir/pwd/cd，Linux 用 ls/pwd/cd\n"
            "\n"
            "📖 使用示例：\n"
            "  # Windows 下:\n"
            "  execute(command='dir', workdir='C:\\project')\n"
            "  execute(command='python --version')\n"
            "  # Linux 下:\n"
            "  execute(command='ls -la', workdir='/home/user')\n"
            "  # 超时控制:\n"
            "  execute(command='long_task', timeout=60)\n"
            "  💡 默认超时 30 秒，长任务设 timeout。\n"
            "  💡 工作目录用 workdir 参数切换，不用 cd 命令。\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="command", type="string", description="要执行的命令", required=True),
            ToolParameter(name="timeout", type="number", description="超时时间（秒）", required=False, default=self._default_timeout),
        ]

    async def execute(self, call_id, **kwargs) -> ToolResult:
        command = kwargs.get("command", "")
        timeout = kwargs.get("timeout", self._default_timeout)
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return ToolResult.success(call_id, self.name, {
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "exit_code": process.returncode,
            })
        except asyncio.TimeoutError:
            return ToolResult.error(call_id, self.name, f"命令执行超时（{timeout}s）: {command}")
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))


