"""
engine/lint/runner.py — 自动代码检查器（AgentLoop 钩子，非 Tool）

不是 LLM 手动调用的工具，而是 AgentLoop 编辑文件后自动触发的钩子。

设计原则：
    1. 编辑文件后自动检查，LLM 不需要知道 LintRunner 的存在
    2. 错误结果直接格式化注入到下一轮 LLM 上下文
    3. 支持 ruff（推荐）→ pylint（fallback）
"""

import os
import json
import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── 支持的 linter 列表 ──

LINTER_CONFIGS = [
    {
        "name": "ruff",
        "check_cmd": ["ruff", "check", "--no-cache", "--output-format=json"],
        "fix_cmd": ["ruff", "check", "--no-cache", "--fix"],
        "version_cmd": ["ruff", "--version"],
        "install_hint": "pip install ruff",
    },
    {
        "name": "pylint",
        "check_cmd": ["pylint", "--output-format=json"],
        "fix_cmd": None,
        "version_cmd": ["pylint", "--version"],
        "install_hint": "pip install pylint",
    },
]


class LintRunner:
    """
    自动代码检查器 — AgentLoop 的钩子，不是 Tool。

    LLM 不需要知道它的存在：
    编辑文件后 AgentLoop 自动调用，错误自动注入到上下文。
    """

    def __init__(self, project_root: str = "."):
        self._project_root = project_root
        self._available_linters: List[str] = []
        self._detect_linters()

    # ── 公共 API ──

    async def run(
        self,
        file_path: str,
        auto_fix: bool = False,
        linter: str = "auto",
    ) -> Dict[str, Any]:
        """
        检查单个文件。

        Args:
            file_path: 文件路径（相对或绝对）
            auto_fix: 是否自动修复可修复的问题（ruff 支持）
            linter: 指定 linter 名称，或 "auto" 自动选择

        Returns:
            {"passed": bool, "error_count": int, "warning_count": int,
             "issues": list, "raw": str}
        """
        full_path = self._resolve_path(file_path)

        if not full_path.exists():
            return {
                "passed": False,
                "error_count": 1,
                "warning_count": 0,
                "issues": [{"message": f"文件不存在: {file_path}"}],
                "raw": f"❌ 文件不存在: {file_path}",
            }

        if not str(full_path).endswith(".py"):
            return {
                "passed": True,
                "error_count": 0,
                "warning_count": 0,
                "issues": [],
                "raw": "",
            }

        if not self._available_linters:
            return {
                "passed": False,
                "error_count": 1,
                "warning_count": 0,
                "issues": [{"message": "未检测到 linter"}],
                "raw": (
                    "❌ 未检测到任何 linter。请先安装:\n"
                    "  pip install ruff    # 推荐\n"
                    "  # 或\n"
                    "  pip install pylint"
                ),
            }

        chosen = self._select_linter(linter)

        result = await self._run_linter(chosen, full_path, auto_fix)

        # 打印 lint 结果摘要
        if result["passed"]:
            logger.info(f"✅ Lint 通过: {file_path}")
        else:
            logger.warning(
                f"⚠️ Lint 发现问题: {file_path} | "
                f"错误: {result['error_count']} | 警告: {result['warning_count']} | "
                f"Linter: {chosen}"
            )

        return result

    def format_feedback(self, result: Dict[str, Any], file_path: str) -> str:
        """
        将 lint 结果格式化为注入 LLM 上下文的反馈文本。

        Returns:
            有错误时返回 Markdown 格式的反馈；通过时返回空字符串。
        """
        if result["passed"]:
            return ""

        rel_path = self._relative_path(Path(file_path))
        error_count = result["error_count"]
        warning_count = result["warning_count"]
        issues = result.get("issues", [])

        lines = [
            f"[自动代码检查] **{rel_path}**",
            f"发现 **{error_count} 个错误**、**{warning_count} 个警告**:",
            "",
        ]

        sorted_issues = sorted(
            issues,
            key=lambda i: (
                self._get_line(i),
                self._get_col(i),
            ),
        )

        for issue in sorted_issues[:15]:
            line = self._get_line(issue)
            col = self._get_col(issue)
            code = issue.get("code", "")
            msg = issue.get("message", "")
            lines.append(f"  • L{line}:{col} [{code}] {msg}")

        if len(sorted_issues) > 15:
            lines.append(f"  ... 还有 {len(sorted_issues) - 15} 个问题")

        lines.append("")
        lines.append("请修复以上问题后重新检查。")

        return "\n".join(lines)

    # ── Linter 执行 ──

    async def _run_linter(
        self, linter_name: str, file_path: Path, auto_fix: bool,
    ) -> Dict[str, Any]:
        if linter_name == "ruff":
            return await self._run_ruff(file_path, auto_fix)
        elif linter_name == "pylint":
            return await self._run_pylint(file_path)
        return {"passed": True, "error_count": 0, "warning_count": 0, "raw": "", "issues": []}

    async def _run_ruff(self, file_path: Path, auto_fix: bool) -> Dict[str, Any]:
        try:
            cmd = ["ruff", "check", "--no-cache", "--output-format=json"]
            if auto_fix:
                cmd.append("--fix")

            proc = await asyncio.create_subprocess_exec(
                *cmd, str(file_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            output = stdout.decode("utf-8", errors="replace").strip()
            err_output = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                return {"passed": True, "error_count": 0, "warning_count": 0, "raw": output or err_output, "issues": []}

            issues = json.loads(output) if (output and output.startswith("[")) else []

            error_count = sum(
                1 for i in issues
                if i.get("severity") == "error" or i.get("code", "").startswith("E")
            )
            warning_count = sum(
                1 for i in issues
                if i.get("severity") in ("warning", "note")
                or i.get("code", "").startswith(("W", "C", "N"))
            )

            return {
                "passed": len(issues) == 0,
                "error_count": error_count or len(issues),
                "warning_count": warning_count,
                "raw": output or err_output,
                "issues": issues,
            }

        except asyncio.TimeoutError:
            return {"passed": False, "error_count": 1, "warning_count": 0, "raw": "❌ ruff 超时", "issues": []}
        except (FileNotFoundError, PermissionError, OSError) as e:
            return {"passed": False, "error_count": 1, "warning_count": 0, "raw": f"❌ ruff: {e}", "issues": []}

    async def _run_pylint(self, file_path: Path) -> Dict[str, Any]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pylint", "--output-format=json", str(file_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            output = stdout.decode("utf-8", errors="replace").strip()
            err_output = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                return {"passed": True, "error_count": 0, "warning_count": 0, "raw": output or err_output, "issues": []}

            issues = json.loads(output) if (output and output.startswith("[")) else []

            error_count = sum(1 for i in issues if i.get("type") in ("error", "fatal"))
            warning_count = sum(1 for i in issues if i.get("type") in ("warning", "convention", "refactor", "info"))

            return {
                "passed": len(issues) == 0,
                "error_count": error_count or len(issues),
                "warning_count": warning_count,
                "raw": output or err_output,
                "issues": issues,
            }
        except Exception as e:
            return {"passed": False, "error_count": 1, "warning_count": 0, "raw": f"❌ pylint: {e}", "issues": []}

    # ── 辅助 ──

    def _detect_linters(self):
        """同步方式检测可用 linter"""
        import subprocess as _sp
        for cfg in LINTER_CONFIGS:
            try:
                result = _sp.run(
                    cfg["version_cmd"][:1] + ["--version"],
                    capture_output=True, timeout=5,
                )
                if result.returncode == 0:
                    self._available_linters.append(cfg["name"])
                    logger.info(f"✅ LintRunner: 检测到 {cfg['name']}")
            except Exception:
                continue

    def _select_linter(self, preferred: str) -> str:
        if preferred != "auto" and preferred in self._available_linters:
            return preferred
        if "ruff" in self._available_linters:
            return "ruff"
        if self._available_linters:
            return self._available_linters[0]
        return "ruff"

    def _resolve_path(self, file_path: str) -> Path:
        p = Path(file_path)
        if p.is_absolute():
            return p
        return Path(self._project_root) / p

    def _relative_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(Path(self._project_root)))
        except ValueError:
            return str(path)

    @staticmethod
    def _get_line(issue: Dict) -> int:
        loc = issue.get("location", {})
        if isinstance(loc, dict):
            return loc.get("row", issue.get("line", 0))
        return issue.get("line", 0)

    @staticmethod
    def _get_col(issue: Dict) -> int:
        loc = issue.get("location", {})
        if isinstance(loc, dict):
            return loc.get("column", 0)
        return issue.get("column", 0)


# ── 快捷辅助函数 ──

def format_lint_feedback(result: Dict[str, Any], file_path: str) -> str:
    """
    将 lint 结果格式化为注入到 LLM 上下文的反馈文本。

    供 AgentLoop 的自动 lint 环节使用：
    LLM 编辑完文件后，框架自动调 lint，结果直接塞进下一轮 LLM 上下文。
    """
    if result.get("passed"):
        return ""

    error_count = result.get("error_count", 0)
    warning_count = result.get("warning_count", 0)
    issues = result.get("issues", [])

    lines = [
        f"[LSP Feedback — {file_path}]",
        f"发现 {error_count} 个错误, {warning_count} 个警告:",
    ]
    for issue in issues[:15]:
        loc = issue.get("location", {})
        if isinstance(loc, dict):
            line = loc.get("row", issue.get("line", "?"))
        else:
            line = issue.get("line", "?")
        code = issue.get("code", "")
        msg = issue.get("message", "")
        lines.append(f"  L{line} [{code}] {msg}")

    return "\n".join(lines)
