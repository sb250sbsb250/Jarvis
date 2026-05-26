"""
工具包 — 注册入口（合并版）

合并记录：
  P0 git_tool (3→1): GitStatusTool+GitCommitTool+GitPushTool → GitTool
  P0 file_tool (3→1): FileReadTool+FileWriteTool+FileListTool → FileTool
  P0 edit_tool (2→1): EditFileTool+InsertInFileTool → EditTool
  P3 excel_tool (26→1): ExcelAppOps+ExcelCellOps+ExcelSheetOps → ExcelTool

使用方式：
    from tools import register_all_tools
    registry = ToolRegistry()
    register_all_tools(registry)

设计：注册时只存类引用，不实例化
"""

import sys, os, logging
from typing import Dict, Any, Optional, List, Type

from engine.tool.base import BaseTool
from engine.tool.registry import ToolRegistry as _ToolRegistry

logger = logging.getLogger(__name__)


# ── 导入所有工具类 ──
from .file_tool import FileTool
from .excel_tool import ExcelTool
from .shell_tool import ShellExecuteTool
from .system_tool import SystemInfoTool, GetTimeTool
from .edit_tool import EditTool
from .search_tool import GlobFindTool, GrepSearchTool
from .web_tool import WebFetchTool, WebSearchTool
from .git_tool import GitTool
from .code_tool import ReadCodeTool, CodeReviewTool
from .image_tool import ImageGenerateTool
from .pdf_tool import PdfReadTool
from .schedule_tool import ScheduleAddTool, ScheduleListTool
from .process_tool import ProcessListTool


# ── 工具类列表 ──
ALL_TOOL_CLASSES: List[type] = [
    FileTool,
    ExcelTool,
    ShellExecuteTool,
    SystemInfoTool, GetTimeTool,
    EditTool,
    GlobFindTool, GrepSearchTool,
    WebFetchTool, WebSearchTool,
    GitTool,
    ReadCodeTool, CodeReviewTool,
    ImageGenerateTool,
    PdfReadTool,
    ScheduleAddTool, ScheduleListTool,
    ProcessListTool,
]


# ── 默认配置 ──
DEFAULT_TOOL_CONFIGS: Dict[type, Dict[str, Any]] = {
    ShellExecuteTool: {"timeout": 30},
    WebFetchTool: {"timeout": 10},
    WebSearchTool: {"max_results": 5},
}


def register_all_tools(
    registry: _ToolRegistry,
    config: Optional[Dict[str, Dict[str, Any]]] = None,
    exclude: Optional[List[str]] = None,
) -> _ToolRegistry:
    """
    注册所有工具（懒加载）

    Args:
        registry: 工具注册中心
        config: 自定义配置，格式: {"tool_name": {"key": "val"}}
        exclude: 排除的工具名称列表

    Returns:
        registry（支持链式调用）
    """
    exclude = exclude or []
    custom_config = config or {}
    registered = 0
    skipped = 0

    for tool_class in ALL_TOOL_CLASSES:
        # 获取工具名称（临时实例化）
        try:
            default_kwargs = DEFAULT_TOOL_CONFIGS.get(tool_class, {})
            temp = tool_class(**default_kwargs)
            tool_name = temp.name
        except Exception as e:
            logger.error(f"获取工具名称失败: {tool_class.__name__}: {e}")
            continue

        if tool_name in exclude:
            logger.debug(f"跳过: {tool_name}")
            skipped += 1
            continue

        # 合并配置
        kwargs = dict(DEFAULT_TOOL_CONFIGS.get(tool_class, {}))
        if tool_name in custom_config:
            kwargs.update(custom_config[tool_name])

        registry.register(tool_class, **kwargs)
        registered += 1

    return registry


def print_tool_list(registry: Optional[_ToolRegistry] = None):
    """打印工具列表"""
    if registry:
        status = registry.get_status()
        logger.info(f"工具注册摘要: {status['registered']} 个注册, {status['cached']} 个已实例化, {status['lazy_pending']} 个待懒加载")
        for name in sorted(registry.list_tools()):
            state = "✅ 已缓存" if registry._cache.get(name) else "💤 懒加载"
            logger.info(f"  {state} {name}")
    else:
        logger.info(f"可用工具 ({len(ALL_TOOL_CLASSES)} 个):")
        for tc in ALL_TOOL_CLASSES:
            try:
                kwargs = DEFAULT_TOOL_CONFIGS.get(tc, {})
                temp = tc(**kwargs)
                logger.info(f"  🔧 {temp.name}: {temp.description[:50]}")
            except:
                logger.info(f"  🔧 {tc.__name__}")
