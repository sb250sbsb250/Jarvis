"""
代码审查/分析工具
"""

import os
import ast
import logging
from typing import List

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.code")


class ReadCodeTool(BaseTool):

    def __init__(self, **kwargs):
        pass

    """阅读代码文件并理解"""

    @property
    def name(self) -> str:
        return "read_code"

    @property
    def description(self) -> str:
        return "读取代码文件并进行结构分析（函数、类、导入）"

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="path", type="string", description="代码文件路径", required=True),
        ]

    async def execute(self, call_id, **kwargs) -> ToolResult:
        path = kwargs.get("path", "")
        try:
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source)
            info = {
                "path": path,
                "size": len(source),
                "lines": source.count("\n") + 1,
                "imports": [],
                "classes": [],
                "functions": [],
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        info["imports"].append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    info["imports"].append(f"{node.module or ''}.{node.names[0].name if node.names else ''}")
                elif isinstance(node, ast.ClassDef):
                    methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                    info["classes"].append({"name": node.name, "methods": methods, "line": node.lineno})
                elif isinstance(node, ast.FunctionDef):
                    info["functions"].append({"name": node.name, "line": node.lineno, "args": [a.arg for a in node.args.args]})
            return ToolResult.success(call_id, self.name, info)
        except SyntaxError as e:
            return ToolResult.success(call_id, self.name, {
                "path": path, "size": len(source), "error": f"语法错误: {e}", "content": source[:2000],
            })
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))


class CodeReviewTool(BaseTool):


    @property
    def name(self) -> str:
        return "code_review"

    @property
    def description(self) -> str:
        return "审查代码文件，找出潜在问题"

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="path", type="string", description="文件路径", required=True),
            ToolParameter(name="check_imports", type="boolean", description="检查导入", required=False, default=True),
        ]

    async def execute(self, call_id, **kwargs) -> ToolResult:
        path = kwargs.get("path", "")
        try:
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
            issues = []
            lines = source.split("\n")
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if len(line) > 200:
                    issues.append({"line": i, "severity": "warning", "message": "行过长 (>200字符)"})
                if stripped.endswith("print(") and "import __future__" not in source:
                    issues.append({"line": i, "severity": "info", "message": "使用了 print（可能只是调试）"})
            return ToolResult.success(call_id, self.name, {
                "file": path, "lines": len(lines), "issues": issues, "issue_count": len(issues),
            })
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))


