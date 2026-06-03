"""
code_tool.py — 统一代码操作工具

单一 CodeTool 类，9 个 action：
 - search  : 搜索代码关键字
 - read    : 读取文件（支持分页、智能摘要）
 - diff    : 预览差异（不改文件）
 - edit    : 编辑代码（精确替换，自动备份）
 - rollback: 回滚编辑
 - append  : 追加内容
 - quality : 代码质量评分
 - style   : 检测项目代码规范
 - grep    : 正则搜索代码

安全原则：read → diff → edit → rollback（需要时）
"""

import os
import re
import ast
import difflib
import shutil
import fnmatch
import logging
from typing import Any, Dict, List, Optional

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger(__name__)


class CodeTool(BaseTool):
    """统一代码操作工具"""

    _edit_history: List[Dict] = []

    @property
    def is_read(self) -> bool:
        """code 负责修改代码，不判断为只读"""
        return False

    @property
    def is_write(self) -> bool:
        """code 负责 edit/append/rollback 等写入操作"""
        return True

    @property
    def name(self) -> str:
        return "code"

    @property
    def description(self) -> str:
        return (
            "代码操作工具。action 可选:\n"
            "search : 搜索关键字，返回匹配行和上下文\n"
            "read   : 读取文件，支持分页和智能摘要（大文件自动截断）\n"
            "edit   : 编辑代码（精确替换，自动备份 .bak）\n"
            "diff   : 预览修改差异（不写入文件）\n"
            "rollback: 回滚编辑（无参数回滚最后一步）\n"
            "append : 追加内容到文件末尾\n"
            "quality: 代码质量评分（类型注解/异常处理/docstring/安全）\n"
            "style  : 检测项目代码规范（pyproject.toml/ruff配置）\n"
            "grep   : 正则搜索代码（支持通配符）\n"
            "安全流程: read → diff → edit"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("action", "string",
                          "search/read/edit/diff/rollback/append/quality/style/grep",
                          required=True,
                          enum=["search", "read", "edit", "diff", "rollback",
                                "append", "quality", "style", "grep"]),
            ToolParameter("path", "string", "文件或目录路径", required=False),
            ToolParameter("keyword", "string", "搜索关键词(search用)", required=False),
            ToolParameter("pattern", "string", "文件匹配模式(search用)，默认*.py", required=False),
            ToolParameter("context_lines", "number", "上下文行数(search用)，默认2", required=False),
            ToolParameter("start_line", "number", "起始行号(read用)", required=False),
            ToolParameter("end_line", "number", "结束行号(read用)", required=False),
            ToolParameter("limit", "number", "读取行数(read用)，默认200", required=False),
            ToolParameter("old_text", "string", "旧文本(edit/diff用)", required=False),
            ToolParameter("new_text", "string", "新文本(edit/diff用)", required=False),
            ToolParameter("content", "string", "追加内容(append用)", required=False),
            ToolParameter("threshold", "number", "质量阈值(quality用)，默认70", required=False),
            ToolParameter("glob", "string", "文件匹配模式(grep用)，默认**/*.py", required=False),
            ToolParameter("base_dir", "string", "搜索根目录，默认.", required=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        action = kwargs.get("action", "read")
        handlers = {
            "search": self._search, "read": self._read,
            "edit": self._edit, "diff": self._diff,
            "rollback": self._rollback, "append": self._append,
            "quality": self._quality, "style": self._style, "grep": self._grep,
        }
        handler = handlers.get(action)
        if not handler:
            return ToolResult.error(call_id, self.name, f"未知操作: {action}")
        return await handler(call_id, kwargs)

    # ── search ──

    async def _search(self, call_id, args):
        keyword = args.get("keyword", "")
        pattern = args.get("pattern", "*.py")
        context_lines = int(args.get("context_lines", 2))
        base_dir = args.get("base_dir", ".")
        if not keyword:
            return ToolResult.error(call_id, self.name, "search 需要 keyword")

        exclude = {'.venv', 'venv', '__pycache__', '.git', 'node_modules',
                   '.idea', '.vscode', 'dist', 'build'}
        results = []
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in exclude and not d.startswith(".")]
            for fname in files:
                if not fnmatch.fnmatch(fname, pattern):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                    for i, line in enumerate(lines):
                        if keyword in line:
                            ctx_start = max(0, i - context_lines)
                            ctx_end = min(len(lines), i + context_lines + 1)
                            results.append({
                                "file": os.path.relpath(fpath, base_dir),
                                "line": i + 1,
                                "match": line.strip()[:200],
                                "context": "".join(
                                    f"{'>' if j==i else ' '} {j+1}: {lines[j].rstrip()}\n"
                                    for j in range(ctx_start, ctx_end)
                                ),
                            })
                            if len(results) >= 30:
                                break
                except Exception:
                    pass
                if len(results) >= 30:
                    break
            if len(results) >= 30:
                break

        if not results:
            return ToolResult.success(call_id, self.name, {
                "keyword": keyword, "matches": 0,
                "_hint": f"未找到 '{keyword}'。换关键词或扩大 pattern",
            })
        return ToolResult.success(call_id, self.name, {
            "keyword": keyword, "matches": len(results), "results": results,
            "_hint": f"找到 {len(results)} 处匹配。用 action='read' 查看上下文",
        })

    # ── read ──

    async def _read(self, call_id, args):
        path = args.get("path", "")
        start_line = int(args.get("start_line", 1))
        end_line = args.get("end_line")
        limit = int(args.get("limit", 200))
        if not path:
            return ToolResult.error(call_id, self.name, "read 需要 path")

        full_path = os.path.abspath(path)
        if not os.path.exists(full_path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        total = len(lines)
        if end_line is None:
            end_line = min(total, start_line + limit - 1)
        else:
            end_line = int(end_line)
        start_idx, end_idx = max(0, start_line - 1), min(total, end_line)
        content = "".join(lines[start_idx:end_idx])

        is_large = total > 300
        structure = []
        if is_large:
            for i, line in enumerate(lines[:300]):
                s = line.strip()
                if any(s.startswith(kw) for kw in ["class ", "def ", "async def ", "import ", "from ", "@"]):
                    if len(s) < 120:
                        structure.append(f"L{i+1}: {s}")

        result = {"path": path, "total_lines": total,
                   "start_line": start_line, "end_line": end_idx, "content": content}
        if is_large:
            result["structure"] = structure[:30]
            result["_hint"] = f"文件共 {total} 行，第 {start_line}-{end_idx} 行。用 start_line/end_line 翻页"
        else:
            result["_hint"] = "如需修改，先用 action='diff' 预览差异"
        return ToolResult.success(call_id, self.name, result)

    # ── diff ──

    async def _diff(self, call_id, args):
        path, old_text, new_text = args.get("path", ""), args.get("old_text", ""), args.get("new_text", "")
        if not path or not old_text:
            return ToolResult.error(call_id, self.name, "diff 需要 path 和 old_text")
        full_path = os.path.abspath(path)
        if not os.path.exists(full_path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
        count = content.count(old_text)
        if count == 0:
            return ToolResult.error(call_id, self.name, "未找到匹配文本。用 action='read' 确认原文")
        if count > 1:
            return ToolResult.error(call_id, self.name,
                                    f"匹配到 {count} 处。请扩大 old_text 使其唯一")
        diff = difflib.unified_diff(
            content.splitlines(keepends=True),
            content.replace(old_text, new_text, 1).splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
        )
        return ToolResult.success(call_id, self.name, {
            "path": path, "diff": "".join(diff),
            "_hint": "确认 diff 无误后，用 action='edit' 提交修改",
        })

    # ── edit ──

    async def _edit(self, call_id, args):
        path, old_text, new_text = args.get("path", ""), args.get("old_text", ""), args.get("new_text", "")
        if not path or not old_text:
            return ToolResult.error(call_id, self.name, "edit 需要 path 和 old_text")
        full_path = os.path.abspath(path)
        if not os.path.exists(full_path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
        count = content.count(old_text)
        if count == 0:
            return ToolResult.error(call_id, self.name, "未找到匹配文本。用 action='diff' 预览确认")
        if count > 1:
            return ToolResult.error(call_id, self.name,
                                    f"匹配到 {count} 处。请扩大 old_text 使其唯一")
        bak_path = full_path + ".bak"
        shutil.copy2(full_path, bak_path)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content.replace(old_text, new_text, 1))

        edit_id = len(self._edit_history) + 1
        self._edit_history.append({"id": edit_id, "path": path, "backup": bak_path,
                                    "old_text": old_text, "new_text": new_text})
        return ToolResult.success(call_id, self.name, {
            "path": path, "edit_id": edit_id, "backup": bak_path, "status": "已修改",
            "_hint": f"编辑 #{edit_id} 已应用。回滚用 action='rollback'",
        })

    # ── rollback ──

    async def _rollback(self, call_id, args):
        path = args.get("path", "")
        if not path:
            return ToolResult.error(call_id, self.name, "rollback 需要 path")
        path_history = [e for e in self._edit_history if e["path"] == path]
        if not path_history:
            return ToolResult.error(call_id, self.name, f"没有 {path} 的编辑记录")

        entry = path_history[-1]
        full_path = os.path.abspath(entry["path"])
        if os.path.exists(entry["backup"]):
            shutil.copy2(entry["backup"], full_path)
        else:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content.replace(entry["new_text"], entry["old_text"], 1))
        self._edit_history.remove(entry)
        return ToolResult.success(call_id, self.name,
                                   {"path": entry["path"], "status": "已回滚"})

    # ── append ──

    async def _append(self, call_id, args):
        path = args.get("path", "")
        content = args.get("content", args.get("new_text", ""))
        if not path or not content:
            return ToolResult.error(call_id, self.name, "append 需要 path 和 content")
        full_path = os.path.abspath(path)
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        with open(full_path, "a", encoding="utf-8") as f:
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")
        return ToolResult.success(call_id, self.name, {"path": path, "appended": True})

    # ── quality ──

    async def _quality(self, call_id, args):
        path = args.get("path", "")
        threshold = int(args.get("threshold", 70))
        if not path:
            return ToolResult.error(call_id, self.name, "quality 需要 path")
        full_path = os.path.abspath(path)
        if not os.path.exists(full_path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                code = f.read()
        except Exception as e:
            return ToolResult.error(call_id, self.name, f"读取失败: {e}")

        lines = code.split("\n")
        score, issues = 100, []

        # 类型注解
        func_defs = [l for l in lines if l.strip().startswith("def ") or l.strip().startswith("async def ")]
        if func_defs:
            annotated = sum(1 for l in func_defs if " -> " in l)
            rate = annotated / len(func_defs)
            if rate < 0.5:
                score -= 20
                issues.append({"severity": "major", "type": "typing",
                               "desc": f"类型注解覆盖率仅 {rate:.0%}",
                               "fix": f"为 {len(func_defs) - annotated} 个函数添加返回类型注解"})
            elif rate < 0.8:
                score -= 10
                issues.append({"severity": "minor", "type": "typing",
                               "desc": f"部分函数缺少返回类型注解",
                               "fix": f"补充类型注解"})

        # I/O 异常
        io_ps = ["open(", "requests.", "httpx.", ".read()", ".write("]
        if any(p in code for p in io_ps) and "try:" not in code:
            score -= 15
            issues.append({"severity": "major", "type": "error_handling",
                           "desc": "存在I/O操作但无异常处理",
                           "fix": "用 try-except 包裹 I/O 操作"})

        # docstring
        if any(l.strip().startswith("def ") for l in lines) and '"""' not in code:
            score -= 10
            issues.append({"severity": "minor", "type": "documentation",
                           "desc": "函数缺少 docstring"})

        # 安全
        danger = {"eval(": ("critical", "用 ast.literal_eval"),
                  "exec(": ("critical", "避免使用 exec"),
                  "shell=True": ("major", "使用 subprocess.run 参数列表"),
                  "password": ("warning", "从环境变量读取密码")}
        for p, (sev, fix) in danger.items():
            if p.lower() in code.lower():
                score -= 20 if sev == "critical" else 15 if sev == "major" else 10
                issues.append({"severity": sev, "type": "security",
                               "desc": f"发现 {p}", "fix": fix})

        # 函数长度
        cur, cnt = None, 0
        for l in lines:
            s = l.strip()
            if s.startswith("def ") or s.startswith("async def "):
                if cur and cnt > 50:
                    issues.append({"severity": "minor", "type": "maintainability",
                                   "desc": f"函数过长: {cur} ({cnt}行)", "fix": "拆分"})
                    score -= 5
                cur, cnt = s[:60], 0
            elif cur is not None:
                cnt += 1
        if cur and cnt > 50:
            score -= 5
            issues.append({"severity": "minor", "type": "maintainability",
                           "desc": f"函数过长: {cur} ({cnt}行)", "fix": "拆分"})

        score = max(0, score)
        passed = score >= threshold
        sev_order = {"critical": 0, "major": 1, "minor": 2, "warning": 3}
        return ToolResult.success(call_id, self.name, {
            "path": path, "score": score, "passed": passed, "threshold": threshold,
            "issues": sorted(issues, key=lambda x: sev_order.get(x["severity"], 4)),
            "summary": f"评分 {score}/100，{'通过' if passed else f'低于阈值 {threshold}'}，{len(issues)} 个问题",
        })

    # ── style ──

    async def _style(self, call_id, args):
        path = args.get("path", ".")
        full_path = os.path.abspath(path)
        if not os.path.isdir(full_path):
            return ToolResult.error(call_id, self.name, f"目录不存在: {path}")

        rules = []
        config = self._load_toml(os.path.join(full_path, "pyproject.toml"))
        if config:
            rules.append({"source": "pyproject.toml"})
            ruff = config.get("tool", {}).get("ruff", {})
            if ruff:
                rules.append({"key": "line_length", "value": ruff.get("line-length", 88)})
                rules.append({"key": "linter", "value": "ruff"})

        if not rules:
            return ToolResult.success(call_id, self.name, {
                "path": path, "has_config": False,
                "_hint": "未检测到项目代码规范配置，使用默认规范",
            })
        return ToolResult.success(call_id, self.name, {"path": path, "has_config": True, "rules": rules})

    @staticmethod
    def _load_toml(filepath):
        if not os.path.isfile(filepath):
            return None
        try:
            import tomllib
            with open(filepath, "rb") as f:
                return tomllib.load(f)
        except ImportError:
            try:
                import tomli
                with open(filepath, "rb") as f:
                    return tomli.load(f)
            except ImportError:
                return None

    # ── grep ──

    async def _grep(self, call_id, args):
        pattern_str = args.get("pattern", "")
        glob_pattern = args.get("glob", "**/*.py")
        base_dir = args.get("base_dir", ".")
        if not pattern_str:
            return ToolResult.error(call_id, self.name, "grep 需要 pattern")
        try:
            regex = re.compile(pattern_str)
        except re.error as e:
            return ToolResult.error(call_id, self.name, f"正则无效: {e}")

        exclude = {'.venv', 'venv', '__pycache__', '.git', 'node_modules',
                   '.idea', '.vscode', 'dist', 'build'}
        file_glob = glob_pattern.split("/")[-1] if "/" in glob_pattern else glob_pattern
        recursive = "**" in glob_pattern

        results, scanned = [], 0
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in exclude and not d.startswith(".")]
            if not recursive and os.path.relpath(root, base_dir).count(os.sep) > 0:
                dirs.clear()
                continue
            for fname in files:
                if not fnmatch.fnmatch(fname, file_glob):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                    scanned += 1
                    for i, line in enumerate(lines):
                        m = regex.search(line)
                        if m:
                            ctx_start = max(0, i - 2)
                            ctx_end = min(len(lines), i + 3)
                            results.append({
                                "file": os.path.relpath(fpath, base_dir),
                                "line": i + 1,
                                "match": line.strip()[:200],
                                "context": "".join(
                                    f"{'>' if j==i else ' '} {j+1}: {lines[j].rstrip()}\n"
                                    for j in range(ctx_start, ctx_end)
                                ),
                            })
                            if len(results) >= 30:
                                break
                except Exception:
                    pass
                if len(results) >= 30:
                    break
            if len(results) >= 30:
                break

        return ToolResult.success(call_id, self.name, {
            "pattern": pattern_str, "files_scanned": scanned,
            "matches": len(results), "results": results[:30],
        })
