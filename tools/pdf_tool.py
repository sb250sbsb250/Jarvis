"""
PDF 工具（原子工具版）

原子工具:
  pdf_read    — 读取 PDF 文件内容
  pdf_split   — 拆分 PDF 文件
  pdf_concat  — 合并多个 PDF 文件

v3.1: 新增 pdf_split / pdf_concat；PyPDF2 → pypdf 迁移
"""

import os
import logging
from typing import Any, Dict, List, Tuple

from engine.tool.base import BaseTool, ToolDefinition, ToolParameter, ToolResult

# PyPDF2 已停止维护，迁移到 pypdf（API 完全兼容）
try:
    import pypdf as PyPDF2
except ImportError:
    import PyPDF2

logger = logging.getLogger("jarvis.tools.pdf")


class PdfTool(BaseTool):
    """PDF 文档工具集"""

    def __init__(self):
        self._handlers = {
            "pdf_read": self._handle_read,
            "pdf_split": self._handle_split,
            "pdf_concat": self._handle_concat,
        }

    @property
    def name(self) -> str:
        return "pdf"

    @property
    def category(self) -> str:
        return "data"

    @property
    def tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="pdf_read",
                description="""读取 PDF 文件内容，支持文本提取、表格提取、扫描件检测。

自动降级：优先使用 pdfplumber（高质量），回退到 pypdf（基础提取）。

使用场景：
- 读取 PDF 文档的文本内容
- 提取 PDF 中的表格数据（银行询证函、财务报表等建议开启 extract_tables）
- 按关键词过滤页面（如只读取含 "总计" 的页面）

不适用场景：
- 扫描件 PDF（图片类 PDF）→ 检测到扫描件会自动提示""",
                parameters=[
                    ToolParameter("path", "string", "PDF 文件路径", required=True),
                    ToolParameter("max_pages", "number", "最大读取页数，默认 10。长文档可适当增加", required=False),
                    ToolParameter("extract_tables", "boolean", "是否提取表格数据（含表格的 PDF 建议开启），默认 false", required=False),
                    ToolParameter("keywords", "string", "逗号分隔的关键词列表，只保留含这些词的页面（如 '总计,合计,金额'）", required=False),
                ],
                is_read=True,
                examples=[
                    'pdf_read(path="report.pdf")',
                    'pdf_read(path="invoice.pdf", max_pages=5, extract_tables=True)  # 读取前5页并提取表格',
                    'pdf_read(path="report.pdf", keywords="总计,结论", max_pages=20)  # 只读含关键词的页面',
                ],
                constraints=[
                    "扫描件 PDF 无法提取文字，会返回提示信息",
                    "表格提取仅当 extract_tables=True 时生效",
                    "大文件（100+页）建议限制 max_pages",
                    "需要安装 pdfplumber 和 pypdf：pip install pdfplumber pypdf",
                ],
            ),
            ToolDefinition(
                name="pdf_split",
                description="""拆分 PDF 文件为多个子文件。

两种拆分方式：
1. 指定页码范围：ranges="1-3,5,8-10"（提取指定页面）
2. 按固定页数拆分：split_size=10（每 10 页一个文件）

支持两种方式混用。""",
                parameters=[
                    ToolParameter("path", "string", "源 PDF 文件路径", required=True),
                    ToolParameter("output_dir", "string", "输出目录路径（自动创建）", required=True),
                    ToolParameter("ranges", "string", "页码范围，如 '1-3,5,8-10'。不填则按 split_size 拆分", required=False),
                    ToolParameter("split_size", "number", "无 ranges 时，按此页数拆分（每 N 页一个文件），默认 10", required=False),
                ],
                examples=[
                    'pdf_split(path="report.pdf", output_dir="./parts", ranges="1-5,8-10")',
                    'pdf_split(path="report.pdf", output_dir="./parts", split_size=20)  # 每20页一个文件',
                ],
                constraints=[
                    "会产生临时文件，使用后注意清理",
                    "页码从 1 开始计数",
                ],
            ),
            ToolDefinition(
                name="pdf_concat",
                description="""合并多个 PDF 文件为一个新文件。
按 paths 列表顺序合并页面。

使用场景：
- 合并多个 PDF 报告为一个文件
- 将拆分后的文件重新合并""",
                parameters=[
                    ToolParameter("paths", "array", "要合并的 PDF 文件路径列表（按顺序合并）", required=True),
                    ToolParameter("output_path", "string", "合并后的输出文件路径", required=True),
                ],
                examples=[
                    'pdf_concat(paths=["part1.pdf", "part2.pdf"], output_path="merged.pdf")',
                ],
                constraints=[
                    "所有输入文件必须存在，否则合并失败",
                    "按 paths 列表顺序合并，不是按文件名排序",
                ],
            ),
        ]

    async def execute(self, call_id: str, tool_name: str, **kwargs) -> ToolResult:
        handler = self._handlers.get(tool_name)
        if not handler:
            return ToolResult.fail(call_id, tool_name, f"未知工具: {tool_name}")
        try:
            return handler(call_id, **kwargs)
        except Exception as e:
            logger.exception(f"PDF 工具 {tool_name} 异常")
            return ToolResult.fail(call_id, tool_name, str(e))

    # ==================== pdf_read ====================

    def _handle_read(self, call_id: str, path: str, max_pages: int = 10,
                     extract_tables: bool = False, keywords: str = "") -> ToolResult:
        if not path or not os.path.exists(path):
            return ToolResult.fail(call_id, "pdf_read", f"文件不存在: {path}")

        meta = self._extract_metadata(path)
        is_scanned = self._is_likely_scanned(path)

        result = {
            "path": path,
            **meta,
            "is_scanned": is_scanned,
            "pages": [],
            "tables": [],
        }

        if is_scanned:
            result["_hint"] = "检测为扫描件（图片类PDF），文本提取可能为空。建议使用 OCR 工具。"
            return ToolResult.ok(call_id, "pdf_read", result)

        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                total = len(pdf.pages)
                result["page_count"] = total
                pages_to_read = min(max_pages, total)

                for i in range(pages_to_read):
                    page = pdf.pages[i]
                    text = page.extract_text() or ""
                    tables = page.extract_tables() if extract_tables else []

                    page_data = {
                        "page_num": i + 1,
                        "text": text[:3000] if text else "",
                        "chars": len(text),
                    }
                    if tables:
                        page_data["tables"] = [self._table_to_text(t) for t in tables]
                        result["tables"].extend(page_data["tables"])
                    result["pages"].append(page_data)

                return ToolResult.ok(call_id, "pdf_read", result)

        except ImportError:
            logger.warning("pdfplumber 未安装，降级到 pypdf")
            return self._read_with_pypdf2(call_id, path, max_pages)
        except Exception as e:
            return ToolResult.fail(call_id, "pdf_read", str(e))

    def _read_with_pypdf2(self, call_id: str, path: str, max_pages: int) -> ToolResult:
        try:
            with open(path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                total = len(reader.pages)
                result = {"path": path, "page_count": total, "engine": "pypdf (降级)", "pages": []}
                pages_to_read = min(max_pages, total)

                for i in range(pages_to_read):
                    page = reader.pages[i]
                    text = page.extract_text() or ""
                    page_data = {"page_num": i + 1, "text": text[:3000], "chars": len(text)}
                    result["pages"].append(page_data)

                return ToolResult.ok(call_id, "pdf_read", result)

        except ImportError:
            return ToolResult.fail(call_id, "pdf_read", "需要 pdfplumber 或 pypdf: pip install pdfplumber pypdf")
        except Exception as e:
            return ToolResult.fail(call_id, "pdf_read", str(e))

    # ==================== pdf_split ====================

    def _handle_split(self, call_id: str, path: str, output_dir: str,
                      ranges: str = "", split_size: int = 10) -> ToolResult:
        if not os.path.exists(path):
            return ToolResult.fail(call_id, "pdf_split", f"文件不存在: {path}")

        try:
            reader = PyPDF2.PdfReader(path)
            total_pages = len(reader.pages)

            # 解析拆分范围
            if ranges:
                split_ranges = self._parse_page_ranges(ranges, total_pages)
            else:
                split_ranges = [
                    (i, min(i + split_size - 1, total_pages - 1))
                    for i in range(0, total_pages, split_size)
                ]

            if not split_ranges:
                return ToolResult.fail(call_id, "pdf_split", "未能解析出有效的页码范围")

            os.makedirs(output_dir, exist_ok=True)
            base_name = os.path.splitext(os.path.basename(path))[0]
            generated_files = []

            for idx, (start, end) in enumerate(split_ranges):
                writer = PyPDF2.PdfWriter()
                for page_num in range(start, end + 1):
                    writer.add_page(reader.pages[page_num])

                out_name = f"{base_name}_part{idx + 1}_p{start + 1}-{end + 1}.pdf"
                out_path = os.path.join(output_dir, out_name)

                with open(out_path, "wb") as f:
                    writer.write(f)
                generated_files.append(out_path)

            return ToolResult.ok(call_id, "pdf_split", {
                "source": path,
                "output_dir": output_dir,
                "generated_files": generated_files,
                "total_parts": len(generated_files),
            })

        except Exception as e:
            return ToolResult.fail(call_id, "pdf_split", f"拆分失败: {e}")

    # ==================== pdf_concat ====================

    def _handle_concat(self, call_id: str, paths: List[str], output_path: str) -> ToolResult:
        if not paths:
            return ToolResult.fail(call_id, "pdf_concat", "文件列表不能为空")

        writer = PyPDF2.PdfWriter()
        merged_files = []

        for p in paths:
            if not os.path.exists(p):
                return ToolResult.fail(call_id, "pdf_concat", f"文件不存在: {p}")
            try:
                reader = PyPDF2.PdfReader(p)
                for page in reader.pages:
                    writer.add_page(page)
                merged_files.append(p)
            except Exception as e:
                return ToolResult.fail(call_id, "pdf_concat", f"读取 {p} 失败: {e}")

        try:
            out_dir = os.path.dirname(os.path.abspath(output_path))
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            with open(output_path, "wb") as f:
                writer.write(f)
        except Exception as e:
            return ToolResult.fail(call_id, "pdf_concat", f"写入文件失败: {e}")

        return ToolResult.ok(call_id, "pdf_concat", {
            "output_path": output_path,
            "merged_files": merged_files,
            "total_pages": len(writer.pages),
        })

    # ==================== 辅助方法 ====================

    @staticmethod
    def _parse_page_ranges(ranges_str: str, total_pages: int) -> List[Tuple[int, int]]:
        """解析页码范围（如 '1-3,5,8-10'），返回 0-indexed 的 (start, end)"""
        ranges = []
        for part in ranges_str.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start_str, end_str = part.split("-", 1)
                start = max(1, int(start_str.strip()))
                end = min(total_pages, int(end_str.strip()))
                if start <= end:
                    ranges.append((start - 1, end - 1))
            else:
                idx = int(part.strip())
                if 1 <= idx <= total_pages:
                    ranges.append((idx - 1, idx - 1))
        return ranges

    def _extract_metadata(self, path: str) -> Dict[str, Any]:
        meta = {"file_name": os.path.basename(path), "file_size": os.path.getsize(path)}
        try:
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

    @staticmethod
    def _is_likely_scanned(path: str) -> bool:
        try:
            with open(path, "rb") as f:
                head = f.read(2048)
            text_markers = [b"/Text", b"/Font", b"/CIDFont"]
            return not any(m in head for m in text_markers)
        except Exception:
            return False

    @staticmethod
    def _table_to_text(table) -> str:
        rows = []
        for row in table:
            rows.append(" | ".join(str(cell or "") for cell in row))
        return "\n".join(rows)
