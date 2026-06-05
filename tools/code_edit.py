"""
tools/code_edit.py — 代码编辑工具

安全流程三件套：diff 预览 → write 提交 → rollback 回滚
"""

import os
import difflib
import shutil
import logging
from typing import Any, Dict, List, Optional

from engine.tool.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class CodeEditTool(BaseTool):
    """代码编辑 — diff/write/rollback"""

    _history: List[Dict] = []

    @property
    def name(self) -> str:
        return "code_edit"

    @property
    def description(self) -> str:
        return (
            "代码编辑工具。安全流程: diff → write → rollback\n\n"
            "actions:\n"
            "- diff:     预览替换差异（不改文件）\n"
            "  code_edit(action='diff', path='app.py', old_text='旧', new_text='新')\n"
            "- write:    提交修改（自动备份 .bak）\n"
            "  code_edit(action='write', path='app.py', old_text='旧', new_text='新')\n"
            "- create:   创建新文件或覆盖已有文件\n"
            "  code_edit(action='create', path='new.py', content='完整内容')\n"
            "- rollback: 撤销最近一次编辑\n"
            "  code_edit(action='rollback', path='app.py')\n"
            "\n"
            "注意: old_text 必须在文件中唯一。如需更多上下文，先用 code_graph(action='read')。"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("action", "string", "操作", required=True,
                          enum=["diff", "write", "rollback", "create"]),
            ToolParameter("path", "string", "文件路径", required=True),
            ToolParameter("old_text", "string", "要替换的旧文本 (diff/write)", required=False),
            ToolParameter("new_text", "string", "替换后的新文本 (diff/write) 或文件内容 (create)", required=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        action = kwargs.get("action", "")

        if action == "diff":
            return await self._diff(call_id, kwargs)
        elif action == "write":
            return await self._write(call_id, kwargs)
        elif action == "rollback":
            return await self._rollback(call_id, kwargs)
        elif action == "create":
            return await self._create(call_id, kwargs)
        else:
            return ToolResult.error(call_id, self.name, f"未知操作: {action}")

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
            # 尝试模糊定位
            old_first = old_text.strip().split("\n")[0][:60] if old_text.strip() else ""
            return ToolResult.error(
                call_id, self.name,
                f"未找到匹配文本。old_text 首行: '{old_first}...'\n"
                "请用 code_graph(action='read', path='...') 确认文件内容，"
                "复制原文（含缩进和空行）。"
            )
        if count > 1:
            return ToolResult.error(
                call_id, self.name,
                f"匹配到 {count} 处相同文本。请扩大 old_text 范围（前后多包含几行），"
                "使其在文件中唯一。"
            )

        new_content = content.replace(old_text, new_text, 1)
        diff = difflib.unified_diff(
            content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )

        return ToolResult.success(call_id, self.name, {
            "path": path,
            "diff": "".join(diff),
            "_hint": "确认 diff 无误后，用 code_edit(action='write', ...) 提交修改",
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
            return ToolResult.error(
                call_id, self.name,
                "未找到匹配文本。先用 code_edit(action='diff', ...) 预览确认。"
            )
        if count > 1:
            return ToolResult.error(
                call_id, self.name,
                f"匹配到 {count} 处。请扩大 old_text 范围使其唯一。"
            )

        # 备份
        bak = path + ".bak"
        shutil.copy2(path, bak)

        # 写入
        new_content = content.replace(old_text, new_text, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        self._history.append({
            "path": path,
            "backup": bak,
            "old": old_text,
            "new": new_text,
        })

        return ToolResult.success(call_id, self.name, {
            "path": path,
            "status": "已修改",
            "backup": bak,
            "_hint": "修改已应用。如需回滚用 code_edit(action='rollback', path='...')",
        })

    # ── rollback ──────────────────────────────────────────

    async def _rollback(self, call_id, args):
        path = args.get("path", "")

        entry = next((e for e in reversed(self._history) if e["path"] == path), None)
        if not entry:
            # 尝试从 .bak 恢复
            bak = path + ".bak"
            if os.path.exists(bak):
                shutil.copy2(bak, path)
                return ToolResult.success(call_id, self.name, {
                    "path": path,
                    "status": "已从 .bak 恢复",
                    "_hint": "已恢复到备份状态",
                })
            return ToolResult.error(call_id, self.name, f"没有 {path} 的编辑记录，也没有 .bak 文件")

        if os.path.exists(entry["backup"]):
            shutil.copy2(entry["backup"], path)
        else:
            # 反向替换
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            with open(path, "w", encoding="utf-8") as f:
                f.write(content.replace(entry["new"], entry["old"], 1))

        self._history.remove(entry)
        return ToolResult.success(call_id, self.name, {
            "path": path,
            "status": "已回滚",
            "_hint": "已恢复到修改前状态",
        })

    # ── create ──

    async def _create(self, call_id, args):
        """创建新文件或覆盖已有文件"""
        path = args.get("path", "")
        content = args.get("new_text", args.get("content", ""))

        if not path:
            return ToolResult.error(call_id, self.name, "create 需要 path")

        # 如果文件已存在，先备份
        existed = os.path.isfile(path)
        if existed:
            shutil.copy2(path, path + ".bak")

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        return ToolResult.success(call_id, self.name, {
            "path": path,
            "status": "已创建" if not existed else "已覆盖",
            "size": len(content),
            "backup": path + ".bak" if existed else None,
            "_hint": (
                "文件已创建。如需修改，用 code_edit(action='diff/write', ...)"
                if not existed else "原文件已备份到 .bak。"
            ),
        })
