"""
Git 工具（原子工具版）

原子工具:
  git_status  — 查看工作区状态
  git_commit  — 提交修改
  git_push    — 推送到远程
"""

import os
import logging
from typing import List

from engine.tool.base import BaseTool, ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger("jarvis.tools.git")


class GitTool(BaseTool):
    """Git 操作工具集"""

    def __init__(self):
        self._handlers = {
            "git_status": self._handle_status,
            "git_commit": self._handle_commit,
            "git_push": self._handle_push,
        }
        for t in self.tools:
            t.handler = self._handlers.get(t.name)

    @property
    def name(self) -> str:
        return "git"

    @property
    def category(self) -> str:
        return "version"

    @property
    def tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="git_status",
                description="""查看 Git 仓库工作区状态（修改/新增/删除/未追踪的文件）。

使用场景：
- 在 commit 前查看有哪些文件被修改了
- 确认当前工作区是否干净
- 快速了解项目的 Git 状态""",
                parameters=[
                    ToolParameter("path", "string", "Git 仓库路径，默认当前工作目录", required=False),
                ],
                is_read=True,
                examples=[
                    'git_status()',
                    'git_status(path="/home/project")',
                ],
                constraints=[
                    "当前目录必须是 Git 仓库（有 .git 目录）",
                    "非 Git 仓库会返回错误",
                ],
            ),
            ToolDefinition(
                name="git_commit",
                description="""提交修改到本地仓库。自动执行 git add . 再 commit。

使用场景：
- 完成功能开发后提交代码
- 修复 bug 后提交

提交前建议：先 git_status 确认有哪些文件被修改""",
                parameters=[
                    ToolParameter("message", "string", "提交信息（建议按约定格式如 'feat: 新增...' 或 'fix: 修复...'）", required=True),
                    ToolParameter("path", "string", "Git 仓库路径，默认当前目录", required=False),
                    ToolParameter("add_all", "boolean", "是否自动执行 git add . 添加所有变更，默认 true", required=False),
                ],
                examples=[
                    'git_commit(message="fix: 修复登录bug")',
                    'git_commit(message="feat: 新增用户导出功能", add_all=True)',
                ],
                constraints=[
                    "提交前请确保有文件修改（建议先 git_status 检查）",
                    "message 参数是必需的，不能为空",
                    "add_all=true 会添加所有未追踪的文件，如只想提交部分文件请设为 false",
                    "commit 只提交到本地，如需推送到远程请再调用 git_push",
                ],
            ),
            ToolDefinition(
                name="git_push",
                description="""推送本地提交到远程仓库。
在 git_commit 之后调用，将本地 commit 推送到远程。

使用场景：
- 完成本地提交后推送到远程仓库
- 与团队成员共享代码""",
                parameters=[
                    ToolParameter("path", "string", "Git 仓库路径，默认当前目录", required=False),
                    ToolParameter("remote", "string", "远程仓库名称，默认 origin", required=False),
                    ToolParameter("branch", "string", "目标分支名，默认推送当前分支到同名远程分支", required=False),
                ],
                examples=[
                    'git_push()',
                    'git_push(branch="main")',
                    'git_push(remote="origin", branch="develop")',
                ],
                constraints=[
                    "推送前需要先有本地 commit（先 git_commit）",
                    "需要远程仓库的访问权限",
                    "不要强制推送到主分支（main/master）",
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
            logger.exception("Git 失败")
            return ToolResult.fail(call_id, tool_name, str(e))

    async def _handle_status(self, call_id: str, path: str = ".") -> ToolResult:
        import subprocess
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=path, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return ToolResult.fail(call_id, "git_status", f"git status 失败: {result.stderr.strip()}")
        return ToolResult.ok(call_id, "git_status", {
            "status": result.stdout.strip() or "工作区干净，无修改",
        })

    async def _handle_commit(self, call_id: str, message: str, path: str = ".",
                             add_all: bool = True) -> ToolResult:
        import subprocess
        if add_all:
            add = subprocess.run(
                ["git", "add", "."], cwd=path,
                capture_output=True, text=True, timeout=30
            )
            if add.returncode != 0:
                return ToolResult.fail(call_id, "git_commit", f"git add 失败: {add.stderr.strip()}")
        if not message:
            return ToolResult.fail(call_id, "git_commit", "commit 需要 message 参数")
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=path, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return ToolResult.fail(call_id, "git_commit", f"git commit 失败: {result.stderr.strip()}")
        return ToolResult.ok(call_id, "git_commit", {
            "message": message,
            "output": result.stdout.strip(),
        })

    async def _handle_push(self, call_id: str, path: str = ".", remote: str = "origin",
                           branch: str = "") -> ToolResult:
        import subprocess
        cmd = ["git", "push", remote]
        if branch:
            cmd.append(branch)
        result = subprocess.run(
            cmd, cwd=path,
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return ToolResult.fail(call_id, "git_push", f"git push 失败: {result.stderr.strip()}")
        return ToolResult.ok(call_id, "git_push", {
            "remote": remote,
            "branch": branch or "当前分支",
            "output": result.stdout.strip(),
        })
