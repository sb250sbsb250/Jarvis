"""
Git 工具 — 3合1：状态/提交/推送
"""

import os
import logging
from typing import List

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.git")


class GitTool(BaseTool):
    """Git 操作工具（3合1）"""

    def __init__(self, **kwargs):
        pass

    @property
    def name(self) -> str:
        return "git"

    @property
    def description(self) -> str:
        return ("Git 操作工具。action 可选："
                "status(查看状态) / commit(提交) / push(推送)。"
                "标准流程：status → commit → push")

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="action", type="string", required=True,
                          description="操作: status/commit/push",
                          enum=["status", "commit", "push"]),
            ToolParameter(name="message", type="string", required=False,
                          description="commit 时的提交信息"),
            ToolParameter(name="path", type="string", required=False,
                          description="仓库路径(默认当前目录)", default="."),
            ToolParameter(name="add_all", type="boolean", required=False,
                          description="commit 前是否自动 git add .",
                          default=True),
            ToolParameter(name="remote", type="string", required=False,
                          description="push 时的远程名称", default="origin"),
            ToolParameter(name="branch", type="string", required=False,
                          description="push 时的分支名（默认当前分支）"),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        action = kwargs.get("action", "status")
        path = kwargs.get("path", ".")

        try:
            if action == "status":
                return await self._status(call_id, path)
            elif action == "commit":
                return await self._commit(call_id, path,
                                          kwargs.get("message", ""),
                                          kwargs.get("add_all", True))
            elif action == "push":
                return await self._push(call_id, path,
                                        kwargs.get("remote", "origin"),
                                        kwargs.get("branch", ""))
            else:
                return ToolResult.error(call_id, self.name, f"未知操作: {action}")
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))

    async def _status(self, call_id: str, path: str) -> ToolResult:
        import subprocess
        result = subprocess.run(["git", "status"], cwd=path,
                                capture_output=True, text=True, timeout=10)
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=path,
                                capture_output=True, text=True, timeout=5)
        return ToolResult.success(call_id, self.name, {
            "branch": branch.stdout.strip(),
            "status": result.stdout,
            "dirty": bool(result.stdout.strip()),
        })

    async def _commit(self, call_id: str, path: str,
                      message: str, add_all: bool) -> ToolResult:
        import subprocess
        if not message:
            return ToolResult.error(call_id, self.name, "commit 需要提供 message 参数")
        if add_all:
            subprocess.run(["git", "add", "."], cwd=path,
                           capture_output=True, timeout=10)
        result = subprocess.run(["git", "commit", "-m", message], cwd=path,
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return ToolResult.success(call_id, self.name,
                                      {"output": result.stdout.strip()})
        return ToolResult.error(call_id, self.name, result.stderr.strip())

    async def _push(self, call_id: str, path: str,
                    remote: str, branch: str) -> ToolResult:
        import subprocess
        if not branch:
            branch = subprocess.run(["git", "branch", "--show-current"],
                                    cwd=path, capture_output=True, text=True,
                                    timeout=5).stdout.strip()
        result = subprocess.run(["git", "push", remote, branch], cwd=path,
                                capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            return ToolResult.success(call_id, self.name,
                                      {"output": result.stdout.strip()})
        return ToolResult.error(call_id, self.name, result.stderr.strip())
