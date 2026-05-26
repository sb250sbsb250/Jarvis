"""
Excel 工具 — 26合1：通过 openpyxl 操作 .xlsx 文件

所有操作通过一个 tool 的 action 参数区分，LLM 只需记忆一个工具名。
"""

import os
import json
import logging
from typing import List, Optional, Any, Dict
from dataclasses import dataclass, field

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.excel")

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter, column_index_from_string
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.formatting.rule import CellIsRule, FormulaRule
    HAVE_OPENPYXL = True
except ImportError:
    HAVE_OPENPYXL = False

# ── 颜色映射 ──
_COLOR_MAP = {
    "red": "FF0000", "green": "00FF00", "blue": "0000FF",
    "white": "FFFFFF", "black": "000000", "gray": "808080",
    "lightgray": "D3D3D3", "darkgray": "A9A9A9",
    "yellow": "FFFF00", "orange": "FFA500", "purple": "800080",
    "pink": "FFC0CB", "brown": "A52A2A", "cyan": "00FFFF",
    "magenta": "FF00FF", "navy": "000080", "teal": "008080",
    "maroon": "800000", "olive": "808000", "lime": "00FF00",
    "indigo": "4B0082", "gold": "FFD700", "silver": "C0C0C0",
    "coral": "FF7F50", "salmon": "FA8072", "tomato": "FF6347",
    "wheat": "F5DEB3", "khaki": "F0E68C", "plum": "DDA0DD",
    "orchid": "DA70D6", "violet": "EE82EE", "azure": "F0FFFF",
    "mint": "98FB98", "honeydew": "F0FFF0", "ivory": "FFFFF0",
    "bisque": "FFE4C4", "linen": "FAF0E6", "snow": "FFFAFA",
}

def _parse_color(color: str) -> str:
    """统一解析颜色值为 6 位 RGB 十六进制"""
    if not color:
        return "000000"
    color = color.strip().lower()
    if color in _COLOR_MAP:
        return _COLOR_MAP[color]
    if color.startswith("#"):
        color = color[1:]
    if len(color) == 3:
        color = "".join(c * 2 for c in color)
    if len(color) == 6:
        return color.upper()
    return "000000"


# ── 会话级状态缓存 ──
# key: call_id 的前 8 位（会话标识），value: 最近操作的 file_path
_SESSION_LAST_PATH: Dict[str, str] = {}


def _get_session_path(call_id: str) -> str:
    """从会话缓存中恢复 file_path"""
    if not call_id:
        return ""
    return _SESSION_LAST_PATH.get(call_id[:8], "")


def _set_session_path(call_id: str, file_path: str):
    """保存 file_path 到会话缓存"""
    if call_id and file_path:
        _SESSION_LAST_PATH[call_id[:8]] = file_path


class ExcelTool(BaseTool):
    """Excel 全能操作工具（26合1）"""

    _last_path: Optional[str] = None

    def __init__(self, **kwargs):
        pass

    @property
    def name(self) -> str:
        return "excel"

    @property
    def description(self) -> str:
        return (
            "Excel 操作工具（基于 openpyxl，操作 .xlsx 文件，无需 Excel 安装）。\n"
            "\n"
            "📌 关键规则：第一次操作新文件时，传 file_path 参数会自动创建文件。\n"
            "   之后同一会话中操作同一个文件，可以省略 file_path，工具会记住。\n"
            "\n"
            "典型流程：\n"
            "  1. create_sheet(file_path=\"报表.xlsx\", sheet_name=\"数据\")  ← 自动创建文件\n"
            "  2. write_table(sheet_name=\"数据\", headers=[...], data=[...])  ← 省略 file_path\n"
            "  3. save()  ← 保存\n"
            "\n"
            "action 可选：\n"
            "  - status: 查询文件状态\n"
            "  - read: 读取单元格/区域/整个工作表\n"
            "  - write: 写入单元格\n"
            "  - write_cells: 批量写入区域\n"
            "  - write_table: 写入完整表格（含表头格式）—— 推荐，一次完成数据+格式\n"
            "  - analyze: 分析工作表结构（列名、类型统计）\n"
            "  - create_sheet: 创建新工作表（新文件时传 file_path 自动创建文件）\n"
            "  - rename_sheet: 重命名工作表\n"
            "  - switch_sheet: 切换活动工作表\n"
            "  - delete_sheet: 删除工作表\n"
            "  - list_sheets: 列出所有工作表\n"
            "  - set_font: 设置字体\n"
            "  - set_column_width: 设置列宽\n"
            "  - set_row_height: 设置行高\n"
            "  - merge_cells: 合并单元格\n"
            "  - set_background: 设置单元格/区域背景色\n"
            "  - set_column_background: 设置整列背景色\n"
            "  - set_conditional_format: 设置条件格式\n"
            "  - insert_rows: 插入行\n"
            "  - save: 保存文件\n"
            "  - save_as_pdf: 保存为 PDF（打印输出）\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="action", type="string", required=True,
                          description="操作类型",
                          enum=[
                              "status", "open", "close",
                              "read", "write", "write_cells", "write_table",
                              "analyze",
                              "create_sheet", "rename_sheet", "switch_sheet",
                              "delete_sheet", "list_sheets",
                              "set_font", "set_column_width", "set_row_height",
                              "merge_cells", "set_background",
                              "set_column_background",
                              "set_conditional_format",
                              "save", "save_as_pdf", "insert_rows",
                          ]),
            ToolParameter(name="file_path", type="string", required=False,
                          description=".xlsx 文件路径"),
            ToolParameter(name="sheet_name", type="string", required=False,
                          description="工作表名称"),
            ToolParameter(name="cell", type="string", required=False,
                          description="单元格地址，如 A1"),
            ToolParameter(name="range", type="string", required=False,
                          description="区域，如 A1:C10"),
            ToolParameter(name="value", type="string", required=False,
                          description="写入的值"),
            ToolParameter(name="values", type="string", required=False,
                          description="批量值 JSON 二维数组（write_cells）"),
            ToolParameter(name="headers", type="string", required=False,
                          description="表头 JSON 数组（write_table）"),
            ToolParameter(name="data", type="string", required=False,
                          description="数据体 JSON 二维数组（write_table）"),
            ToolParameter(name="header_bold", type="boolean", required=False,
                          description="表头加粗", default=True),
            ToolParameter(name="header_bg", type="string", required=False,
                          description="表头背景色，如 'blue' / '#4472C4'"),
            ToolParameter(name="auto_width", type="boolean", required=False,
                          description="自动列宽", default=True),
            ToolParameter(name="color", type="string", required=False,
                          description="字体颜色"),
            ToolParameter(name="bold", type="boolean", required=False,
                          description="粗体"),
            ToolParameter(name="size", type="number", required=False,
                          description="字号"),
            ToolParameter(name="bg_color", type="string", required=False,
                          description="背景色（set_background）"),
            ToolParameter(name="width", type="number", required=False,
                          description="列宽"),
            ToolParameter(name="height", type="number", required=False,
                          description="行高"),
            ToolParameter(name="condition", type="string", required=False,
                          description="条件表达式（条件格式）"),
            ToolParameter(name="cond_type", type="string", required=False,
                          description="条件格式类型: cellIs/formula",
                          enum=["cellIs", "formula"]),
            ToolParameter(name="cond_value", type="string", required=False,
                          description="条件格式对比值"),
            ToolParameter(name="cond_fill", type="string", required=False,
                          description="条件格式满足时的填充色"),
            ToolParameter(name="new_name", type="string", required=False,
                          description="新工作表名（rename_sheet）"),
            ToolParameter(name="row_count", type="number", required=False,
                          description="插入行数（insert_rows）"),
            ToolParameter(name="start_row", type="number", required=False,
                          description="起始行号（insert_rows/set_background）"),
            ToolParameter(name="start_col", type="number", required=False,
                          description="起始列号（write_table）"),
            ToolParameter(name="encoding", type="string", required=False,
                          description="编码", default="utf-8"),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        if not HAVE_OPENPYXL:
            return ToolResult.error(
                call_id, self.name,
                "缺少 openpyxl 库。请运行: pip install openpyxl"
            )

        action = kwargs.get("action", "status")
        file_path = kwargs.get("file_path", "")

        # 会话状态：如果没传 file_path，从缓存恢复
        if not file_path and call_id:
            cached = _get_session_path(call_id)
            if cached:
                kwargs["file_path"] = cached
                file_path = cached

        # 会话状态：传了 file_path 就缓存下来
        if file_path and call_id:
            _set_session_path(call_id, file_path)

        handlers = {
            "status": self._status,
            "open": self._open,
            "close": self._close,
            "read": self._read,
            "write": self._write,
            "write_cells": self._write_cells,
            "write_table": self._write_table,
            "analyze": self._analyze,
            "create_sheet": self._create_sheet,
            "rename_sheet": self._rename_sheet,
            "switch_sheet": self._switch_sheet,
            "delete_sheet": self._delete_sheet,
            "list_sheets": self._list_sheets,
            "set_font": self._set_font,
            "set_column_width": self._set_column_width,
            "set_row_height": self._set_row_height,
            "merge_cells": self._merge_cells,
            "set_background": self._set_background,
            "set_column_background": self._set_column_background,
            "set_conditional_format": self._set_conditional_format,
            "save": self._save,
            "save_as_pdf": self._save_as_pdf,
            "insert_rows": self._insert_rows,
        }

        handler = handlers.get(action)
        if not handler:
            return ToolResult.error(call_id, self.name,
                                    f"未知操作: {action}")

        try:
            result = handler(kwargs)
            if isinstance(result, dict) and result.get("success"):
                return ToolResult.success(call_id, self.name, result)
            err = result.get("error", "操作失败") if isinstance(result, dict) else str(result)
            return ToolResult.error(call_id, self.name, err)
        except Exception as e:
            logger.exception(f"[excel.{action}] 异常")
            return ToolResult.error(call_id, self.name, str(e))

    # ═══════════════════════════════════════════════════════
    # 私有：文件 / 工作表辅助
    # ═══════════════════════════════════════════════════════

    def _ensure_wb(self, file_path: str) -> tuple:
        """
        获取或创建工作簿。
        如果文件存在则打开，不存在则自动创建新工作簿并保存。
        返回 (wb, error)
        """
        if not file_path:
            return None, "请提供 file_path 参数"
        if os.path.exists(file_path):
            try:
                wb = openpyxl.load_workbook(file_path)
                return wb, None
            except Exception as e:
                return None, f"打开文件失败: {e}"
        # 文件不存在 → 自动创建
        try:
            os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
            wb = openpyxl.Workbook()
            wb.save(file_path)
            return wb, None
        except Exception as e:
            return None, f"创建文件失败: {e}"

    def _get_ws(self, wb, sheet_name: Optional[str] = None):
        """获取工作表，默认返回活动工作表"""
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                return None, f"工作表 '{sheet_name}' 不存在，已有: {wb.sheetnames}"
            return wb[sheet_name], None
        return wb.active, None

    def _cell_range(self, start_row: int, start_col: int,
                    rows: int, cols: int) -> str:
        """生成区域字符串，如 'A1:C3'"""
        from openpyxl.utils import get_column_letter
        c1 = get_column_letter(start_col)
        c2 = get_column_letter(start_col + cols - 1)
        return f"{c1}{start_row}:{c2}{start_row + rows - 1}"

    # ═══════════════════════════════════════════════════════
    # 各 action 处理函数
    # ═══════════════════════════════════════════════════════

    def _status(self, kwargs: dict) -> dict:
        """查询文件/状态信息"""
        file_path = kwargs.get("file_path", "")
        if not file_path:
            return {"success": True, "data": {
                "excel": "openpyxl",
                "version": openpyxl.__version__,
                "message": "需要 file_path 参数来打开具体文件",
            }}
        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        info = {
            "file": os.path.basename(file_path),
            "sheets": wb.sheetnames,
            "active_sheet": wb.active.title if wb.active else None,
            "sheet_count": len(wb.sheetnames),
        }
        wb.close()
        return {"success": True, "data": info}

    def _open(self, kwargs: dict) -> dict:
        """打开（加载）文件；openpyxl 无持久 session，只做验证"""
        file_path = kwargs.get("file_path", "")
        if not file_path:
            return {"success": False, "error": "请提供 file_path 参数"}
        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        wb.close()
        return {"success": True, "data": {
            "opened": file_path,
            "sheets": wb.sheetnames,
            "active_sheet": wb.active.title,
        }}

    def _close(self, kwargs: dict) -> dict:
        """关闭文件（openpyxl 无状态，返回 ok）"""
        return {"success": True, "data": {"closed": True}}

    def _read(self, kwargs: dict) -> dict:
        """
        读取：优先 range > cell > sheet_name（整表）
        """
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")
        cell = kwargs.get("cell")
        range_val = kwargs.get("range")

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        ws, err = self._get_ws(wb, sheet_name)
        if err:
            wb.close()
            return {"success": False, "error": err}

        if range_val:
            rows_list = []
            for row in ws[range_val]:
                rows_list.append([cell.value for cell in row])
            wb.close()
            return {"success": True, "data": {
                "range": range_val, "rows": rows_list,
                "row_count": len(rows_list),
            }}
        elif cell:
            val = ws[cell].value
            wb.close()
            return {"success": True, "data": {
                "cell": cell, "value": val,
            }}
        else:
            # 整表读取
            rows_list = []
            for row in ws.iter_rows(values_only=True):
                rows_list.append(list(row))
            wb.close()
            return {"success": True, "data": {
                "sheet": ws.title, "rows": rows_list,
                "row_count": len(rows_list), "col_count": len(rows_list[0]) if rows_list else 0,
            }}

    def _write(self, kwargs: dict) -> dict:
        """写入单元格"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")
        cell = kwargs.get("cell", "A1")
        value = kwargs.get("value", "")

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        ws, err = self._get_ws(wb, sheet_name)
        if err:
            wb.close()
            return {"success": False, "error": err}
        ws[cell] = value
        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {"cell": cell, "value": value}}

    def _write_cells(self, kwargs: dict) -> dict:
        """
        批量写入区域。values 为 JSON 二维数组，
        可选 start_row / start_col 指定起始位置（从1开始）。
        """
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")
        values_raw = kwargs.get("values", "[]")
        start_row = kwargs.get("start_row", 1)
        start_col = kwargs.get("start_col", 1)

        try:
            values = json.loads(values_raw) if isinstance(values_raw, str) else values_raw
        except json.JSONDecodeError:
            return {"success": False, "error": "values 必须是有效的 JSON 二维数组"}

        if not isinstance(values, list) or not values:
            return {"success": False, "error": "values 至少需要一行数据"}

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        ws, err = self._get_ws(wb, sheet_name)
        if err:
            wb.close()
            return {"success": False, "error": err}

        for i, row_data in enumerate(values):
            for j, val in enumerate(row_data):
                ws.cell(row=start_row + i, column=start_col + j, value=val)

        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {
            "written_rows": len(values),
            "written_cells": sum(len(r) for r in values),
        }}

    def _write_table(self, kwargs: dict) -> dict:
        """
        写入完整表格（一次完成数据 + 格式）。
        headers = JSON 字符串数组
        data = JSON 二维数组
        """
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")
        headers_raw = kwargs.get("headers", "[]")
        data_raw = kwargs.get("data", "[]")
        header_bold = kwargs.get("header_bold", True)
        header_bg = kwargs.get("header_bg", "")
        auto_width = kwargs.get("auto_width", True)
        start_row = kwargs.get("start_row", 1)
        start_col = kwargs.get("start_col", 1)

        try:
            headers = json.loads(headers_raw) if isinstance(headers_raw, str) else headers_raw
            data = json.loads(data_raw) if isinstance(data_raw, str) else data_raw
        except json.JSONDecodeError:
            return {"success": False, "error": "headers 或 data 不是有效的 JSON"}

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        ws, err = self._get_ws(wb, sheet_name)
        if err:
            wb.close()
            return {"success": False, "error": err}

        # 写表头
        for j, h in enumerate(headers):
            cell = ws.cell(row=start_row, column=start_col + j, value=h)
            if header_bold:
                cell.font = Font(bold=True)
            if header_bg:
                cell.fill = PatternFill(start_color=_parse_color(header_bg),
                                        end_color=_parse_color(header_bg),
                                        fill_type="solid")

        # 写数据体
        for i, row_data in enumerate(data):
            for j, val in enumerate(row_data):
                ws.cell(row=start_row + 1 + i, column=start_col + j, value=val)

        # 自动列宽
        if auto_width:
            for j in range(len(headers)):
                max_len = len(str(headers[j]))
                for i, row_data in enumerate(data):
                    if j < len(row_data):
                        max_len = max(max_len, len(str(row_data[j])))
                col_letter = get_column_letter(start_col + j)
                ws.column_dimensions[col_letter].width = min(max_len + 3, 60)

        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {
            "headers": headers,
            "rows_written": len(data),
            "cols": len(headers),
        }}

    def _analyze(self, kwargs: dict) -> dict:
        """分析工作表结构"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        ws, err = self._get_ws(wb, sheet_name)
        if err:
            wb.close()
            return {"success": False, "error": err}

        info = {
            "sheet": ws.title,
            "dimensions": ws.dimensions,
            "max_row": ws.max_row,
            "max_column": ws.max_column,
            "merged_cells": [str(m) for m in ws.merged_cells.ranges],
            "has_data": ws.max_row > 0,
        }

        # 尝试识别第一行为表头
        if ws.max_row > 0:
            headers = []
            for cell in ws[1]:
                headers.append(cell.value)
            info["header_row"] = headers
            info["header_count"] = len(headers)

        # 基本数据类型统计
        type_stats = {"string": 0, "number": 0, "date": 0, "empty": 0, "other": 0}
        for row in ws.iter_rows(values_only=True):
            for val in row:
                if val is None:
                    type_stats["empty"] += 1
                elif isinstance(val, (int, float)):
                    type_stats["number"] += 1
                elif isinstance(val, str):
                    type_stats["string"] += 1
                elif hasattr(val, "strftime"):
                    type_stats["date"] += 1
                else:
                    type_stats["other"] += 1
        info["type_stats"] = type_stats

        wb.close()
        return {"success": True, "data": info}

    def _create_sheet(self, kwargs: dict) -> dict:
        """创建工作表。如果已存在则自动切换过去，不报错。"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "Sheet1")

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        if sheet_name in wb.sheetnames:
            wb.close()
            return {"success": True, "data": {
                "action": "switched",
                "sheet_name": sheet_name,
                "message": f"切换到已存在的工作表: {sheet_name}",
            }}
        wb.create_sheet(title=sheet_name)
        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {"created": sheet_name}}

    def _rename_sheet(self, kwargs: dict) -> dict:
        """重命名工作表"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")
        new_name = kwargs.get("new_name", "")

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        if sheet_name not in wb.sheetnames:
            wb.close()
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]
        ws.title = new_name
        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {"from": sheet_name, "to": new_name}}

    def _switch_sheet(self, kwargs: dict) -> dict:
        """切换活动工作表——openpyxl 中设置 active"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        if sheet_name not in wb.sheetnames:
            wb.close()
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        wb.active = wb.sheetnames.index(sheet_name)
        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {"active_sheet": sheet_name}}

    def _delete_sheet(self, kwargs: dict) -> dict:
        """删除工作表"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        if sheet_name not in wb.sheetnames:
            wb.close()
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        if len(wb.sheetnames) == 1:
            wb.close()
            return {"success": False, "error": "至少保留一个工作表，不能删除最后一个"}
        del wb[sheet_name]
        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {"deleted": sheet_name}}

    def _list_sheets(self, kwargs: dict) -> dict:
        """列出所有工作表"""
        file_path = kwargs.get("file_path", "")

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        sheets = []
        for name in wb.sheetnames:
            ws = wb[name]
            sheets.append({
                "name": name,
                "rows": ws.max_row,
                "cols": ws.max_column,
            })
        wb.close()
        return {"success": True, "data": {
            "sheets": sheets,
            "active": wb.active.title if wb.active else None,
        }}

    def _set_font(self, kwargs: dict) -> dict:
        """设置单元格字体"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")
        cell = kwargs.get("cell", "A1")
        color = kwargs.get("color", "")
        bold = kwargs.get("bold")
        size = kwargs.get("size")

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        ws, err = self._get_ws(wb, sheet_name)
        if err:
            wb.close()
            return {"success": False, "error": err}

        font_kw = {}
        if color:
            font_kw["color"] = _parse_color(color)
        if bold is not None:
            font_kw["bold"] = bold
        if size:
            font_kw["size"] = size

        ws[cell].font = Font(**font_kw)
        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {"cell": cell, "font": font_kw}}

    def _set_column_width(self, kwargs: dict) -> dict:
        """设置列宽"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")
        cell = kwargs.get("cell", "A")
        width = kwargs.get("width", 10)

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        ws, err = self._get_ws(wb, sheet_name)
        if err:
            wb.close()
            return {"success": False, "error": err}

        ws.column_dimensions[cell.replace("1", "").replace("0", "")].width = width
        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {"column": cell, "width": width}}

    def _set_row_height(self, kwargs: dict) -> dict:
        """设置行高"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")
        cell = kwargs.get("cell", "1")
        height = kwargs.get("height", 20)

        row_num = int(cell.replace("A", "").replace("B", ""))
        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        ws, err = self._get_ws(wb, sheet_name)
        if err:
            wb.close()
            return {"success": False, "error": err}

        ws.row_dimensions[row_num].height = height
        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {"row": row_num, "height": height}}

    def _merge_cells(self, kwargs: dict) -> dict:
        """合并单元格"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")
        range_val = kwargs.get("range", "")

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        ws, err = self._get_ws(wb, sheet_name)
        if err:
            wb.close()
            return {"success": False, "error": err}

        ws.merge_cells(range_val)
        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {"merged": range_val}}

    def _set_background(self, kwargs: dict) -> dict:
        """设置单元格背景色"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")
        cell = kwargs.get("cell", "A1")
        bg_color = kwargs.get("bg_color", "yellow")
        range_val = kwargs.get("range", "")
        start_row = kwargs.get("start_row")
        start_col = kwargs.get("start_col")

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        ws, err = self._get_ws(wb, sheet_name)
        if err:
            wb.close()
            return {"success": False, "error": err}

        fill = PatternFill(start_color=_parse_color(bg_color),
                           end_color=_parse_color(bg_color),
                           fill_type="solid")

        if range_val:
            for row in ws[range_val]:
                for c in row:
                    c.fill = fill
        elif start_row and start_col:
            ws.cell(row=start_row, column=start_col).fill = fill
        else:
            ws[cell].fill = fill

        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {"filled": range_val or cell, "color": bg_color}}

    def _set_column_background(self, kwargs: dict) -> dict:
        """设置整列背景色"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")
        cell = kwargs.get("cell", "A")
        bg_color = kwargs.get("bg_color", "yellow")

        col_letter = cell.replace("1", "").replace("0", "")
        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        ws, err = self._get_ws(wb, sheet_name)
        if err:
            wb.close()
            return {"success": False, "error": err}

        fill = PatternFill(start_color=_parse_color(bg_color),
                           end_color=_parse_color(bg_color),
                           fill_type="solid")
        for row in ws.iter_rows(min_col=column_index_from_string(col_letter),
                                max_col=column_index_from_string(col_letter)):
            for c in row:
                c.fill = fill

        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {"column": col_letter, "color": bg_color}}

    def _set_conditional_format(self, kwargs: dict) -> dict:
        """设置条件格式"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")
        range_val = kwargs.get("range", "A1:A10")
        cond_type = kwargs.get("cond_type", "cellIs")
        condition = kwargs.get("condition", "greaterThan")
        cond_value = kwargs.get("cond_value", "0")
        cond_fill = kwargs.get("cond_fill", "yellow")

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        ws, err = self._get_ws(wb, sheet_name)
        if err:
            wb.close()
            return {"success": False, "error": err}

        fill = PatternFill(start_color=_parse_color(cond_fill),
                           end_color=_parse_color(cond_fill),
                           fill_type="solid")

        if cond_type == "cellIs":
            ws.conditional_formatting.add(
                range_val,
                CellIsRule(operator=condition, formula=[cond_value], fill=fill)
            )
        elif cond_type == "formula":
            ws.conditional_formatting.add(
                range_val,
                FormulaRule(formula=[condition], fill=fill)
            )

        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {
            "range": range_val, "type": cond_type, "fill": cond_fill,
        }}

    def _insert_rows(self, kwargs: dict) -> dict:
        """插入行"""
        file_path = kwargs.get("file_path", "")
        sheet_name = kwargs.get("sheet_name", "")
        start_row = kwargs.get("start_row", 1)
        row_count = kwargs.get("row_count", 1)

        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        ws, err = self._get_ws(wb, sheet_name)
        if err:
            wb.close()
            return {"success": False, "error": err}

        ws.insert_rows(start_row, row_count)
        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {
            "inserted_at": start_row, "count": row_count,
        }}

    def _save(self, kwargs: dict) -> dict:
        """保存文件"""
        file_path = kwargs.get("file_path", "")
        if not file_path:
            return {"success": False, "error": "请提供 file_path"}
        # 只是验证文件可写
        if not os.path.exists(file_path):
            return {"success": False, "error": f"文件不存在: {file_path}"}
        wb, err = self._ensure_wb(file_path)
        if err:
            return {"success": False, "error": err}
        wb.save(file_path)
        wb.close()
        return {"success": True, "data": {"saved": file_path}}

    def _save_as_pdf(self, kwargs: dict) -> dict:
        """保存为 PDF——openpyxl 不支持直接导出 PDF，打印提示"""
        return {"success": True, "data": {
            "note": "openpyxl 不直接支持 PDF 导出。"
                    "请用 Excel 打开后另存为 PDF，或使用虚拟打印机。",
        }}