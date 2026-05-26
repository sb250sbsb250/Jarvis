"""
文件操作工具 — 3合1：读/写/列目录
"""

import os
import logging
from typing import List

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.file")


class FileTool(BaseTool):
    """文件操作工具（3合1）"""

    def __init__(self, **kwargs):
        pass

    @property
    def name(self) -> str:
        return "file"

    @property
    def description(self) -> str:
        return ("文件操作工具。action 可选："
                "read(读取文件) / write(覆盖写入) / list(列出目录)。"
                "标准流程：list → read → write")

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="action", type="string", required=True,
                          description="操作: read/write/list",
                          enum=["read", "write", "list"]),
            ToolParameter(name="path", type="string", required=False,
                          description="文件或目录路径"),
            ToolParameter(name="content", type="string", required=False,
                          description="写入内容（action=write 时必需）"),
            ToolParameter(name="encoding", type="string", required=False,
                          description="编码（默认 utf-8）", default="utf-8"),
            ToolParameter(name="recursive", type="boolean", required=False,
                          description="列出目录时是否递归（action=list）",
                          default=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        action = kwargs.get("action", "read")
        path = kwargs.get("path", "")

        try:
            if action == "read":
                return await self._read(call_id, path,
                                        kwargs.get("encoding", "utf-8"))
            elif action == "write":
                return await self._write(call_id, path,
                                         kwargs.get("content", ""),
                                         kwargs.get("encoding", "utf-8"))
            elif action == "list":
                return await self._list(call_id, path,
                                        kwargs.get("recursive", False))
            else:
                return ToolResult.error(call_id, self.name,
                                        f"未知操作: {action}")
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))

    async def _read(self, call_id: str, path: str, encoding: str) -> ToolResult:
        if not path:
            return ToolResult.error(call_id, self.name,
                                    "缺少必需参数: path（文件路径不能为空）")
        try:
            with open(path, "r", encoding=encoding) as f:
                content = f.read()
            return ToolResult.success(call_id, self.name, {
                "content": content, "path": path, "size": len(content),
            })
        except FileNotFoundError:
            return ToolResult.error(call_id, self.name,
                                    f"文件不存在: {path}")
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))

    async def _write(self, call_id: str, path: str,
                     content: str, encoding: str) -> ToolResult:
        if not path:
            return ToolResult.error(call_id, self.name,
                                    "缺少必需参数: path（文件路径不能为空）")
        if not content:
            return ToolResult.error(call_id, self.name,
                                    "缺少必需参数: content（写入内容不能为空）")
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding=encoding) as f:
                f.write(content)
            return ToolResult.success(call_id, self.name, {
                "path": path, "size": len(content), "action": "written",
            })
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))

    async def _list(self, call_id: str, path: str,
                    recursive: bool) -> ToolResult:
        if not path:
            return ToolResult.error(call_id, self.name,
                                    "缺少必需参数: path（目录路径不能为空）")
        try:
            entries = []
            if recursive:
                for root, dirs, files in os.walk(path):
                    for f in files:
                        full = os.path.join(root, f)
                        rel = os.path.relpath(full, path)
                        entries.append({
                            "name": rel, "size": os.path.getsize(full),
                            "type": "file",
                        })
                    for d in dirs:
                        full = os.path.join(root, d)
                        rel = os.path.relpath(full, path)
                        entries.append({
                            "name": rel + "/", "size": 0, "type": "dir",
                        })
            else:
                for entry in os.scandir(path):
                    entries.append({
                        "name": entry.name,
                        "size": entry.stat().st_size,
                        "type": "dir" if entry.is_dir() else "file",
                    })
            return ToolResult.success(call_id, self.name, {
                "path": path, "entries": entries, "count": len(entries),
            })
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))
