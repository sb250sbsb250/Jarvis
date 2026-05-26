"""
batch_processors.py — 文件级处理器注册中心

提供可注册的文件处理函数，供 MapNode + FileProcessorNode 调度。
每个处理器接收一个文件路径，返回结构化结果 dict。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# ── 处理器注册表 ──

_PROCESSORS: Dict[str, Callable[[str], Optional[Dict[str, Any]]]] = {}


def register_processor(name: str) -> Callable:
    """装饰器：注册一个文件处理器"""
    def decorator(fn: Callable[[str], Optional[Dict[str, Any]]]):
        _PROCESSORS[name] = fn
        logger.info(f"[processors] 注册处理器: {name}")
        return fn
    return decorator


def get_processor(name: str) -> Optional[Callable[[str], Optional[Dict[str, Any]]]]:
    """获取已注册的处理器"""
    return _PROCESSORS.get(name)


def list_processors() -> Dict[str, str]:
    """列出所有已注册的处理器（名称 + 文档）"""
    return {
        name: (fn.__doc__ or "").strip()[:100]
        for name, fn in _PROCESSORS.items()
    }


# ══════════════════════════════════════
#  内置默认处理器
# ══════════════════════════════════════


@register_processor("file_exists")
def _check_file_exists(file_path: str) -> Optional[Dict[str, Any]]:
    """检查文件是否存在并返回基本信息"""
    if not os.path.exists(file_path):
        return None
    stat = os.stat(file_path)
    return {
        "file_name": os.path.basename(file_path),
        "file_size": stat.st_size,
        "last_modified": stat.st_mtime,
        "is_file": os.path.isfile(file_path),
        "is_dir": os.path.isdir(file_path),
    }


@register_processor("text_analyzer")
def _text_analyzer(file_path: str) -> Optional[Dict[str, Any]]:
    """分析文本文件：字数、行数、字符统计"""
    if not os.path.isfile(file_path):
        return None
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        lines = content.splitlines()
        return {
            "file_name": os.path.basename(file_path),
            "char_count": len(content),
            "line_count": len(lines),
            "word_count": len(content.split()),
            "preview": content[:200],
        }
    except Exception as e:
        logger.warning(f"[text_analyzer] 读取失败 {file_path}: {e}")
        return None
