"""
tools/code_tool.py — 代码工具（读写一体）

代码文件的精确操作：读取 → 预览 → 编辑 → 回滚
"""

import os
import difflib
import shutil
import logging
from typing import Any, Dict, List, Optional

from engine.tool.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class CodeTool(BaseTool):
    """代码工具 — read/diff/write/rollback/append/create"""

    _history: List[Dict] = []

    @property
    def name(self) -> str:
        return "code"

    @property
    def description(self) -> str:
        return (
            "代码读写工具。action:\n"
            "- read:     读取文件（自动提取结构，大文件分页）\n"
            "  code(action='read', path='app.py', start_line=10, end_line=50)\n"
            "- diff:     预览替换差异（不改文件）\n"
            "  code(action='diff', path='app.py', old_text='旧代码', new_text='新代码')\n"
            "- write:    提交修改（自动备份 .bak）\n"
            "  code(action='write', path='app.py', old_text='旧代码', new_text='新代码')\n"
            "- rollback: 撤销最近一次编辑\n"
            "  code(action='rollback', path='app.py')\n"
            "- append:   追加内容到文件末尾\n"
            "  code(action='append', path='app.py', new_text='新内容')\n"
            "- create:   创建新文件或覆盖已有文件\n"
            "  code(action='create', path='new.py', new_text='完整内容')\n"
            "\n"
            "安全流程: read → diff → write。old_text 必须在文件中唯一。"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("action", "string", "操作", required=True,
                          enum=["read", "diff", "write", "rollback", "append", "create"]),
            ToolParameter("path", "string", "文件路径", required=True),
            ToolParameter("old_text", "string", "要替换的旧文本 (diff/write)", required=False),
            ToolParameter("new_text", "string", "替换后的新文本 (diff/write/append/create)", required=False),
            ToolParameter("start_line", "number", "起始行 (read, 默认1)", required=False),
            ToolParameter("end_line", "number", "结束行 (read, 默认200)", required=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        action = kwargs.get("action", "read")

        handlers = {
            "read": self._read,
            "diff": self._diff,
            "write": self._write,
            "rollback": self._rollback,
            "append": self._append,
            "create": self._create,
        }
        handler = handlers.get(action)
        if not handler:
            return ToolResult.error(call_id, self.name, f"未知操作: {action}")
        return await handler(call_id, kwargs)

    # ── read ──────────────────────────────────────────────

    async def _read(self, call_id, args):
        path = args.get("path", "")
        if not os.path.isfile(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        total = len(lines)
        start = max(0, int(args.get("start_line", 1)) - 1)
        end = min(total, int(args.get("end_line", total)))
        content = "".join(lines[start:end])

        # 大文件提取结构索引
        structure = []
        for i, line in enumerate(lines[:min(500, total)]):
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in
                   ["class ", "def ", "async def ", "import ", "from ", "@"]):
                if len(stripped) < 120:
                    structure.append(f"L{i+1}: {stripped}")

        result = {
            "path": path, "total_lines": total,
            "lines": f"{start+1}-{end}", "content": content,
        }
        if total > 200:
            result["structure"] = structure[:30]
            result["_hint"] = f"文件共 {total} 行。用 start_line/end_line 翻页"
        else:
            result["_hint"] = "如需修改，先用 code(action='diff', ...) 预览"

        return ToolResult.success(call_id, self.name, result)

    # ── diff ──────────────────────────────────────────────

    async def _diff(self, call_id, args):
        path = args.get("path", "")
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")

        if not os.path.isfile(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")
        if not old_text:
            return ToolResult.error(call_id, self.name, "diff 需要 old_text")

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            return ToolResult.error(call_id, self.name,
                "未找到匹配文本。请用 code(action='read', path='...') 确认文件内容，复制原文（含缩进）。")
        if count > 1:
            return ToolResult.error(call_id, self.name,
                f"匹配到 {count} 处。请扩大 old_text 范围使其唯一。")

        new_content = content.replace(old_text, new_text, 1)
        diff = difflib.unified_diff(
            content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
        )
        return ToolResult.success(call_id, self.name, {
            "path": path, "diff": "".join(diff),
            "_hint": "确认 diff 无误后，用 code(action='write', ...) 提交修改",
        })

    # ── write ─────────────────────────────────────────────

    async def _write(self, call_id, args):
        path = args.get("path", "")
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")

        if not os.path.isfile(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")
        if not old_text:
            return ToolResult.error(call_id, self.name, "write 需要 old_text")

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            return ToolResult.error(call_id, self.name, "未找到匹配文本。先用 code(action='diff', ...) 预览。")
        if count > 1:
            return ToolResult.error(call_id, self.name, f"匹配到 {count} 处。请扩大 old_text 范围使其唯一。")

        shutil.copy2(path, path + ".bak")
        new_content = content.replace(old_text, new_text, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        self._history.append({"path": path, "backup": path + ".bak", "old": old_text, "new": new_text})
        return ToolResult.success(call_id, self.name, {
            "path": path, "status": "已修改", "backup": path + ".bak",
            "_hint": "修改已应用。用 code(action='rollback', ...) 回滚",
        })

    # ── rollback ──────────────────────────────────────────

    async def _rollback(self, call_id, args):
        path = args.get("path", "")
        entry = next((e for e in reversed(self._history) if e["path"] == path), None)

        if not entry:
            bak = path + ".bak"
            if os.path.exists(bak):
                shutil.copy2(bak, path)
                return ToolResult.success(call_id, self.name, {"path": path, "status": "已从 .bak 恢复"})
            return ToolResult.error(call_id, self.name, f"没有 {path} 的编辑记录")

        if os.path.exists(entry["backup"]):
            shutil.copy2(entry["backup"], path)
        else:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            with open(path, "w", encoding="utf-8") as f:
                f.write(content.replace(entry["new"], entry["old"], 1))

        self._history.remove(entry)
        return ToolResult.success(call_id, self.name, {"path": path, "status": "已回滚"})

    # ── append ────────────────────────────────────────────

    async def _append(self, call_id, args):
        path = args.get("path", "")
        new_text = args.get("new_text", "")

        if not path:
            return ToolResult.error(call_id, self.name, "append 需要 path")
        if not new_text:
            return ToolResult.error(call_id, self.name, "append 需要 new_text")

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + new_text if os.path.isfile(path) and os.path.getsize(path) > 0 else new_text)
            if not new_text.endswith("\n"):
                f.write("\n")

        return ToolResult.success(call_id, self.name, {
            "path": path, "status": "已追加", "size": os.path.getsize(path),
        })

    # ── create ────────────────────────────────────────────

    async def _create(self, call_id, args):
        path = args.get("path", "")
        content = args.get("new_text", "")

        if not path:
            return ToolResult.error(call_id, self.name, "create 需要 path")

        existed = os.path.isfile(path)
        if existed:
            shutil.copy2(path, path + ".bak")

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        return ToolResult.success(call_id, self.name, {
            "path": path, "status": "已创建" if not existed else "已覆盖",
            "size": len(content), "backup": path + ".bak" if existed else None,
        })
