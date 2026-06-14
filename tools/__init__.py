"""
工具包 — 注册入口（v3.0 原子工具版）

每个大类注册多个原子工具。
LLM 看到的扁平工具列表由 Registry 自动生成。

使用方式：
    from tools import register_all_tools
    registry = ToolRegistry()
    register_all_tools(registry)
"""

import logging
from typing import Dict, Optional, List, Type

from engine.tool.base import BaseTool
from engine.tool.registry import ToolRegistry as _ToolRegistry

logger = logging.getLogger(__name__)

# ── 9 个大类 ──
from .file_tool import FileTool              # 7 个原子工具
from .excel_tool import ExcelTool            # 10 个原子工具
from .code_graph_tool import CodeGraphTool   # 7 个原子工具
from .code_tool import CodeTool              # 6 个原子工具
from .pdf_tool import PdfTool                # 1 个原子工具
from .word_tool import WordTool              # 2 个原子工具
from .shell_tool import ShellExecuteTool     # 1 个原子工具
from .web_tool import WebTool                # 2 个原子工具
from .git_tool import GitTool                # 3 个原子工具
from .system_tool import SystemTool          # 3 个原子工具
from .image_tool import ImageTool            # 2 个原子工具
from .pentest_tool import PentestTool        # 1 个原子工具
from .todo_tool import TodoTool              # 2 个原子工具

# ── 大类列表（13 个大类，共 47 个原子工具） ──
ALL_TOOL_CLASSES: List[type] = [
    FileTool,          # 文件: list/read/glob/write/append/rename/diff
    ExcelTool,         # Excel: open/close/list_sheets/read_sheet/write...
    CodeGraphTool,     # 代码图谱: related/symbol/callers/callees/impact/folder/stats
    CodeTool,          # 代码编辑: read/diff/write/rollback/append/create
    PdfTool,           # PDF: read
    WordTool,          # Word: read/write
    PentestTool,       # 渗透: run
    ShellExecuteTool,  # Shell: run
    WebTool,           # 网络: fetch/search
    GitTool,           # Git: status/commit/push
    SystemTool,        # 系统: info/time/cwd
    ImageTool,         # 图片: read/ocr
    TodoTool,          # Todo: write/list
]

# ── 默认配置 ──
DEFAULT_TOOL_CONFIGS: Dict[Type, Dict] = {
    ShellExecuteTool: {"timeout": 30},
    CodeGraphTool: {"project_root": "."},
}

STANDARD_TOOL_NAMES = [
    # 文件
    "file_list", "file_read", "file_glob", "file_write",
    "file_append", "file_rename", "file_diff",
    # Excel
    "excel_open", "excel_close", "excel_list_sheets", "excel_read_sheet",
    "excel_get_structure", "excel_write_cell", "excel_write_by_header",
    "excel_insert_rows", "excel_format_range", "excel_save",
    # 代码图谱
    "code_graph_related", "code_graph_symbol", "code_graph_callers",
    "code_graph_callees", "code_graph_impact", "code_graph_folder",
    "code_graph_stats",
    # 代码编辑
    "code_read", "code_diff", "code_write", "code_rollback",
    "code_append", "code_create",
    # PDF
    "pdf_read",
    "pdf_split",
    "pdf_concat",
    # Word
    "word_read", "word_write",
    # Shell
    "shell_run",
    # 网络
    "web_fetch", "web_search",
    # Git
    "git_status", "git_commit", "git_push",
    # 系统
    "system_info", "system_time", "system_cwd",
    # 图片
    "image_read", "image_ocr",
    # 渗透
    "pentest_run",
    # Todo
    "todo_write", "todo_list",
]


def register_all_tools(
    registry: _ToolRegistry,
    config: Optional[Dict[str, Dict]] = None,
    exclude: Optional[List[str]] = None,
) -> _ToolRegistry:
    """
    注册所有工具大类到 Registry。

    Args:
        registry: ToolRegistry 实例
        config: 覆盖默认配置，{大类名: {配置字典}}
        exclude: 要跳过的大类名列表
    """
    exclude = exclude or []
    custom_config = config or {}
    registered = 0

    for tool_class in ALL_TOOL_CLASSES:
        try:
            default_kwargs = DEFAULT_TOOL_CONFIGS.get(tool_class, {})
            temp = tool_class(**default_kwargs)
            ns = temp.name
        except Exception as e:
            logger.error(f"获取工具名称失败: {tool_class.__name__}: {e}")
            continue

        if ns in exclude:
            logger.debug(f"跳过: {ns}")
            continue

        kwargs = dict(DEFAULT_TOOL_CONFIGS.get(tool_class, {}))
        if ns in custom_config:
            kwargs.update(custom_config[ns])

        registry.register(tool_class, **kwargs)
        registered += 1

    logger.info(f"注册完成: {registered} 个大类")
    return registry


def print_tool_list(registry: Optional[_ToolRegistry] = None):
    """打印所有工具（大类 + 原子工具）"""
    if registry:
        status = registry.get_status()
        logger.info(f"ToolRegistry v3: {status['categories']} 个大类, {status['atomic_tools']} 个原子工具")
        for ns in sorted(registry.list_categories()):
            inst = registry._get_instance(ns) if hasattr(registry, '_get_instance') else None
            if inst:
                names = inst.get_tool_names()
                logger.info(f"  {ns} ({len(names)}): {', '.join(names)}")
    else:
        for tc in ALL_TOOL_CLASSES:
            try:
                kwargs = DEFAULT_TOOL_CONFIGS.get(tc, {})
                inst = tc(**kwargs)
                names = inst.get_tool_names()
                logger.info(f"  {inst.name} ({len(names)}): {', '.join(names)}")
            except Exception:
                logger.info(f"  {tc.__name__}")
