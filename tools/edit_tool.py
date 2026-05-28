"""
文本编辑工具 — 2合1：搜索替换/插入
"""

import os
import re
import logging
from typing import List

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.edit")


class EditTool(BaseTool):
    """文本编辑操作工具（2合1）"""

    def __init__(self, **kwargs):
        pass

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return (
            "文本编辑工具。action 可选："
            "replace(搜索替换)/insert(插入内容)。"
            "replace 用旧文本精确匹配后替换；"
            "insert 在指定行号后或指定文本后追加内容。\n"
            "\n"
            "📖 使用示例：\n"
            "  # replace — 搜索替换:\n"
            "  edit(action='replace', path='app.py', old_text='旧代码', new_text='新代码')\n"
            "  💡 old_text 必须是唯一匹配（不能有2处相同文本）\n"
            "\n"
            "  # insert — 在文本后插入:\n"
            "  edit(action='insert', path='app.py', insert_after='def hello():', content='    return 42')\n"
            "\n"
            "  # insert — 在行号后插入:\n"
            "  edit(action='insert', path='app.py', insert_line=5, content='# 新行')\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="action", type="string", required=True,
                          description="操作: replace/insert",
                          enum=["replace", "insert"]),
            ToolParameter(name="path", type="string", required=True,
                          description="文件路径"),
            ToolParameter(name="old_text", type="string", required=False,
                          description="要替换的旧文本（精确匹配，action=replace 时必需）"),
            ToolParameter(name="new_text", type="string", required=False,
                          description="替换后的新文本（action=replace 时必需）"),
            ToolParameter(name="insert_line", type="number", required=False,
                          description="在此行号后插入（action=insert 时可选）"),
            ToolParameter(name="insert_after", type="string", required=False,
                          description="在此文本后插入（action=insert 时可选）"),
            ToolParameter(name="content", type="string", required=False,
                          description="要插入的内容（action=insert 时必需）"),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        action = kwargs.get("action", "replace")
        path = kwargs.get("path", "")

        if not path:
            return ToolResult.error(call_id, self.name,
                                    "缺少必需参数: path（文件路径不能为空）")

        try:
            if action == "replace":
                return await self._replace(call_id, path,
                                           kwargs.get("old_text", ""),
                                           kwargs.get("new_text", ""))
            elif action == "insert":
                return await self._insert(call_id, path,
                                          kwargs.get("insert_line"),
                                          kwargs.get("insert_after"),
                                          kwargs.get("content", ""))
            else:
                return ToolResult.error(call_id, self.name,
                                        f"未知操作: {action}")
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))

    async def _replace(self, call_id: str, path: str,
                       old_text: str, new_text: str) -> ToolResult:
        if not old_text:
            return ToolResult.error(call_id, self.name,
                                    "replace 需要提供 old_text 参数")
        if new_text is None:
            return ToolResult.error(call_id, self.name,
                                    "replace 需要提供 new_text 参数")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if old_text not in content:
                return ToolResult.error(call_id, self.name,
                                        f"未找到要替换的文本: {old_text[:50]}")
            new_content = content.replace(old_text, new_text, 1)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return ToolResult.success(call_id, self.name, {
                "path": path, "replaced": True, "new_size": len(new_content),
            })
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))

    async def _insert(self, call_id: str, path: str,
                      insert_line, insert_after, content: str) -> ToolResult:
        if not content:
            return ToolResult.error(call_id, self.name,
                                    "insert 需要提供 content 参数")
        if insert_line is None and not insert_after:
            return ToolResult.error(call_id, self.name,
                                    "insert 需要提供 insert_line 或 insert_after")
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if insert_line is not None:
                lines.insert(insert_line, content + "\n")
                new_content = "".join(lines)
            else:
                new_content = ""
                for line in lines:
                    new_content += line
                    if insert_after in line:
                        new_content += content + "\n"
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return ToolResult.success(call_id, self.name, {
                "path": path, "inserted": True,
            })
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))
