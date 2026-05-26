"""
搜索工具 — 文件搜索、内容搜索
"""

import os
import fnmatch
import logging
from typing import List

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.search")


class GlobFindTool(BaseTool):

    def __init__(self, **kwargs):
        pass

    """使用通配符搜索文件"""

    @property
    def name(self) -> str:
        return "glob_find"

    @property
    def description(self) -> str:
        return "使用通配符模式搜索文件，如 **/*.py"

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="pattern", type="string", description="通配符模式，如 **/*.py", required=True),
            ToolParameter(name="root", type="string", description="搜索根目录（默认当前目录）", required=False, default="."),
            ToolParameter(name="max_results", type="number", description="最大返回数", required=False, default=50),
        ]

    async def execute(self, call_id, **kwargs) -> ToolResult:
        pattern = kwargs.get("pattern", "")
        root = kwargs.get("root", ".")
        max_results = kwargs.get("max_results", 50)
        try:
            matches = []
            for root_dir, dirs, files in os.walk(root):
                for f in files:
                    full = os.path.join(root_dir, f)
                    rel = os.path.relpath(full, root)
                    if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f, pattern):
                        matches.append(rel)
                        if len(matches) >= max_results:
                            break
                if len(matches) >= max_results:
                    break
            return ToolResult.success(call_id, self.name, {
                "pattern": pattern,
                "root": root,
                "matches": matches,
                "count": len(matches),
                "truncated": len(matches) >= max_results,
            })
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))


class GrepSearchTool(BaseTool):


    @property
    def name(self) -> str:
        return "grep_search"

    @property
    def description(self) -> str:
        return "在文件中搜索文本内容"

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="pattern", type="string", description="搜索文本（支持正则）", required=True),
            ToolParameter(name="root", type="string", description="搜索根目录", required=False, default="."),
            ToolParameter(name="include", type="string", description="文件过滤模式，如 *.py", required=False, default="*"),
            ToolParameter(name="max_results", type="number", description="最大结果数", required=False, default=30),
        ]

    async def execute(self, call_id, **kwargs) -> ToolResult:
        pattern = kwargs.get("pattern", "")
        root = kwargs.get("root", ".")
        include = kwargs.get("include", "*")
        max_results = kwargs.get("max_results", 30)
        try:
            import re as _re
            compiled = _re.compile(pattern)
            results = []
            for root_dir, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
                for f in files:
                    if not fnmatch.fnmatch(f, include):
                        continue
                    if f.startswith("."):
                        continue
                    fpath = os.path.join(root_dir, f)
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                            for i, line in enumerate(fh, 1):
                                if compiled.search(line):
                                    rel = os.path.relpath(fpath, root)
                                    results.append({
                                        "file": rel,
                                        "line": i,
                                        "content": line.rstrip()[:200],
                                    })
                                    if len(results) >= max_results:
                                        break
                        if len(results) >= max_results:
                            break
                    except (IOError, OSError):
                        continue
                if len(results) >= max_results:
                    break
            return ToolResult.success(call_id, self.name, {
                "pattern": pattern,
                "results": results,
                "count": len(results),
            })
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))


