"""
tools/excel_tool.py — Excel 操作工具（xlwings 版）

单工具多 action，所有操作通过 action 参数区分。

为什么用 xlwings 而不是 openpyxl：
  - 合并单元格天然支持（审计底稿必备）
  - 行插入不丢格式
  - 格式设置（字体/边框/数字格式）
  - 自动列宽
  - 保存时保留所有原始格式

依赖: xlwings + Excel（Windows）
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import xlwings as xw

from engine.tool.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

# ── 全局状态 ──
_open_apps: Dict[str, Any] = {}     # alias → xlwings App
_open_books: Dict[str, Any] = {}    # alias → xlwings Book
_file_aliases: Dict[str, str] = {}  # alias → real_path


def _safe_quit():
    """安全清理所有 Excel 进程"""
    for alias in list(_open_books.keys()):
        try:
            _open_books[alias].close()
        except Exception:
            pass
    for alias in list(_open_apps.keys()):
        try:
            _open_apps[alias].quit()
        except Exception:
            pass
    _open_apps.clear()
    _open_books.clear()
    _file_aliases.clear()


import atexit
atexit.register(_safe_quit)


class ExcelTool(BaseTool):
    """Excel 操作工具（xlwings 版）"""

    @property
    def is_read(self) -> bool:
        return False

    @property
    def is_write(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "excel"

    @property
    def description(self) -> str:
        return (
            "Excel 操作（xlwings），保留格式和合并单元格。\n"
            "标准流程: connect → read_sheet → [修改] → save → close\n"
            "操作:\n"
            "  connect — 打开文件（file_path，可选 alias）\n"
            "  list_sheets — 列出工作表名称和行列数\n"
            "  read_sheet — 读取工作表（分页、智能跳过空列）\n"
            "  get_sheet_info — 表头/数据类型/合并单元格检测\n"
            "  find_column — 按列名查找列索引\n"
            "  write_cell — 写入单个单元格\n"
            "  write_row — 写入一行\n"
            "  write_batch — 批量写入\n"
            "  write_dict — 按列名写入\n"
            "  insert_rows — 在指定行前插入行（自动复制上一行格式，支持一次多行）\n"
            "  format_range — 设置字体/边框/数字格式\n"
            "  auto_fit — 自动调整列宽\n"
            "  save — 保存\n"
            "  close — 关闭"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("action", "string", "操作类型", required=True,
                          enum=["connect", "list_sheets", "read_sheet", "get_sheet_info",
                                "find_column", "write_cell", "write_row", "write_batch",
                                "write_dict", "insert_rows", "format_range", "auto_fit",
                                "save", "close"]),
            ToolParameter("file_path", "string", "文件路径或别名", required=False),
            ToolParameter("alias", "string", "文件别名（connect时设置）", required=False),
            ToolParameter("sheet_name", "string", "工作表名", required=False),
            ToolParameter("header_row", "number", "表头行号", required=False),
            ToolParameter("start_row", "number", "起始行(read_sheet用)", required=False),
            ToolParameter("end_row", "number", "结束行(read_sheet用)", required=False),
            ToolParameter("row", "number", "行号", required=False),
            ToolParameter("column", "number", "列号", required=False),
            ToolParameter("columns", "string", "列号列表(JSON数组)", required=False),
            ToolParameter("values", "string", "值列表(JSON数组)", required=False),
            ToolParameter("value", "string", "值", required=False),
            ToolParameter("data", "string", "批量数据(JSON)", required=False),
            ToolParameter("row_data", "string", "行数据(JSON)", required=False),
            ToolParameter("count", "number", "插入行数", required=False),
            ToolParameter("format", "string", "格式配置(JSON)", required=False),
            ToolParameter("target", "string", "查找目标(列名)", required=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        action = kwargs.get("action", "read_sheet")
        handlers = {
            "connect": self._connect, "list_sheets": self._list_sheets,
            "read_sheet": self._read_sheet, "get_sheet_info": self._sheet_info,
            "find_column": self._find_column,
            "write_cell": self._write_cell, "write_row": self._write_row,
            "write_batch": self._write_batch, "write_dict": self._write_dict,
            "insert_rows": self._insert_rows, "format_range": self._format_range,
            "auto_fit": self._auto_fit, "save": self._save, "close": self._close,
        }
        handler = handlers.get(action)
        if not handler:
            return ToolResult.error(call_id, self.name, f"未知操作: {action}")
        try:
            return handler(call_id, kwargs)
        except Exception as e:
            logger.exception(f"Excel {action} 失败")
            return ToolResult.error(call_id, self.name, str(e))

    # ── connect ──

    def _connect(self, call_id, args):
        file_path = args.get("file_path", "")
        alias = args.get("alias", file_path)
        if not file_path:
            return ToolResult.error(call_id, self.name, "connect 需要 file_path")

        if not os.path.isabs(file_path):
            file_path = os.path.abspath(file_path)

        if not os.path.exists(file_path):
            # 新文件：创建
            app = xw.App(visible=False, add_book=True)
            wb = app.books.active
            wb.save(file_path)
        else:
            app = xw.App(visible=False)
            app.display_alerts = False
            wb = app.books.open(file_path)

        _file_aliases[alias] = file_path
        _open_apps[alias] = app
        _open_books[alias] = wb

        sheets = [s.name for s in wb.sheets]
        return ToolResult.success(call_id, self.name, {
            "file_path": file_path, "alias": alias,
            "sheets": sheets, "status": "已打开",
            "_hint": f"已打开 {file_path}，别名 '{alias}'。下一步: list_sheets 或 read_sheet"
        })

    # ── list_sheets ──

    def _list_sheets(self, call_id, args):
        wb = self._get_book(args)
        if not wb:
            return self._need_connect(call_id)
        sheets = []
        for s in wb.sheets:
            used = s.used_range
            sheets.append({
                "name": s.name,
                "rows": used.last_cell.row if used else 0,
                "cols": used.last_cell.column if used else 0,
            })
        return ToolResult.success(call_id, self.name, {
            "sheets": sheets,
            "_hint": "用 read_sheet 读取指定工作表"
        })

    # ── read_sheet ──

    def _read_sheet(self, call_id, args):
        wb = self._get_book(args)
        if not wb:
            return self._need_connect(call_id)

        sheet = self._get_sheet(wb, args)
        if not sheet:
            return ToolResult.error(call_id, self.name, "sheet_name 不存在")

        used = sheet.used_range
        total_rows = used.last_cell.row if used else 0
        total_cols = used.last_cell.column if used else 0

        start = max(1, int(args.get("start_row", 1)))
        end = min(total_rows, int(args.get("end_row", total_rows)))
        if end < start:
            end = min(start + 200, total_rows)

        header_row = int(args.get("header_row", 1))
        has_header = header_row >= 1

        # 读取数据
        raw = sheet.range((start, 1), (end, total_cols)).value
        if not isinstance(raw, list):
            raw = [[raw]]

        # 智能跳过空列
        valid_cols, col_names = [], {}
        for c in range(total_cols):
            col_data = [raw[r][c] for r in range(len(raw)) if r < len(raw) and c < len(raw[r])]
            if any(v is not None for v in col_data):
                valid_cols.append(c)
                if has_header and start <= header_row <= end:
                    hdr_val = None
                    for r in range(len(raw)):
                        if start + r == header_row and c < len(raw[r]):
                            hdr_val = raw[r][c]
                            break
                    col_names[c] = str(hdr_val) if hdr_val is not None else f"Col{c+1}"
                else:
                    col_names[c] = f"Col{c+1}"

        # 格式化输出
        rows = []
        for r_idx, row_data in enumerate(raw):
            cells = {}
            for c in valid_cols:
                if c < len(row_data):
                    cells[col_names[c]] = row_data[c]
            rows.append({"row": start + r_idx, "data": cells})

        return ToolResult.success(call_id, self.name, {
            "sheet": sheet.name, "total_rows": total_rows, "total_cols": total_cols,
            "start_row": start, "end_row": end, "header_row": header_row,
            "columns": col_names, "rows": rows,
            "_hint": f"读取 {sheet.name} 第 {start}-{end} 行（共 {total_rows} 行）。用 start_row/end_row 翻页"
        })

    # ── get_sheet_info ──

    def _sheet_info(self, call_id, args):
        wb = self._get_book(args)
        if not wb:
            return self._need_connect(call_id)
        sheet = self._get_sheet(wb, args)
        if not sheet:
            return ToolResult.error(call_id, self.name, "sheet_name 不存在")

        used = sheet.used_range
        total_rows = used.last_cell.row if used else 0
        total_cols = used.last_cell.column if used else 0

        # 表头（默认第一行）
        header_row = int(args.get("header_row", 1))
        headers = []
        for c in range(1, total_cols + 1):
            v = sheet.range((header_row, c)).value
            headers.append({"col": c, "name": str(v) if v is not None else f"Col{c}"})

        # 合并单元格检测
        merged = []
        try:
            for mg in sheet.used_range.api.MergeAreas:
                addr = mg.Address.replace("$", "")
                merged.append(addr)
        except Exception:
            pass

        return ToolResult.success(call_id, self.name, {
            "sheet": sheet.name, "total_rows": total_rows, "total_cols": total_cols,
            "header_row": header_row, "headers": headers,
            "merged_cells": merged[:20],
            "_hint": f"共有 {len(merged)} 个合并区域。用 find_column 查找特定列"
        })

    # ── find_column ──

    def _find_column(self, call_id, args):
        wb = self._get_book(args)
        if not wb:
            return self._need_connect(call_id)
        sheet = self._get_sheet(wb, args)
        target = args.get("target", "")
        if not target:
            return ToolResult.error(call_id, self.name, "需要 target（要查找的列名）")

        header_row = int(args.get("header_row", 1))
        used = sheet.used_range
        total_cols = used.last_cell.column if used else 0

        matches = []
        for c in range(1, total_cols + 1):
            v = sheet.range((header_row, c)).value
            if v is not None and target.lower() in str(v).lower():
                matches.append({"col": c, "name": str(v)})

        if not matches:
            return ToolResult.success(call_id, self.name, {
                "target": target, "found": False,
                "_hint": f"未找到包含 '{target}' 的列。用 get_sheet_info 查看所有列名"
            })
        return ToolResult.success(call_id, self.name, {
            "target": target, "found": True, "matches": matches
        })

    # ── write_cell ──

    def _write_cell(self, call_id, args):
        wb = self._get_book(args)
        if not wb:
            return self._need_connect(call_id)
        sheet = self._get_sheet(wb, args)
        row, col = int(args.get("row", 1)), int(args.get("column", 1))
        value = args.get("value", "")
        sheet.range((row, col)).value = value
        return ToolResult.success(call_id, self.name, {
            "sheet": sheet.name, "row": row, "col": col, "value": str(value)[:100],
            "_hint": "写入成功。记得 save + close"
        })

    # ── write_row ──

    def _write_row(self, call_id, args):
        wb = self._get_book(args)
        if not wb:
            return self._need_connect(call_id)
        sheet = self._get_sheet(wb, args)
        row = int(args.get("row", 1))
        columns = self._parse_json(args.get("columns", "[]"))
        values = self._parse_json(args.get("values", "[]"))
        for c, v in zip(columns, values):
            sheet.range((row, c)).value = v
        return ToolResult.success(call_id, self.name, {
            "sheet": sheet.name, "row": row, "written": len(columns)
        })

    # ── write_batch ──

    def _write_batch(self, call_id, args):
        wb = self._get_book(args)
        if not wb:
            return self._need_connect(call_id)
        sheet = self._get_sheet(wb, args)
        data = self._parse_json(args.get("data", "[]"))
        for entry in data:
            r = int(entry.get("row", 1))
            for col_str, val in entry.get("columns", {}).items():
                sheet.range((r, int(col_str))).value = val
        return ToolResult.success(call_id, self.name, {
            "sheet": sheet.name, "entries": len(data)
        })

    # ── write_dict ──

    def _write_dict(self, call_id, args):
        wb = self._get_book(args)
        if not wb:
            return self._need_connect(call_id)
        sheet = self._get_sheet(wb, args)
        row_data = self._parse_json(args.get("row_data", "{}"))
        row = int(row_data.get("row", 1))
        data = row_data.get("data", {})
        header_row = int(args.get("header_row", 1))

        # 建立表头映射
        used = sheet.used_range
        max_col = used.last_cell.column if used else 0
        col_map = {}
        for c in range(1, max_col + 1):
            v = sheet.range((header_row, c)).value
            if v is not None:
                col_map[str(v)] = c

        written = 0
        for col_name, val in data.items():
            if col_name in col_map:
                sheet.range((row, col_map[col_name])).value = val
                written += 1
            else:
                # 模糊匹配
                for hdr, ci in col_map.items():
                    if col_name.lower() in hdr.lower() or hdr.lower() in col_name.lower():
                        sheet.range((row, ci)).value = val
                        written += 1
                        break

        return ToolResult.success(call_id, self.name, {
            "sheet": sheet.name, "row": row, "written": written
        })

    # ── insert_rows ──

    def _insert_rows(self, call_id, args):
        """
        插入行并复制上一行格式（边框/字体/数字格式）。
        支持一次插入多行。
        """
        wb = self._get_book(args)
        if not wb:
            return self._need_connect(call_id)
        sheet = self._get_sheet(wb, args)
        start_row = int(args.get("start_row", args.get("row", 1)))
        count = int(args.get("count", 1))

        if start_row <= 1:
            start_row = 2  # 不能在第1行前插入

        # 先复制上一行的格式（在插入前复制，插入后粘贴）
        source_row = start_row - 1
        sheet.range(f"{source_row}:{source_row}").copy()

        # 插入空行
        sheet.range(f"{start_row}:{start_row + count - 1}").api.Insert()

        # 粘贴格式到新插入的行（xlPasteFormats = -4122）
        target = sheet.range(f"{start_row}:{start_row + count - 1}")
        target.api.PasteSpecial(Paste=-4122)

        # 清除粘贴后可能残留的选中状态
        try:
            wb.app.api.CutCopyMode = False
        except Exception:
            pass

        return ToolResult.success(call_id, self.name, {
            "sheet": sheet.name, "start_row": start_row, "count": count,
            "format_from": source_row,
            "_hint": f"已在第 {start_row} 行插入 {count} 行（已复制第 {source_row} 行格式），记得 save"
        })

    # ── format_range ──

    def _format_range(self, call_id, args):
        wb = self._get_book(args)
        if not wb:
            return self._need_connect(call_id)
        sheet = self._get_sheet(wb, args)
        rng_str = args.get("format", "{}")
        fmt = self._parse_json(rng_str)

        addr = fmt.get("range", "")
        if not addr:
            return ToolResult.error(call_id, self.name, "需要 range（如 'A1:C10'）")

        rng = sheet.range(addr)

        # 字体
        font = fmt.get("font", {})
        if font:
            if "bold" in font:
                rng.api.Font.Bold = font["bold"]
            if "size" in font:
                rng.api.Font.Size = font["size"]
            if "name" in font:
                rng.api.Font.Name = font["name"]
            if "color" in font:
                rng.api.Font.Color = self._rgb(font["color"])

        # 边框
        border = fmt.get("border", {})
        if border:
            for side in ["Left", "Right", "Top", "Bottom"]:
                if side.lower() in border:
                    b = border[side.lower()]
                    edge = getattr(rng.api.Borders, getattr(
                        getattr(rng.api.Borders, f"xlEdge{side}"), "xlEdge" + side, None
                    ), None)

        # 数字格式
        num_fmt = fmt.get("number_format", "")
        if num_fmt:
            rng.api.NumberFormat = num_fmt

        # 对齐
        align = fmt.get("alignment", {})
        if align:
            if "horizontal" in align:
                h_map = {"left": -4131, "center": -4108, "right": -4152}
                rng.api.HorizontalAlignment = h_map.get(align["horizontal"], -4108)
            if "vertical" in align:
                v_map = {"top": -4160, "center": -4108, "bottom": -4107}
                rng.api.VerticalAlignment = v_map.get(align["vertical"], -4108)

        return ToolResult.success(call_id, self.name, {
            "range": addr, "sheet": sheet.name,
            "_hint": f"已设置 {addr} 格式"
        })

    @staticmethod
    def _rgb(color):
        """颜色转 RGB"""
        if isinstance(color, int):
            return color
        if isinstance(color, str) and color.startswith("#"):
            r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
            return r + (g << 8) + (b << 16)
        return 0

    # ── auto_fit ──

    def _auto_fit(self, call_id, args):
        wb = self._get_book(args)
        if not wb:
            return self._need_connect(call_id)
        sheet = self._get_sheet(wb, args)
        sheet.used_range.autofit()
        return ToolResult.success(call_id, self.name, {
            "sheet": sheet.name, "status": "已自动调整列宽"
        })

    # ── save ──

    def _save(self, call_id, args):
        wb = self._get_book(args)
        if not wb:
            return self._need_connect(call_id)
        wb.save()
        return ToolResult.success(call_id, self.name, {
            "status": "已保存", "file_path": wb.fullname
        })

    # ── close ──

    def _close(self, call_id, args):
        alias = args.get("file_path", "")
        if alias not in _open_books:
            return ToolResult.error(call_id, self.name, "文件未打开")

        wb = _open_books.pop(alias, None)
        app = _open_apps.pop(alias, None)
        _file_aliases.pop(alias, None)

        if wb:
            try:
                wb.save()
            except Exception:
                pass
            try:
                wb.close()
            except Exception:
                pass
        if app:
            try:
                app.quit()
            except Exception:
                pass

        return ToolResult.success(call_id, self.name, {
            "alias": alias, "status": "已关闭"
        })

    # ── helpers ──

    def _get_book(self, args):
        alias = args.get("file_path", "")
        return _open_books.get(alias)

    def _get_sheet(self, wb, args):
        name = args.get("sheet_name", "")
        if name:
            try:
                return wb.sheets[name]
            except Exception:
                pass
        # 默认第一个工作表
        return wb.sheets[0] if wb.sheets else None

    def _need_connect(self, call_id):
        return ToolResult.error(call_id, self.name,
                                "文件未打开。请先 excel(action='connect', file_path='xxx.xlsx', alias='x')")

    @staticmethod
    def _parse_json(s):
        try:
            return json.loads(s) if isinstance(s, str) else s
        except (json.JSONDecodeError, TypeError):
            return {}
