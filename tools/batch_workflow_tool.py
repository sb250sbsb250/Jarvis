"""
tools/batch_workflow_tool.py — 批处理工具（向后兼容入口）

提供一个 `batch_process` 工具，让 Agent 可以以传统方式调用批处理。
内部使用 DAG 执行引擎，保持可观测性。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from engine.dag import (
    AgentGraphBuilder, GraphExecutor, WorkflowGraph,
    ListFilesNode, FileProcessorNode, FileRenameNode, MapNode,
    LLMNode, ExecutionContext,
    global_tracer,
)
from engine.dag.batch_processors import get_processor, list_processors

logger = logging.getLogger(__name__)

# ── 配置 ──

DEFAULT_TIMEOUT = 300  # 5 分钟
MAX_PARALLEL = 10


async def batch_process(
    folder_path: str,
    processor_name: str = "file_exists",
    file_patterns: str = ".jpg,.png,.pdf",
    post_action: str = "rename",
    dry_run: bool = True,
    llm_client: Any = None,
    tool_registry: Any = None,
) -> Dict[str, Any]:
    """
    批量处理文件。

    Args:
        folder_path: 目标文件夹路径
        processor_name: 处理器名称
        file_patterns: 文件匹配模式（逗号分隔）
        post_action: 后置操作 ("rename" / "none")
        dry_run: 试运行模式
        llm_client: LLM 客户端（自动从上下文中获取）
        tool_registry: 工具注册表

    Returns:
        执行摘要
    """
    if not os.path.isdir(folder_path):
        return {"error": f"文件夹不存在: {folder_path}"}

    processor_fn = get_processor(processor_name)
    if processor_fn is None:
        available = list(list_processors().keys())
        return {
            "error": f"处理器 '{processor_name}' 未注册",
            "available_processors": available,
        }

    # 构建 DAG
    builder = AgentGraphBuilder(
        llm_client=llm_client,
        tool_registry=tool_registry,
        system_prompt="你是一个文件批处理助手。",
    )

    graph = builder.build_batch_process(
        folder_path=folder_path,
        processor_name=processor_name,
        post_action=post_action,
        file_patterns=file_patterns,
        dry_run=dry_run,
        max_parallel=MAX_PARALLEL,
    )

    # 执行
    executor = GraphExecutor(
        llm_client=llm_client,
        tool_registry=tool_registry,
        max_parallel=MAX_PARALLEL,
    )

    initial_input = {
        "folder_path": folder_path,
        "file_patterns": file_patterns,
    }

    try:
        ctx = await executor.run(
            graph,
            initial_input,
            timeout=DEFAULT_TIMEOUT,
            enable_tracing=True,
        )

        summary = ctx.get_summary()

        # 提取关键结果
        file_list = ctx.get_node_output("list_files", "output")
        processed = ctx.get_node_output("map_process", "output")
        rename_result = ctx.get_node_output("rename_files", "output")

        result = {
            "success": not summary.get("has_error", False),
            "summary": summary,
            "file_count": ctx.get_node_output("list_files", "count"),
            "processed": processed,
            "rename_result": rename_result,
            "folder_path": folder_path,
            "processor": processor_name,
        }

        return result

    except Exception as e:
        logger.exception(f"批量处理失败: {e}")
        return {
            "error": str(e),
            "success": False,
        }


# ── 工具定义（供 ToolRegistry 注册） ──

BATCH_WORKFLOW_TOOL = {
    "type": "function",
    "function": {
        "name": "batch_process",
        "description": "批量处理文件夹中的文件。支持列出文件、文件分析、批量重命名等操作。",
        "parameters": {
            "type": "object",
            "properties": {
                "folder_path": {
                    "type": "string",
                    "description": "目标文件夹路径",
                },
                "processor_name": {
                    "type": "string",
                    "description": "处理器名称。可选: file_exists, text_analyzer",
                    "enum": list(list_processors().keys()),
                },
                "file_patterns": {
                    "type": "string",
                    "description": "文件匹配模式，逗号分隔（默认 .jpg,.png,.pdf）",
                },
                "post_action": {
                    "type": "string",
                    "enum": ["rename", "none"],
                    "description": "后置操作: rename=根据结果重命名, none=仅处理",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "试运行模式，不实际重命名",
                },
            },
            "required": ["folder_path", "processor_name"],
        },
    },
}
