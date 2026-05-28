"""
tool/code_tool.py — 代码操作工具

从 engine/dag/node.py 迁移：
  - CodeSearchNode → CodeSearchTool
  - CodeEditorNode → ReadCodeTool + WriteCodeTool + RollbackCodeTool
"""

import difflib
import fnmatch
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult


class CodeSearchTool(BaseTool):
    """在项目中搜索关键字"""

    @property
    def name(self) -> str:
        return "code_search"

    @property
    def description(self) -> str:
        return "在项目中搜索关键字，返回匹配的文件路径、行号、代码片段"

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("keyword", "string", "要搜索的关键字", required=True),
            ToolParameter("file_pattern", "string", "文件匹配模式，如 *.py", required=False, default="*.py"),
            ToolParameter("base_dir", "string", "搜索根目录", required=False, default="."),
            ToolParameter("max_results", "number", "最大结果数", required=False, default=100),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        keyword = kwargs.get("keyword", "")
        file_pattern = kwargs.get("file_pattern", "*.py")
        base_dir = kwargs.get("base_dir", ".")
        max_results = kwargs.get("max_results", 100)

        if not keyword:
            return ToolResult.error(call_id, self.name, "缺少搜索关键字")

        exclude_dirs = {'.venv', 'venv', '__pycache__', '.git', 'node_modules', '.idea', '.vscode'}
        results = []

        for root, dirs, fnames in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for fname in fnames:
                if not fnmatch.fnmatch(fname, file_pattern):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if keyword in line:
                                rel = os.path.relpath(fpath, base_dir)
                                results.append({
                                    "file": rel,
                                    "line": i,
                                    "content": line.strip()[:200],
                                })
                                if len(results) >= max_results:
                                    return ToolResult.success(call_id, self.name, {
                                        "results": results,
                                        "count": len(results),
                                        "truncated": True,
                                    })
                except Exception:
                    pass

        return ToolResult.success(call_id, self.name, {
            "results": results,
            "count": len(results),
        })


class ReadCodeTool(BaseTool):
    """读取代码文件内容（支持分页，智能摘要模式大文件自动截断）"""

    @property
    def name(self) -> str:
        return "read_code"

    @property
    def description(self) -> str:
        return (
            "读取源码文件，返回结构化摘要（行数、类/函数结构、预览）。"
            "大文件（>200行）自动返回摘要+结构索引，"
            "通过 offset 参数分页读取后续内容。"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("path", "string", "文件路径", required=True),
            ToolParameter("offset", "number", "起始行号（从1开始），不传则返回摘要+前200行", required=False),
            ToolParameter("limit", "number", "读取行数", required=False, default=200),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        path = kwargs.get("path", "")
        offset = kwargs.get("offset")
        limit = int(kwargs.get("limit", 200))

        # ⭐ 参数验证
        if not path or not path.strip():
            return ToolResult.error(
                call_id, self.name,
                "path 参数不能为空。请提供要读取的代码文件完整路径。"
            )
        if not os.path.exists(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        total = len(lines)
        total_chars = sum(len(l) for l in lines)

        # ── 分页模式：指定了 offset ──
        if offset is not None:
            start = max(0, int(offset) - 1)
            end = min(total, start + limit)
            content = "".join(lines[start:end])
            has_more = end < total
            return ToolResult.success(call_id, self.name, {
                "path": path,
                "total_lines": total,
                "start_line": int(offset),
                "end_line": end,
                "has_more": has_more,
                "next_offset": end + 1 if has_more else None,
                "content": content,
            })

        # ── 首次读取：智能摘要 ──
        preview_lines = min(limit, total)
        preview = "".join(lines[:preview_lines])
        ext = os.path.splitext(path)[1].lower()

        # 提取结构
        structure = []
        for i, line in enumerate(lines[:500], 1):
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in
                   ["class ", "def ", "async def ", "@", "import ", "from "]):
                if len(stripped) < 120:
                    structure.append(f"L{i}: {stripped}")

        is_large = total > 500 or total_chars > 10000

        result = {
            "path": path,
            "total_lines": total,
            "total_chars": total_chars,
            "extension": ext,
            "preview": preview,
            "preview_lines": preview_lines,
        }

        if is_large:
            result.update({
                "is_large": True,
                "has_more": True,
                "next_offset": preview_lines + 1,
                "structure": structure[:30],
                "how_to_read_more": (
                    f"文件较大（{total_chars} 字符，{total} 行）。"
                    f"调用 read_code path={path} offset={preview_lines + 1} limit=200 读取后续"
                ),
            })

        return ToolResult.success(call_id, self.name, result)


class EditCodeTool(BaseTool):
    """编辑代码文件（精确替换，带备份和回滚能力）"""

    # 类级别编辑历史
    _edit_history: Dict[str, List[Dict]] = {}

    @property
    def name(self) -> str:
        return "edit_code"

    @property
    def description(self) -> str:
        return "对代码文件做精确替换编辑。必须提供 old_text 精确定位要替换的原文。自动备份，支持回滚。"

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("path", "string", "文件路径", required=True),
            ToolParameter("old_text", "string", "要替换的原文（必须是唯一定位）", required=True),
            ToolParameter("new_text", "string", "替换后的新文本", required=True),
            ToolParameter("create_backup", "boolean", "是否创建备份", required=False, default=True),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        path = kwargs["path"]
        old_text = kwargs["old_text"]
        new_text = kwargs.get("new_text", "")
        create_backup = kwargs.get("create_backup", True)

        if not os.path.exists(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            return ToolResult.error(call_id, self.name, "未找到匹配文本")
        if count > 1:
            return ToolResult.error(call_id, self.name, f"匹配到 {count} 处，请提供更精确的定位")

        bak = None
        if create_backup:
            bak = path + ".bak"
            shutil.copy2(path, bak)

        new_content = content.replace(old_text, new_text, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        edit_id = hash(path + old_text) & 0xFFFFFFFF
        if path not in self._edit_history:
            self._edit_history[path] = []
        self._edit_history[path].append({
            "id": edit_id,
            "path": path,
            "backup": bak,
            "old_text": old_text,
            "new_text": new_text,
            "time": time.time(),
        })

        return ToolResult.success(call_id, self.name, {
            "path": path,
            "edit_id": edit_id,
            "backup": bak,
        })


class RollbackCodeTool(BaseTool):
    """回滚代码编辑"""

    @property
    def name(self) -> str:
        return "rollback_code"

    @property
    def description(self) -> str:
        return "回滚之前的代码编辑。不传 edit_id 则回滚最后一次编辑。"

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("path", "string", "文件路径", required=True),
            ToolParameter("edit_id", "number", "要回滚的编辑 ID（不传则回滚最后一次）", required=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        path = kwargs.get("path", "")
        edit_id = kwargs.get("edit_id")

        history = EditCodeTool._edit_history.get(path, [])
        if not history:
            return ToolResult.error(call_id, self.name, f"没有可回滚的编辑记录: {path}")

        if edit_id:
            entries = [e for e in history if e["id"] == edit_id]
        else:
            entries = [history[-1]]

        if not entries:
            return ToolResult.error(call_id, self.name, "未找到指定的编辑记录")

        entry = entries[0]
        bak = entry.get("backup")

        if bak and os.path.exists(bak):
            shutil.copy2(bak, path)
            return ToolResult.success(call_id, self.name, {
                "path": path, "rolled_back": True, "method": "backup"
            })

        # 没有备份则反向替换
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        new_content = content.replace(entry["new_text"], entry["old_text"], 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return ToolResult.success(call_id, self.name, {
            "path": path, "rolled_back": True, "method": "reverse"
        })
