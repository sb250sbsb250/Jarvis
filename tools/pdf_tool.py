"""
PDF 工具 — 读取 PDF 内容
"""

import logging
from typing import List

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.pdf")


class PdfReadTool(BaseTool):

    def __init__(self, **kwargs):
        pass

    """读取 PDF 文件"""

    @property
    def name(self) -> str:
        return "pdf_read"

    @property
    def description(self) -> str:
        return "读取 PDF 文件内容"

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="path", type="string", description="PDF 文件路径", required=True),
            ToolParameter(name="max_pages", type="number", description="最大页数", required=False, default=10),
        ]

    async def execute(self, call_id, **kwargs) -> ToolResult:
        path = kwargs.get("path", "")
        max_pages = kwargs.get("max_pages", 10)
        try:
            import PyPDF2
            text = ""
            with open(path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                pages = min(len(reader.pages), max_pages)
                for i in range(pages):
                    text += f"\n--- Page {i+1} ---\n"
                    text += reader.pages[i].extract_text() or ""
            return ToolResult.success(call_id, self.name, {
                "path": path, "pages_read": pages, "total_pages": len(reader.pages), "content": text[:10000],
            })
        except ImportError:
            try:
                import pdfplumber
                text = ""
                with pdfplumber.open(path) as pdf:
                    for i, page in enumerate(pdf.pages[:max_pages]):
                        text += f"\n--- Page {i+1} ---\n"
                        text += page.extract_text() or ""
                return ToolResult.success(call_id, self.name, {
                    "path": path, "pages_read": min(len(pdf.pages), max_pages), "total_pages": len(pdf.pages), "content": text[:10000],
                })
            except ImportError:
                return ToolResult.error(call_id, self.name, "需要安装 PyPDF2 或 pdfplumber: pip install PyPDF2")
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))


