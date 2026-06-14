"""
Shell 工具（原子工具版）

原子工具:
  shell_run  — 执行 Shell 命令或 Python 代码
"""

import asyncio
import os
import re
import logging
import tempfile
from typing import List

from engine.tool.base import (
    BaseTool, ToolDefinition, ToolParameter, ToolResult,
    CATEGORY_SYSTEM,
)

logger = logging.getLogger(__name__)


class ShellExecuteTool(BaseTool):
    """Shell 命令执行工具"""

    def __init__(self, **kwargs):
        self._default_timeout = kwargs.get("timeout", 30)
        self._handlers = {"shell_run": self._handle_run}
        for t in self.tools:
            t.handler = self._handlers.get(t.name)

    @property
    def name(self) -> str:
        return "shell"

    @property
    def category(self) -> str:
        return CATEGORY_SYSTEM

    @property
    def tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="shell_run",
                description="""执行 Shell 命令或 Python 代码。

用法1 — 简单命令：shell_run(command='dir')  适合单行命令
用法2 — Python 代码：shell_run(code='...')  适合多行 Python（自动写临时文件执行）

推荐用 code 参数执行多行 Python，避免 JSON 转义和引号嵌套问题。

使用场景：
- 运行构建/编译命令（python build.py, npm run build）
- 检查系统状态（dir, ls, git status, pip list）
- 执行数据分析脚本
- 运行自动化测试""",
                parameters=[
                    ToolParameter("command", "string", "Shell 命令字符串（简单命令用 command，多行脚本用 code）", required=False),
                    ToolParameter("code", "string", "Python 代码（多行用，自动写入 .py 临时文件再执行，避免 JSON 转义问题）", required=False),
                    ToolParameter("timeout", "number", "超时秒数，默认 30。大数据处理/编译等操作应适当增加", required=False),
                ],
                examples=[
                    'shell_run(command="dir")  # Windows 下列出目录',
                    'shell_run(command="python -m pytest tests/")  # 运行测试',
                    'shell_run(code="import os\\nfor f in os.listdir(\".\"):\\n    print(f)")  # 多行Python',
                    'shell_run(command="pip list --format=columns", timeout=60)  # 设置更长超时',
                ],
                constraints=[
                    "Windows 用 dir/cls/type，Linux/Mac 用 ls/clear/cat",
                    "默认超时 30 秒，长时间任务（安装依赖、大数据处理）请增加 timeout",
                    "含有管道/重定向/复杂转义的复杂命令，拆分为多个简单命令或直接用 code 参数",
                    "连续 2 次 shell_run 失败后，不要重复相同的命令，考虑换方案",
                    "file_rename 后目录变动不会自动同步，shell_run 默认在工作目录执行",
                ],
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

    async def _handle_run(self, call_id: str, command: str = "", code: str = "",
                          timeout: int = None) -> ToolResult:
        timeout = timeout or self._default_timeout

        # 优先处理 code 参数
        if code:
            command = self._prepare_code_execution(code)
            logger.info(f"执行 Python 代码 ({len(code)} 字符)")
        elif not command:
            return ToolResult.fail(call_id, "shell_run", "需要 command 或 code 参数")

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=True,
            )
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )

            stdout = self._decode(stdout_data)
            stderr = self._decode(stderr_data)

            if not stdout.strip() and not stderr.strip():
                stdout = "命令执行完成，无输出"

            return ToolResult.ok(call_id, "shell_run", {
                "stdout": stdout,
                "stderr": stderr,
                "return_code": proc.returncode,
            })

        except asyncio.TimeoutError:
            return ToolResult.fail(call_id, "shell_run",
                f"执行超时（{timeout}秒），如需更长超时请增加 timeout 参数")
        except Exception as e:
            return ToolResult.fail(call_id, "shell_run", str(e))

    @staticmethod
    def _decode(data: bytes) -> str:
        try:
            return data.decode("utf-8").strip()
        except UnicodeDecodeError:
            try:
                return data.decode("gbk").strip()
            except UnicodeDecodeError:
                return data.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _prepare_code_execution(code: str) -> str:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", encoding="utf-8", delete=False,
        )
        tmp.write(code)
        tmp.close()
        return f'python "{tmp.name}"'
