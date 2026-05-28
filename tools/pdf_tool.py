"""
PDF 工具 — 读取 PDF 内容（支持文本提取、表格提取、扫描件检测、智能截断）
"""

import logging
import os
from typing import Any, Dict, List, Optional

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.pdf")


class PdfReadTool(BaseTool):
    """PDF 文件读取工具（带表格提取 + 扫描件检测 + 关键词优先截断）"""

    def __init__(self, **kwargs):
        pass

    @property
    def name(self) -> str:
        return "pdf_read"

    @property
    def description(self) -> str:
        return (
            "读取 PDF 文件内容（支持银行询证函等表格类 PDF）\n"
            "可检测扫描件，支持多引擎降级（pdfplumber → PyPDF2）\n"
            "自动提取表格结构，按关键词优先截断保留重要信息\n"
            "\n"
            "📖 使用示例：\n"
            "  # 读全部:\n"
            "  pdf_read(path='report.pdf')\n"
            "  # 指定页码范围:\n"
            "  pdf_read(path='report.pdf', start_page=1, max_pages=3)\n"
            "  💡 返回每页文本+表格结构。自动检测扫描件（图片类PDF）。\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="path", type="string", description="PDF 文件路径", required=True),
            ToolParameter(name="max_pages", type="number", description="最大读取页数", required=False, default=10),
            ToolParameter(name="extract_tables", type="boolean", description="是否提取表格（银行询证函建议开启）", required=False, default=False),
            ToolParameter(name="keywords", type="string", description="逗号分隔的关键词，优先保留含这些词的页面", required=False),
        ]

    # ── 元数据提取 ──

    def _extract_metadata(self, path: str) -> Dict[str, Any]:
        """提取 PDF 文件元数据"""
        meta: Dict[str, Any] = {
            "file_name": os.path.basename(path),
            "file_size": os.path.getsize(path),
        }
        try:
            import PyPDF2
            with open(path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                meta["page_count"] = len(reader.pages)
                info = reader.metadata
                if info:
                    for k in ("/Title", "/Author", "/Subject", "/Creator", "/Producer", "/CreationDate", "/ModDate"):
                        v = info.get(k)
                        if v:
                            meta[k.lstrip("/").lower()] = str(v)
        except Exception:
            pass
        return meta

    def _is_likely_scanned(self, path: str) -> bool:
        """通过文件头快速判断是否为扫描件"""
        try:
            with open(path, "rb") as f:
                head = f.read(2048)
            text_markers = [b"/Text", b"/Font", b"/CIDFont"]
            return not any(m in head for m in text_markers)
        except Exception:
            return False

    # ── 文本提取（多引擎降级） ──

    async def _extract_text(self, path: str, max_pages: int) -> Dict[str, Any]:
        """提取文本 + 表格，优先 pdfplumber（中文支持好）"""

        result: Dict[str, Any] = {
            "content": "",
            "pages_read": 0,
            "total_pages": 0,
            "method": "",
            "tables": [],
        }

        # 引擎 1：pdfplumber（最佳中文 + 表格支持）
        try:
            import pdfplumber  # type: ignore

            with pdfplumber.open(path) as pdf:
                result["total_pages"] = len(pdf.pages)
                text_parts: List[str] = []
                for i, page in enumerate(pdf.pages[:max_pages]):
                    page_text = page.extract_text() or ""
                    if page_text.strip():
                        text_parts.append(f"\n--- Page {i + 1} ---\n{page_text}")
                        result["pages_read"] += 1
                    # 表格提取
                    if page.find_tables():
                        for tbl in page.extract_tables():
                            if tbl and any(any(cell for cell in row) for row in tbl):
                                result["tables"].append({
                                    "page": i + 1,
                                    "headers": [str(c or "") for c in tbl[0]],
                                    "rows": [[str(c or "") for c in row] for row in tbl[1:]],
                                })
                result["content"] = "".join(text_parts)
                result["method"] = "pdfplumber"
                return result
        except ImportError:
            pass

        # 引擎 2：PyPDF2（基础兼容）
        try:
            import PyPDF2  # type: ignore

            with open(path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                result["total_pages"] = len(reader.pages)
                text_parts = []
                for i in range(min(len(reader.pages), max_pages)):
                    page_text = reader.pages[i].extract_text() or ""
                    if page_text.strip():
                        text_parts.append(f"\n--- Page {i + 1} ---\n{page_text}")
                        result["pages_read"] += 1
                result["content"] = "".join(text_parts)
                result["method"] = "PyPDF2"
                return result
        except ImportError:
            pass

        raise ImportError("需要安装 pdfplumber 或 PyPDF2: pip install pdfplumber")

    # ── 智能截断（关键词优先） ──

    def _smart_truncate(self, content: str, max_chars: int = 15000, keywords: Optional[List[str]] = None) -> str:
        """按关键词优先截断，保留含关键词的页面"""
        if len(content) <= max_chars:
            return content

        pages = content.split("--- Page ")
        if not pages:
            return content[:max_chars]

        keywords = [kw.strip().lower() for kw in keywords] if keywords else []

        important: List[str] = []
        others: List[str] = []

        for page in pages:
            if not page.strip():
                continue
            page_lower = page.lower()
            if keywords and any(kw in page_lower for kw in keywords):
                important.append(page)
            else:
                others.append(page)

        # 优先组装重要页面
        result = ""
        for page in important:
            chunk = f"--- Page {page}"
            if len(result) + len(chunk) > max_chars - 200:
                result += chunk[:(max_chars - len(result) - 200)]
                result += "\n...[重要内容截断]"
                break
            result += chunk

        # 用其他页面填充剩余
        remaining = max_chars - len(result) - 50
        for page in others:
            if remaining <= 0:
                break
            chunk = f"\n--- Page {page}"
            if len(chunk) <= remaining:
                result += chunk
                remaining -= len(chunk)
            else:
                result += chunk[:remaining]
                break

        if len(content) > len(result):
            result += "\n...[内容过多已截断]"

        return result

    # ── 主入口 ──

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        path = kwargs.get("path", "")
        max_pages = kwargs.get("max_pages", 10)
        extract_tables = kwargs.get("extract_tables", False)
        keywords_str = kwargs.get("keywords", "")

        if not path:
            return ToolResult.error(call_id, self.name, "请提供 PDF 文件路径")
        if not os.path.exists(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        try:
            # 1. 元数据
            metadata = self._extract_metadata(path)
            metadata["likely_scanned"] = self._is_likely_scanned(path)

            # 2. 文本提取
            result_data = await self._extract_text(path, max_pages)

            # 3. 表格（仅当 pdfplumber 已使用时才有效）
            if extract_tables and result_data.get("tables"):
                tbl_summary = []
                for tbl in result_data["tables"]:
                    tbl_summary.append({
                        "page": tbl["page"],
                        "headers": tbl["headers"],
                        "rows": len(tbl["rows"]),
                    })
                result_data["table_count"] = len(tbl_summary)
                result_data["table_summary"] = tbl_summary

            # 4. 关键词智能截断（仅当有内容且有关键词时）
            content = result_data.get("content", "")
            if content and keywords_str:
                kw_list = [k.strip() for k in keywords_str.split(",") if k.strip()]
                content = self._smart_truncate(content, max_chars=15000, keywords=kw_list)

            # 5. 合并结果
            return ToolResult.success(call_id, self.name, {
                "path": path,
                "pages_read": result_data["pages_read"],
                "total_pages": result_data["total_pages"],
                "content": content[:15000],
                "method": result_data["method"],
                "metadata": metadata,
                "table_count": result_data.get("table_count", 0),
                "table_summary": result_data.get("table_summary", []),
                "tables": result_data.get("tables", []) if extract_tables else [],
            })

        except ImportError as e:
            return ToolResult.error(call_id, self.name, str(e))
        except Exception as e:
            logger.exception(f"PDF 读取失败: {path}")
            return ToolResult.error(call_id, self.name, str(e))
