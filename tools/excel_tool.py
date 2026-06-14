"""
Excel 工具（原子工具版）

原子工具:
  excel_open             — 打开 Excel 文件
  excel_close            — 关闭 Excel 文件
  excel_list_sheets      — 列出工作表
  excel_read_sheet       — 读取工作表数据
  excel_get_structure    — 获取工作表结构
  excel_write_cell       — 写入单元格
  excel_write_by_header  — 按列名写入
  excel_insert_rows      — 插入行
  excel_format_range     — 设置单元格格式
  excel_save             — 保存文件

依赖: xlwings + Excel（Windows）
"""

import json
import os
import logging
from typing import Any, Dict, List

import xlwings as xw

from engine.tool.base import (
    BaseTool, ToolDefinition, ToolParameter, ToolResult,
    CATEGORY_DATA,
)

logger = logging.getLogger(__name__)

# ── 全局状态 ──
_open_apps: Dict[str, Any] = {}
_open_books: Dict[str, Any] = {}
_ref_map: Dict[str, str] = {}


def _safe_quit():
    for ref in list(_open_books.keys()):
        try:
            _open_books[ref].close()
        except Exception:
            pass
    for ref in list(_open_apps.keys()):
        try:
            _open_apps[ref].quit()
        except Exception:
            pass
    _open_apps.clear()
    _open_books.clear()
    _ref_map.clear()


import atexit
atexit.register(_safe_quit)


class ExcelTool(BaseTool):
    """Excel 操作工具集（基于 xlwings）"""

    def __init__(self):
        self._handlers = {
            "excel_open": self._handle_open,
            "excel_close": self._handle_close,
            "excel_list_sheets": self._handle_list_sheets,
            "excel_read_sheet": self._handle_read_sheet,
            "excel_get_structure": self._handle_get_structure,
            "excel_write_cell": self._handle_write_cell,
            "excel_write_by_header": self._handle_write_by_header,
            "excel_insert_rows": self._handle_insert_rows,
            "excel_format_range": self._handle_format_range,
            "excel_save": self._handle_save,
        }
        for t in self.tools:
            t.handler = self._handlers.get(t.name)

    @property
    def name(self) -> str:
        return "excel"

    @property
    def category(self) -> str:
        return CATEGORY_DATA

    @property
    def tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="excel_open",
                description="""打开 Excel 文件，获得一个引用标识（ref）。

Excel 工具的初始步骤！必须先调用此工具获得 ref，后续操作都通过 ref 引用文件。

使用场景：
- 打开已有 .xlsx/.xls 文件进行读取或编辑
- 创建一个新的 Excel 文件（如果文件不存在会自动创建）

工作流程：
excel_open → excel_list_sheets → excel_read_sheet/write → excel_save → excel_close""",
                parameters=[
                    ToolParameter("file_path", "string", "Excel 文件路径（支持相对和绝对路径）", required=True),
                    ToolParameter("ref", "string", "文件引用标识，后续操作都通过这个 ref 引用。默认用文件名（不含扩展名）", required=False),
                ],
                examples=[
                    'excel_open(file_path="data.xlsx")',
                    'excel_open(file_path="report.xlsx", ref="rpt")',
                ],
                constraints=[
                    "必须先调用此工具获得 ref，后续所有操作都基于 ref",
                    "操作完毕后必须调用 excel_save + excel_close 释放 Excel 进程资源",
                    "同一个 ref 只能打开一个文件，重复打开会覆盖",
                ],
            ),
            ToolDefinition(
                name="excel_close",
                description="""关闭 Excel 文件，释放系统资源。
关闭前会自动保存文件。操作完毕后务必调用此工具，否则 Excel 进程会残留在后台。""",
                parameters=[ToolParameter("ref", "string", "文件引用标识（与 excel_open 时使用的 ref 一致）", required=True)],
                examples=['excel_close(ref="data")'],
                constraints=[
                    "关闭前会自动保存（无需先调 excel_save）",
                    "请务必在操作完成后调用，否则 Excel 进程不会退出",
                ],
            ),
            ToolDefinition(
                name="excel_list_sheets",
                description="""列出 Excel 文件的所有工作表名称及其行列数。
打开文件后的第一步，先了解文件结构再进行后续操作。""",
                parameters=[ToolParameter("ref", "string", "文件引用标识", required=True)],
                examples=['excel_list_sheets(ref="data")'],
                is_read=True,
            ),
            ToolDefinition(
                name="excel_read_sheet",
                description="""读取工作表的数据内容。支持分页读取，自动跳过空列。

使用场景：
- 读取工作表的所有数据
- 只读取某几行的数据（通过 start_row/end_row 参数）
- 了解数据的列名和内容""",
                parameters=[
                    ToolParameter("ref", "string", "文件引用标识", required=True),
                    ToolParameter("sheet_name", "string", "工作表名称（如 'Sheet1'，通过 excel_list_sheets 获取）", required=True),
                    ToolParameter("start_row", "number", "起始行号（从 1 开始），默认 1", required=False),
                    ToolParameter("end_row", "number", "结束行号，默认到工作表末尾", required=False),
                    ToolParameter("header_row", "number", "表头所在行号，默认 1。用于生成列名", required=False),
                ],
                examples=[
                    'excel_read_sheet(ref="data", sheet_name="Sheet1", start_row=1, end_row=50)',
                    'excel_read_sheet(ref="rpt", sheet_name="员工表", header_row=2)  # 表头在第2行',
                ],
                is_read=True,
                constraints=[
                    "如果工作表名包含中文或空格，确保 sheet_name 完全匹配",
                    "大数据量时建议分页读取，不要一次读取全部行",
                    "header_row 用于确定列名，默认第 1 行为表头",
                ],
            ),
            ToolDefinition(
                name="excel_get_structure",
                description="""获取工作表的列名和合并单元格信息（轻量版，不含数据）。
比 excel_read_sheet 更快，只返回结构不返回数据。

使用场景：
- 快速了解工作表有哪些列
- 查看合并单元格区域
- 在写入前确认表头名称""",
                parameters=[
                    ToolParameter("ref", "string", "文件引用标识", required=True),
                    ToolParameter("sheet_name", "string", "工作表名称", required=True),
                    ToolParameter("header_row", "number", "表头所在行，默认 1", required=False),
                ],
                is_read=True,
                examples=[
                    'excel_get_structure(ref="data", sheet_name="Sheet1")',
                ],
            ),
            ToolDefinition(
                name="excel_write_cell",
                description="""写入单个单元格的值。
按行列号定位，适用于精确的单元格写入。

使用场景：
- 修改某个单元格的值
- 设置标题或汇总行

如果是按列名写入整行数据，建议用 excel_write_by_header（更直观）。""",
                parameters=[
                    ToolParameter("ref", "string", "文件引用标识", required=True),
                    ToolParameter("sheet_name", "string", "工作表名称", required=True),
                    ToolParameter("row", "number", "行号（从 1 开始）", required=True),
                    ToolParameter("column", "number", "列号（从 1 开始，A=1, B=2, ...）", required=True),
                    ToolParameter("value", "string", "要写入的值（数字或文本）", required=True),
                ],
                examples=[
                    'excel_write_cell(ref="data", sheet_name="Sheet1", row=2, column=3, value="已完成")',
                ],
                constraints=[
                    "row/column 都从 1 开始计数（A1=row=1,column=1）",
                    "写入后需调用 excel_save 保存",
                ],
            ),
            ToolDefinition(
                name="excel_write_by_header",
                description="""按列名写入一行数据到指定行。
自动根据表头名称匹配列位置，支持模糊匹配。

使用场景：
- 按列名填充一行数据（最常用的写入方式）
- 更新已有行的指定列值
- data 字典中未匹配到的列会被忽略（不会报错）""",
                parameters=[
                    ToolParameter("ref", "string", "文件引用标识", required=True),
                    ToolParameter("sheet_name", "string", "工作表名称", required=True),
                    ToolParameter("row", "number", "写入的目标行号", required=True),
                    ToolParameter("data", "object", "列名→值的字典，如 {'姓名': '张三', '年龄': 25, '状态': '已完成'}", required=True),
                    ToolParameter("header_row", "number", "表头所在行，默认 1", required=False),
                ],
                examples=[
                    'excel_write_by_header(ref="data", sheet_name="Sheet1", row=5, data={"姓名": "张三", "年龄": 25})',
                ],
                constraints=[
                    "先调用 excel_list_sheets 或 excel_get_structure 确认列名",
                    "data 字典的 key 需与表头名称匹配（支持模糊匹配）",
                    "写入后需调用 excel_save 保存",
                ],
            ),
            ToolDefinition(
                name="excel_insert_rows",
                description="""在指定行前插入一行或多行，自动复制上一行的格式（边框、字体、数字格式）。

使用场景：
- 在表格中间插入新数据行
- 批量插入多行

注意：不会复制公式，只复制格式。""",
                parameters=[
                    ToolParameter("ref", "string", "文件引用标识", required=True),
                    ToolParameter("sheet_name", "string", "工作表名称", required=True),
                    ToolParameter("start_row", "number", "从第几行之前开始插入（如 start_row=3 表示在第3行前插入）", required=True),
                    ToolParameter("count", "number", "插入行数，默认 1", required=False),
                ],
                examples=[
                    'excel_insert_rows(ref="data", sheet_name="Sheet1", start_row=3, count=2)  # 在第3行前插入2行',
                ],
                constraints=[
                    "不能在第 1 行前插入（会报错）",
                    "插入后记得调用 excel_save 保存",
                    "自动复制上一行的格式，但不复制公式",
                ],
            ),
            ToolDefinition(
                name="excel_format_range",
                description="""设置单元格区域的格式：字体加粗/大小/颜色、数字格式、对齐方式。

使用场景：
- 设置表头字体加粗
- 设置数字格式（如金额保留两位小数）
- 设置单元格对齐方式（居中/左对齐/右对齐）""",
                parameters=[
                    ToolParameter("ref", "string", "文件引用标识", required=True),
                    ToolParameter("sheet_name", "string", "工作表名称", required=True),
                    ToolParameter("range", "string", "单元格范围，如 'A1:C10' 或 'B2:B100'", required=True),
                    ToolParameter("font", "object", "字体设置字典：{bold: true, size: 12, color: '#FF0000', name: '微软雅黑'}", required=False),
                    ToolParameter("number_format", "string", "数字格式代码，如 '#,##0.00' 或 '0%'", required=False),
                    ToolParameter("alignment", "object", "对齐设置：{horizontal: 'center'|'left'|'right', vertical: 'center'|'top'|'bottom'}", required=False),
                ],
                examples=[
                    'excel_format_range(ref="data", sheet_name="Sheet1", range="A1:C1", font={"bold": true})',
                    'excel_format_range(ref="rpt", sheet_name="销售表", range="D2:D100", number_format="#,##0.00")',
                ],
                constraints=[
                    "font.color 支持 #RRGGBB 格式（如 '#FF0000' 为红色）",
                    "对齐方式的 horizontal: center/left/right, vertical: center/top/bottom",
                ],
            ),
            ToolDefinition(
                name="excel_save",
                description="保存 Excel 文件的更改到磁盘。写入/修改/插入/格式化后需要调用来保存。",
                parameters=[ToolParameter("ref", "string", "文件引用标识", required=True)],
                examples=['excel_save(ref="data")'],
                constraints=[
                    "修改后一定要调用保存，否则更改会丢失",
                    "excel_close 会自动保存，但建议操作过程中手动保存",
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
            logger.exception(f"Excel {tool_name} 失败")
            return ToolResult.fail(call_id, tool_name, str(e))

    def _handle_open(self, call_id: str, file_path: str, ref: str = None) -> ToolResult:
        if ref is None:
            ref = os.path.basename(file_path)

        if not os.path.isabs(file_path):
            file_path = os.path.abspath(file_path)

        app = xw.App(visible=False)
        app.display_alerts = False

        if os.path.exists(file_path):
            wb = app.books.open(file_path)
            status = "已打开"
        else:
            wb = app.books.add()
            wb.save(file_path)
            status = "已创建"

        _ref_map[ref] = file_path
        _open_apps[ref] = app
        _open_books[ref] = wb

        sheets = [s.name for s in wb.sheets]
        return ToolResult.ok(call_id, "excel_open", {
            "ref": ref, "file_path": file_path, "status": status, "sheets": sheets,
        })

    def _handle_close(self, call_id: str, ref: str) -> ToolResult:
        if ref not in _open_books:
            return ToolResult.fail(call_id, "excel_close", f"未找到打开的文件: {ref}")
        wb = _open_books.pop(ref)
        app = _open_apps.pop(ref)
        _ref_map.pop(ref, None)
        try:
            wb.save()
            wb.close()
        except Exception:
            pass
        try:
            app.quit()
        except Exception:
            pass
        return ToolResult.ok(call_id, "excel_close", {"ref": ref, "status": "已关闭"})

    def _handle_list_sheets(self, call_id: str, ref: str) -> ToolResult:
        wb = self._book(ref)
        if not wb:
            return self._not_found(call_id, "excel_list_sheets", ref)
        sheets = []
        for s in wb.sheets:
            used = s.used_range
            sheets.append({
                "name": s.name,
                "rows": used.last_cell.row if used else 0,
                "cols": used.last_cell.column if used else 0,
            })
        return ToolResult.ok(call_id, "excel_list_sheets", {"sheets": sheets})

    def _handle_read_sheet(self, call_id: str, ref: str, sheet_name: str,
                           start_row: int = 1, end_row: int = None,
                           header_row: int = 1) -> ToolResult:
        wb = self._book(ref)
        if not wb:
            return self._not_found(call_id, "excel_read_sheet", ref)
        sheet = self._sheet(wb, sheet_name)
        if not sheet:
            return ToolResult.fail(call_id, "excel_read_sheet", f"工作表不存在: {sheet_name}")

        used = sheet.used_range
        total_rows = used.last_cell.row if used else 0
        total_cols = used.last_cell.column if used else 0
        if end_row is None:
            end_row = total_rows
        end_row = min(end_row, total_rows)

        raw = sheet.range((start_row, 1), (end_row, total_cols)).value
        if not isinstance(raw, list):
            raw = [[raw]]

        valid_cols = []
        col_names = {}
        for c in range(total_cols):
            col_data = [raw[r][c] for r in range(len(raw)) if r < len(raw) and c < len(raw[r])]
            if any(v is not None for v in col_data):
                valid_cols.append(c)
                if start_row <= header_row <= end_row:
                    hv = None
                    for r in range(len(raw)):
                        if start_row + r == header_row:
                            hv = raw[r][c]
                            break
                    col_names[c] = str(hv) if hv is not None else f"Col{c+1}"
                else:
                    col_names[c] = f"Col{c+1}"

        rows = []
        for ri, rd in enumerate(raw):
            cells = {}
            for c in valid_cols:
                cells[col_names[c]] = rd[c] if c < len(rd) else None
            rows.append({"row": start_row + ri, **cells})

        return ToolResult.ok(call_id, "excel_read_sheet", {
            "sheet": sheet_name, "total_rows": total_rows, "total_cols": total_cols,
            "range": f"{start_row}-{end_row}", "columns": list(col_names.values()), "rows": rows,
        })

    def _handle_get_structure(self, call_id: str, ref: str, sheet_name: str,
                              header_row: int = 1) -> ToolResult:
        wb = self._book(ref)
        if not wb:
            return self._not_found(call_id, "excel_get_structure", ref)
        sheet = self._sheet(wb, sheet_name)
        if not sheet:
            return ToolResult.fail(call_id, "excel_get_structure", f"工作表不存在: {sheet_name}")

        used = sheet.used_range
        total_rows = used.last_cell.row if used else 0
        total_cols = used.last_cell.column if used else 0

        headers = []
        for c in range(1, total_cols + 1):
            v = sheet.range((header_row, c)).value
            headers.append({"col": c, "name": str(v) if v is not None else f"Col{c}"})

        merged = []
        try:
            for mg in sheet.used_range.api.MergeAreas:
                merged.append(mg.Address.replace("$", ""))
        except Exception:
            pass

        return ToolResult.ok(call_id, "excel_get_structure", {
            "sheet": sheet_name, "total_rows": total_rows, "total_cols": total_cols,
            "headers": headers, "merged_cells": merged[:20],
        })

    def _handle_write_cell(self, call_id: str, ref: str, sheet_name: str,
                           row: int, column: int, value: str) -> ToolResult:
        wb = self._book(ref)
        if not wb:
            return self._not_found(call_id, "excel_write_cell", ref)
        sheet = self._sheet(wb, sheet_name)
        if not sheet:
            return ToolResult.fail(call_id, "excel_write_cell", f"工作表不存在: {sheet_name}")
        sheet.range((row, column)).value = value
        return ToolResult.ok(call_id, "excel_write_cell", {
            "sheet": sheet_name, "row": row, "col": column, "value": str(value)[:100],
        })

    def _handle_write_by_header(self, call_id: str, ref: str, sheet_name: str,
                                row: int, data: dict, header_row: int = 1) -> ToolResult:
        wb = self._book(ref)
        if not wb:
            return self._not_found(call_id, "excel_write_by_header", ref)
        sheet = self._sheet(wb, sheet_name)
        if not sheet:
            return ToolResult.fail(call_id, "excel_write_by_header", f"工作表不存在: {sheet_name}")

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
                for hdr, ci in col_map.items():
                    if col_name.lower() in hdr.lower() or hdr.lower() in col_name.lower():
                        sheet.range((row, ci)).value = val
                        written += 1
                        break

        return ToolResult.ok(call_id, "excel_write_by_header", {
            "sheet": sheet_name, "row": row, "written": written,
        })

    def _handle_insert_rows(self, call_id: str, ref: str, sheet_name: str,
                            start_row: int, count: int = 1) -> ToolResult:
        wb = self._book(ref)
        if not wb:
            return self._not_found(call_id, "excel_insert_rows", ref)
        sheet = self._sheet(wb, sheet_name)
        if not sheet:
            return ToolResult.fail(call_id, "excel_insert_rows", f"工作表不存在: {sheet_name}")

        if start_row <= 1:
            start_row = 2

        source_row = start_row - 1
        sheet.range(f"{source_row}:{source_row}").copy()
        sheet.range(f"{start_row}:{start_row + count - 1}").api.Insert()
        target = sheet.range(f"{start_row}:{start_row + count - 1}")
        target.api.PasteSpecial(Paste=-4122)
        try:
            wb.app.api.CutCopyMode = False
        except Exception:
            pass

        return ToolResult.ok(call_id, "excel_insert_rows", {
            "sheet": sheet_name, "start_row": start_row, "count": count,
            "format_from": source_row,
        })

    def _handle_format_range(self, call_id: str, ref: str, sheet_name: str,
                             range: str, font: dict = None,
                             number_format: str = None, alignment: dict = None) -> ToolResult:
        wb = self._book(ref)
        if not wb:
            return self._not_found(call_id, "excel_format_range", ref)
        sheet = self._sheet(wb, sheet_name)
        if not sheet:
            return ToolResult.fail(call_id, "excel_format_range", f"工作表不存在: {sheet_name}")

        rng = sheet.range(range)

        if font:
            if "bold" in font:
                rng.api.Font.Bold = font["bold"]
            if "size" in font:
                rng.api.Font.Size = font["size"]
            if "name" in font:
                rng.api.Font.Name = font["name"]
            if "color" in font:
                rng.api.Font.Color = self._rgb(font["color"])
        if number_format:
            rng.api.NumberFormat = number_format
        if alignment:
            h_map = {"left": -4131, "center": -4108, "right": -4152}
            v_map = {"top": -4160, "center": -4108, "bottom": -4107}
            if "horizontal" in alignment:
                rng.api.HorizontalAlignment = h_map.get(alignment["horizontal"], -4108)
            if "vertical" in alignment:
                rng.api.VerticalAlignment = v_map.get(alignment["vertical"], -4108)

        return ToolResult.ok(call_id, "excel_format_range", {
            "range": range, "sheet": sheet_name,
        })

    def _handle_save(self, call_id: str, ref: str) -> ToolResult:
        wb = self._book(ref)
        if not wb:
            return self._not_found(call_id, "excel_save", ref)
        wb.save()
        return ToolResult.ok(call_id, "excel_save", {"status": "已保存", "file_path": wb.fullname})

    @staticmethod
    def _rgb(color):
        if isinstance(color, int):
            return color
        if isinstance(color, str) and color.startswith("#"):
            r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
            return r + (g << 8) + (b << 16)
        return 0

    @staticmethod
    def _book(ref: str):
        return _open_books.get(ref)

    @staticmethod
    def _sheet(wb, name: str):
        try:
            return wb.sheets[name]
        except Exception:
            return None

    @staticmethod
    def _not_found(call_id, tool_name, ref):
        return ToolResult.fail(call_id, tool_name, f"文件未打开: {ref}。请先用 excel_open 打开文件")
