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
            "git_pull": self._handle_pull,
            "git_fetch": self._handle_fetch,
            "git_diff": self._handle_diff,
            "git_log": self._handle_log,
            "git_branch_list": self._handle_branch_list,
            "git_stash": self._handle_stash,
            "git_stash_pop": self._handle_stash_pop,
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
            ToolDefinition(
                name="git_pull",
                description="""从远程仓库拉取并合并最新代码。

使用场景：
- 升级前拉取远程更新
- 同步团队成员的最新提交""",
                parameters=[
                    ToolParameter("path", "string", "Git 仓库路径，默认当前目录", required=False),
                    ToolParameter("remote", "string", "远程仓库名称，默认 origin", required=False),
                    ToolParameter("branch", "string", "远程分支名，默认拉取同名远程分支", required=False),
                ],
                examples=[
                    'git_pull()',
                    'git_pull(branch="main")',
                    'git_pull(remote="origin", branch="develop")',
                ],
                constraints=[
                    "拉取前建议先 git_stash 暂存本地未提交的修改",
                    "如有冲突会返回冲突信息，需要手动解决",
                ],
            ),
            ToolDefinition(
                name="git_fetch",
                description="""从远程仓库 fetch 最新信息（不合并，仅更新远程引用）。

使用场景：
- 在 pull 前先 fetch 查看远程有什么变化
- 配合 git_diff 查看远程与本地的差异""",
                parameters=[
                    ToolParameter("path", "string", "Git 仓库路径，默认当前目录", required=False),
                    ToolParameter("remote", "string", "远程仓库名称，默认 origin", required=False),
                ],
                is_read=True,
                examples=[
                    'git_fetch()',
                    'git_fetch(remote="origin")',
                ],
            ),
            ToolDefinition(
                name="git_diff",
                description="""比较两个 Git 引用（分支/commit/tag）之间的差异。

使用场景：
- 查看远程分支与本地的差异（git_fetch 后使用）
- 审查某个 commit 的变更内容
- 升级前查看远程有哪些文件变了""",
                parameters=[
                    ToolParameter("path", "string", "Git 仓库路径，默认当前目录", required=False),
                    ToolParameter("base", "string", "基准引用（如 HEAD, main, commit hash）", required=True),
                    ToolParameter("target", "string", "目标引用（如 origin/main, 某个 commit）", required=True),
                    ToolParameter("stat_only", "boolean", "只返回文件变更统计，不返回详细 diff，默认 false", required=False),
                ],
                is_read=True,
                examples=[
                    'git_diff(base="HEAD", target="origin/main", stat_only=True)',
                    'git_diff(base="HEAD~3", target="HEAD")',
                ],
                constraints=[
                    "stat_only=True 返回文件级别摘要（推荐用于概览）",
                    "stat_only=False 返回详细代码差异（内容较长）",
                    "base 和 target 必须是有效的 git 引用",
                ],
            ),
            ToolDefinition(
                name="git_log",
                description="""查看 Git commit 历史记录。

使用场景：
- 了解最近的提交历史
- 查找某个功能的提交时间
- 查看远程分支的更新情况""",
                parameters=[
                    ToolParameter("path", "string", "Git 仓库路径，默认当前目录", required=False),
                    ToolParameter("count", "integer", "显示最近几条，默认 10", required=False),
                    ToolParameter("oneline", "boolean", "单行摘要格式，默认 true", required=False),
                ],
                is_read=True,
                examples=[
                    'git_log(count=5)',
                    'git_log(count=20, oneline=False)',
                ],
            ),
            ToolDefinition(
                name="git_branch_list",
                description="""列出 Git 仓库的分支。

使用场景：
- 查看当前有哪些分支
- 确认远程分支是否存在""",
                parameters=[
                    ToolParameter("path", "string", "Git 仓库路径，默认当前目录", required=False),
                    ToolParameter("remote", "boolean", "是否也显示远程分支，默认 true", required=False),
                ],
                is_read=True,
                examples=[
                    'git_branch_list()',
                    'git_branch_list(remote=False)',
                ],
            ),
            ToolDefinition(
                name="git_stash",
                description="""暂存当前未提交的修改（git stash push）。

使用场景：
- 升级或拉取远程代码前暂存本地修改
- 临时切换到其他任务""",
                parameters=[
                    ToolParameter("path", "string", "Git 仓库路径，默认当前目录", required=False),
                    ToolParameter("message", "string", "暂存说明，可选", required=False),
                ],
                examples=[
                    'git_stash()',
                    'git_stash(message="升级前暂存")',
                ],
                constraints=[
                    "暂存后工作区会变为干净状态",
                    "使用 git_stash_pop 恢复暂存的修改",
                ],
            ),
            ToolDefinition(
                name="git_stash_pop",
                description="""恢复最近一次 git_stash 暂存的修改。

使用场景：
- 升级完成后恢复之前的本地修改
- 拉取远程代码后恢复暂存""",
                parameters=[
                    ToolParameter("path", "string", "Git 仓库路径，默认当前目录", required=False),
                ],
                examples=[
                    'git_stash_pop()',
                ],
                constraints=[
                    "如果没有 stash 记录会报错",
                    "恢复时可能有冲突，需要手动解决",
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

    async def _handle_pull(self, call_id: str, path: str = ".", remote: str = "origin",
                           branch: str = "") -> ToolResult:
        import subprocess
        cmd = ["git", "pull", remote]
        if branch:
            cmd.append(branch)
        result = subprocess.run(
            cmd, cwd=path,
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return ToolResult.fail(call_id, "git_pull", f"git pull 失败: {result.stderr.strip()}")
        return ToolResult.ok(call_id, "git_pull", {
            "remote": remote,
            "branch": branch or "当前分支",
            "output": result.stdout.strip(),
        })

    async def _handle_fetch(self, call_id: str, path: str = ".", remote: str = "origin") -> ToolResult:
        import subprocess
        result = subprocess.run(
            ["git", "fetch", remote], cwd=path,
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return ToolResult.fail(call_id, "git_fetch", f"git fetch 失败: {result.stderr.strip()}")
        return ToolResult.ok(call_id, "git_fetch", {
            "remote": remote,
            "output": result.stdout.strip() or result.stderr.strip() or "fetch 完成",
        })

    async def _handle_diff(self, call_id: str, base: str, target: str,
                           path: str = ".", stat_only: bool = False) -> ToolResult:
        import subprocess
        cmd = ["git", "diff"]
        if stat_only:
            cmd.append("--stat")
        cmd.append(f"{base}..{target}")
        result = subprocess.run(
            cmd, cwd=path,
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return ToolResult.fail(call_id, "git_diff", f"git diff 失败: {result.stderr.strip()}")
        output = result.stdout.strip()
        if not output:
            output = "无差异"
        return ToolResult.ok(call_id, "git_diff", {
            "base": base,
            "target": target,
            "stat_only": stat_only,
            "diff": output,
        })

    async def _handle_log(self, call_id: str, path: str = ".",
                          count: int = 10, oneline: bool = True) -> ToolResult:
        import subprocess
        cmd = ["git", "log", f"-{count}"]
        if oneline:
            cmd.append("--oneline")
        result = subprocess.run(
            cmd, cwd=path,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return ToolResult.fail(call_id, "git_log", f"git log 失败: {result.stderr.strip()}")
        return ToolResult.ok(call_id, "git_log", {
            "count": count,
            "log": result.stdout.strip() or "无历史记录",
        })

    async def _handle_branch_list(self, call_id: str, path: str = ".",
                                  remote: bool = True) -> ToolResult:
        import subprocess
        cmd = ["git", "branch"]
        if remote:
            cmd.append("-a")
        result = subprocess.run(
            cmd, cwd=path,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return ToolResult.fail(call_id, "git_branch_list", f"git branch 失败: {result.stderr.strip()}")
        return ToolResult.ok(call_id, "git_branch_list", {
            "branches": result.stdout.strip() or "无分支",
        })

    async def _handle_stash(self, call_id: str, path: str = ".",
                            message: str = "") -> ToolResult:
        import subprocess
        cmd = ["git", "stash", "push"]
        if message:
            cmd.extend(["-m", message])
        result = subprocess.run(
            cmd, cwd=path,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return ToolResult.fail(call_id, "git_stash", f"git stash 失败: {result.stderr.strip()}")
        return ToolResult.ok(call_id, "git_stash", {
            "message": message or "auto",
            "output": result.stdout.strip(),
        })

    async def _handle_stash_pop(self, call_id: str, path: str = ".") -> ToolResult:
        import subprocess
        result = subprocess.run(
            ["git", "stash", "pop"], cwd=path,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return ToolResult.fail(call_id, "git_stash_pop", f"git stash pop 失败: {result.stderr.strip()}")
        return ToolResult.ok(call_id, "git_stash_pop", {
            "output": result.stdout.strip(),
        })
