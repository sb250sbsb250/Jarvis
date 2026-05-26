"""
专家系统 — Jarvis V3 多 Agent 协作层

提供领域专家的注册、路由、编排能力。
每个专家拥有专属工具集和系统提示词，由 ExpertOrchestrator 自动路由。

使用示例:
    from engine.expert import ExpertRegistry, ExpertOrchestrator

    registry = ExpertRegistry()
    # ... 注册自定义 ExpertAgent ...

    orchestrator = ExpertOrchestrator(llm_client, registry)
    result = await orchestrator.process("帮我写一段 Python 代码")
"""
from .base import ExpertDomain, ExpertAgent
from .registry import ExpertRegistry
from .orchestrator import ExpertOrchestrator

__all__ = [
    "ExpertDomain",
    "ExpertAgent",
    "ExpertRegistry",
    "ExpertOrchestrator",
]
