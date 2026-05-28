"""
工具包 — 注册入口（合并版）

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
from .file_tool import ListFilesTool, ReadFileTool, WriteFileTool, FileRenameTool, DiffFileTool, ReadImageTool, ReadPdfTool
from .excel_tool import ExcelTool
from .shell_tool import ShellExecuteTool
from .system_tool import SystemInfoTool, GetTimeTool
from .edit_tool import EditTool
from .code_editor_v3 import ProjectSearchTool, CodeEditorTool
from .web_tool import WebFetchTool, WebSearchTool
from .git_tool import GitTool
from .code_tool import CodeSearchTool
from .pdf_tool import PdfReadTool
from .word_tool import WordTool
from .image_recognize_tool import ImageRecognizeTool


# ── 工具类列表（精简去重版） ──
ALL_TOOL_CLASSES: List[type] = [
    # 文件读写
    ListFilesTool, ReadFileTool, WriteFileTool, FileRenameTool,
    DiffFileTool, ReadImageTool, ReadPdfTool,
    # Excel（统一版）
    ExcelTool,
    # 代码搜索/编辑
    ProjectSearchTool, CodeEditorTool, CodeSearchTool,
    # 图片识别
    ImageRecognizeTool,
    # 系统
    ShellExecuteTool, SystemInfoTool, GetTimeTool,
    # 网络
    WebFetchTool, WebSearchTool,
    # 编辑
    EditTool,
    # 版本控制
    GitTool,
    # Office
    WordTool,
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
    exclude = exclude or []
    custom_config = config or {}
    registered = 0
    skipped = 0

    for tool_class in ALL_TOOL_CLASSES:
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
