"""
tools/word_tool.py — Word 文档工具

独立 Word 操作，从 office_tool 拆分。
"""

import os
import logging
from typing import List

from engine.tool.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class WordTool(BaseTool):
    """Word 文档 — 独立工具"""

    @property
    def name(self) -> str:
        return "word"

    @property
    def description(self) -> str:
        return (
            "Word 文档操作。action: read_docx / write_docx\n"
            "- read_docx: path='a.docx'\n"
            "- write_docx: path='a.docx', text='内容'"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("action", "string", "read_docx/write_docx", required=True,
                          enum=["read_docx", "write_docx"]),
            ToolParameter("path", "string", "文件路径", required=True),
            ToolParameter("text", "string", "写入内容(write_docx用)", required=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        action = kwargs.get("action", "read_docx")
        path = kwargs.get("path", "")

        if not path or not os.path.exists(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        if action == "read_docx":
            return await self._read_docx(call_id, path)
        elif action == "write_docx":
            return await self._write_docx(call_id, path, kwargs.get("text", ""))
        else:
            return ToolResult.error(call_id, self.name, f"未知操作: {action}")

    async def _read_docx(self, call_id, path):
        try:
            from docx import Document
            doc = Document(path)
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
            return ToolResult.success(call_id, self.name, {
                "path": path,
                "content": text[:15000],
                "paragraphs": len(doc.paragraphs),
            })
        except ImportError:
            pass

        # fallback: zip+xml 解析
        try:
            from zipfile import ZipFile
            from xml.etree.ElementTree import parse
            with ZipFile(path) as z:
                with z.open("word/document.xml") as xml_file:
                    tree = parse(xml_file)
                    text = "".join(
                        node.text or ""
                        for node in tree.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
                    )
                    return ToolResult.success(call_id, self.name, {
                        "path": path,
                        "content": text[:15000],
                        "_hint": "pip install python-docx 获得更好的格式支持",
                    })
        except Exception as e:
            return ToolResult.error(call_id, self.name, f"读取失败: {e}")

    async def _write_docx(self, call_id, path, text):
        if not text:
            return ToolResult.error(call_id, self.name, "write_docx 需要 text")

        try:
            from docx import Document
            doc = Document()
            for line in text.split("\n"):
                if line.strip():
                    doc.add_paragraph(line)
            doc.save(path)
            return ToolResult.success(call_id, self.name, {
                "path": path,
                "status": "已创建",
                "size": os.path.getsize(path),
                "_hint": f"文件已保存到 {path}",
            })
        except ImportError:
            return ToolResult.error(call_id, self.name, "需要 python-docx: pip install python-docx")
