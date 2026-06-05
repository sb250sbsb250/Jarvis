"""
工具包 — 注册入口（合并版：27→9）

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


# ── 12 个统一工具 ──
from .file_tool import FileTool
from .excel_tool import ExcelTool
from .code_graph_tool import CodeGraphTool     # 代码分析（只读）: AST/依赖/调用链
from .code_tool import CodeTool                # 代码编辑（读写）: read/diff/write/rollback/append/create
from .pdf_tool import PdfReadTool              # PDF: 文本提取/表格/扫描件
from .word_tool import WordTool                # Word: read_docx/write_docx
from .pentest_tool import PentestTool
from .shell_tool import ShellExecuteTool
from .web_tool import WebTool
from .git_tool import GitTool
from .system_tool import SystemTool
from .image_tool import ImageTool


# ── 工具类列表（12个） ──
ALL_TOOL_CLASSES: List[type] = [
    FileTool,          # 通用文件: list/read/write/append/rename/diff/glob
    ExcelTool,         # Excel: connect/read/write/migrate...
    CodeGraphTool,     # 代码分析（只读）: AST/依赖/调用链/影响分析
    CodeTool,          # 代码编辑（读写）: read/diff/write/rollback/append/create
    PdfReadTool,       # PDF: 文本提取/表格/扫描件检测
    WordTool,          # Word: read_docx/write_docx
    PentestTool,       # 渗透测试: 18工具 via WSL
    ShellExecuteTool,  # Shell: 命令执行
    WebTool,           # 网络: fetch/search
    GitTool,           # 版本控制: status/commit/push
    SystemTool,        # 系统: info/time/cwd
    ImageTool,         # 图片: read/ocr
]


# ── 默认配置 ──
DEFAULT_TOOL_CONFIGS: Dict[type, Dict[str, Any]] = {
    ShellExecuteTool: {"timeout": 30},
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

    logger.info(f"工具注册完成: {registered} 个注册, {skipped} 个跳过")
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
                logger.info(f"  🔧 {temp.name}: {temp.description[:60]}")
            except:
                logger.info(f"  🔧 {tc.__name__}")
