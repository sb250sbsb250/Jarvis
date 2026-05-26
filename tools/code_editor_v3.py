"""
tools/code_editor_v3.py — 代码编辑工具（合并版）

从 8 个独立工具合并为 2 个：
  1. project_search  — 搜索项目代码 + 读取文件
  2. code_editor     — 编辑代码（read/diff/write/rollback/append）

保留原有的安全流程（备份、diff 预览、回滚），统一入口降低 LLM 认知负担。
"""

from __future__ import annotations

import os
import re
import difflib
import shutil
import logging
from typing import Any, Dict, List, Optional

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.code_v3")


# ═══════════════════════════════════════════
#  Tool 1: ProjectSearchTool
# ═══════════════════════════════════════════

class ProjectSearchTool(BaseTool):
    """项目代码搜索与文件读取"""

    def __init__(self, base_dir: str = ".", **kwargs):
        self.base_dir = base_dir

    @property
    def name(self) -> str:
        return "project_search"

    @property
    def description(self) -> str:
        return (
            "在项目中搜索代码或读取文件内容。\n"
            "用法:\n"
            "  - 传 keyword 搜索文件内容（返回匹配行）\n"
            "  - 传 file_path 直接读取文件内容\n"
            "支持通配符 file_pattern 过滤文件类型（默认 *.py）"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="keyword", type="string",
                          description="搜索关键字", required=False),
            ToolParameter(name="file_path", type="string",
                          description="要读取的文件路径（相对或绝对）", required=False),
            ToolParameter(name="file_pattern", type="string",
                          description="文件过滤模式（默认 *.py，仅搜索时生效）",
                          required=False, default="*.py"),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        file_path = kwargs.get("file_path")
        keyword = kwargs.get("keyword", "")
        file_pattern = kwargs.get("file_pattern", "*.py")

        if file_path:
            return await self._read_file(call_id, file_path)
        if keyword:
            return await self._search_code(call_id, keyword, file_pattern)

        return ToolResult.error(call_id, self.name, "需要提供 keyword 或 file_path")

    # ── 读取文件 ──

    async def _read_file(self, call_id: str, path: str) -> ToolResult:
        full_path = path if os.path.isabs(path) else os.path.join(self.base_dir, path)
        if not os.path.exists(full_path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
            lines = content.split("\n")
            return ToolResult.success(call_id, self.name, {
                "path": path,
                "size": len(content),
                "lines": len(lines),
                "content": content,
            })
        except Exception as e:
            return ToolResult.error(call_id, self.name, f"读取失败: {e}")

    # ── 搜索代码 ──

    async def _search_code(self, call_id: str, keyword: str, file_pattern: str) -> ToolResult:
        import fnmatch
        results = []
        for root, dirs, fnames in os.walk(self.base_dir):
            dirs[:] = [d for d in dirs
                       if d not in ('.venv', '__pycache__', '.git', 'node_modules', '.idea')]
            for fname in fnames:
                if not fnmatch.fnmatch(fname, file_pattern):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if keyword in line:
                                rel = os.path.relpath(fpath, self.base_dir)
                                results.append({
                                    "file": rel,
                                    "line": i,
                                    "content": line.strip()[:150],
                                })
                except Exception:
                    pass

        return ToolResult.success(call_id, self.name, {
            "keyword": keyword,
            "results": results,
            "count": len(results),
        })


# ═══════════════════════════════════════════
#  Tool 2: CodeEditorTool
# ═══════════════════════════════════════════

class CodeEditorTool(BaseTool):
    """代码编辑器 — 合一操作"""

    # 类级别编辑历史（跨调用持久化）
    _shared_history: dict = {}

    def __init__(self, base_dir: str = ".", **kwargs):
        self.base_dir = base_dir
        self._edit_counter = 0
        if "history" not in self._shared_history:
            self._shared_history["history"] = []

    @property
    def name(self) -> str:
        return "code_editor"

    @property
    def description(self) -> str:
        return (
            "代码编辑器。\n"
            "操作类型 (action):\n"
            "  - read:     读取文件内容（支持 offset/limit 分页）\n"
            "  - diff:     预览修改（安全，不写入文件）\n"
            "  - write:    写入修改（自动备份，支持 edit_id 回滚）\n"
            "  - rollback: 回滚修改（传 edit_id 回滚指定编辑，不传回滚最后一步）\n"
            "  - append:   追加内容到文件末尾\n\n"
            "安全流程: read → diff → write（确认 diff 后）"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="action", type="string",
                          description="操作: read/diff/write/rollback/append",
                          required=True,
                          enum=["read", "diff", "write", "rollback", "append"]),
            ToolParameter(name="path", type="string",
                          description="文件路径", required=True),
            ToolParameter(name="old_text", type="string",
                          description="要替换的旧文本（diff/write 时必填）", required=False),
            ToolParameter(name="new_text", type="string",
                          description="新文本（diff/write/append 时必填）", required=False),
            ToolParameter(name="content", type="string",
                          description="追加内容（append 时用，同 new_text）", required=False),
            ToolParameter(name="edit_id", type="number",
                          description="编辑 ID（rollback 时可选，指定回滚目标）", required=False),
            ToolParameter(name="offset", type="number",
                          description="起始行号（read 时可选，默认 1）", required=False, default=1),
            ToolParameter(name="limit", type="number",
                          description="读取行数（read 时可选，默认 200）", required=False, default=200),
        ]

    # ── 编辑历史 ──

    @property
    def _edit_history(self) -> list:
        return self._shared_history["history"]

    # ── 内部方法 ──

    def _resolve(self, rel_path: str) -> str:
        return rel_path if os.path.isabs(rel_path) else os.path.join(self.base_dir, rel_path)

    def _backup(self, path: str) -> Optional[str]:
        bak_path = path + ".bak"
        try:
            shutil.copy2(path, bak_path)
            return bak_path
        except Exception:
            return None

    @staticmethod
    def _generate_diff(old: str, new: str, path: str) -> str:
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
        return "".join(diff)

    # ── 主入口 ──

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        action = kwargs.get("action", "read")
        path = kwargs.get("path", "")
        full_path = self._resolve(path)

        if action == "read":
            return await self._read_file(call_id, path, full_path, kwargs)
        elif action == "diff":
            return await self._diff_file(call_id, path, full_path, kwargs)
        elif action == "write":
            return await self._write_file(call_id, path, full_path, kwargs)
        elif action == "rollback":
            return await self._rollback_file(call_id, path, full_path, kwargs)
        elif action == "append":
            return await self._append_file(call_id, path, full_path, kwargs)
        else:
            return ToolResult.error(call_id, self.name, f"未知操作: {action}")

    async def _read_file(self, call_id, path, full_path, params):
        if not os.path.exists(full_path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        offset = int(params.get("offset", 1))
        limit = int(params.get("limit", 200))

        with open(full_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        total = len(lines)
        start = max(0, offset - 1)
        end = min(total, start + limit)
        content = "".join(lines[start:end])

        return ToolResult.success(call_id, self.name, {
            "path": path,
            "content": content,
            "total_lines": total,
            "start_line": offset,
            "end_line": end,
        })

    async def _diff_file(self, call_id, path, full_path, params):
        old_text = params.get("old_text", "")
        new_text = params.get("new_text", "")

        if not old_text:
            return ToolResult.error(call_id, self.name, "缺少 old_text")
        if not os.path.exists(full_path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            return ToolResult.error(call_id, self.name, "未找到匹配文本")
        if count > 1:
            return ToolResult.error(call_id, self.name, f"匹配到 {count} 处，请提供更精确的定位")

        new_content = content.replace(old_text, new_text, 1)
        diff = self._generate_diff(content, new_content, path)

        return ToolResult.success(call_id, self.name, {
            "path": path,
            "diff": diff,
            "matches": count,
            "note": "确认 diff 无误后，请使用 action=write 提交",
        })

    async def _write_file(self, call_id, path, full_path, params):
        old_text = params.get("old_text", "")
        new_text = params.get("new_text", "")

        if not os.path.exists(full_path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            return ToolResult.error(call_id, self.name, "未找到匹配文本")
        if count > 1:
            return ToolResult.error(call_id, self.name, f"匹配到 {count} 处，请提供更精确的定位")

        # 备份
        bak = self._backup(full_path)

        # 写入
        new_content = content.replace(old_text, new_text, 1)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        # 记录
        self._edit_counter += 1
        self._edit_history.append({
            "id": self._edit_counter,
            "path": path,
            "backup": bak,
            "old_text": old_text,
            "new_text": new_text,
        })

        return ToolResult.success(call_id, self.name, {
            "path": path,
            "edit_id": self._edit_counter,
            "backup": bak,
            "note": f"编辑 #{self._edit_counter} 已应用。回滚请使用 action=rollback edit_id={self._edit_counter}",
        })

    async def _rollback_file(self, call_id, path, full_path, params):
        edit_id = params.get("edit_id")

        if edit_id:
            entries = [e for e in self._edit_history if e["id"] == edit_id]
        else:
            entries = [self._edit_history[-1]] if self._edit_history else []

        if not entries:
            return ToolResult.error(call_id, self.name, "没有可回滚的编辑")

        entry = entries[0]
        bak_path = entry.get("backup")

        if bak_path and os.path.exists(bak_path):
            shutil.copy2(bak_path, self._resolve(entry["path"]))
            method = "backup"
        else:
            # 反向替换
            target_path = self._resolve(entry["path"])
            if os.path.exists(target_path):
                with open(target_path, "r", encoding="utf-8") as f:
                    content = f.read()
                new_content = content.replace(entry["new_text"], entry["old_text"], 1)
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                method = "reverse"
            else:
                return ToolResult.error(call_id, self.name, f"文件已不存在: {entry['path']}")

        return ToolResult.success(call_id, self.name, {
            "path": entry["path"],
            "rolled_back": True,
            "method": method,
            "edit_id": entry["id"],
        })

    async def _append_file(self, call_id, path, full_path, params):
        new_text = params.get("new_text", params.get("content", ""))
        if not new_text:
            return ToolResult.error(call_id, self.name, "缺少追加内容")

        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        with open(full_path, "a", encoding="utf-8") as f:
            f.write(new_text)
            if not new_text.endswith("\n"):
                f.write("\n")

        return ToolResult.success(call_id, self.name, {
            "path": path,
            "appended": True,
        })
