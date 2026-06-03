"""
tools/shell_tool.py — Shell 命令工具（带智能输出恢复）
"""

import asyncio
import os
import logging
from typing import List

from engine.tool.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class ShellExecuteTool(BaseTool):
    """Shell 命令 — 自动处理编码和空输出"""

    def __init__(self, **kwargs):
        self._default_timeout = kwargs.get("timeout", 30)

    @property
    def name(self) -> str:
        return "shell"

    @property
    def description(self) -> str:
        return (
            "执行 Shell 命令，自动处理输出捕获。\n"
            "- command='dir' 或 'python script.py'\n"
            "- timeout=30 (秒)\n"
            "Python 脚本自动加 -u 禁用缓冲。输出为空时自动写文件再读。"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("command", "string", "Shell 命令", required=True),
            ToolParameter("timeout", "number", "超时秒数，默认30", required=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        command = kwargs.get("command", "")
        timeout = float(kwargs.get("timeout", self._default_timeout))

        if not command:
            return ToolResult.error(call_id, self.name, "需要 command 参数")

        # 自动给 python 命令加 -u 禁用缓冲
        if command.startswith("python ") and "-u" not in command:
            command = command.replace("python ", "python -u ", 1)

        # 尝试1：直接执行
        result = await self._run(command, timeout)

        # 尝试2：输出为空 → 写文件再读
        if not result["stdout"].strip() and not result["stderr"].strip():
            temp_file = f"_jarvis_shell_out_{call_id[:8]}.txt"
            fallback = f'{command} > "{temp_file}" 2>&1'
            await self._run(fallback, timeout)

            try:
                if os.path.exists(temp_file):
                    with open(temp_file, "r", encoding="utf-8", errors="replace") as f:
                        file_content = f.read()
                    if file_content.strip():
                        result["stdout"] = file_content
                        result["_recovered"] = True
                    os.remove(temp_file)
            except Exception:
                pass

        # 构建返回
        response = {
            "command": command[:200],
            "stdout": result["stdout"][:8000],
            "stderr": result["stderr"][:2000],
            "exit_code": result["exit_code"],
        }

        # 智能提示
        if not result["stdout"].strip() and not result["stderr"].strip():
            response["_hint"] = (
                "命令没有输出。可能原因:\n"
                "1. 文件路径错误 → 检查路径和反斜杠转义\n"
                "2. sheet/文件名不存在 → 先用相关工具列出可用的\n"
                "3. 缺少依赖 → pip install 需要的库\n"
                "请排查原因，不要重复执行相同命令"
            )
        elif result["exit_code"] != 0:
            response["_hint"] = f"命令返回非零退出码 {result['exit_code']}。检查 stderr 中的错误信息"

        return ToolResult.success(call_id, self.name, response)

    async def _run(self, command: str, timeout: float) -> dict:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "exit_code": proc.returncode or 0,
            }
        except asyncio.TimeoutError:
            return {"stdout": "", "stderr": f"超时 ({timeout}s)", "exit_code": -1}
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "exit_code": -1}
