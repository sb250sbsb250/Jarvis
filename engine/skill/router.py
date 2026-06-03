"""
skill/router.py — Skill 路由器

统一走 AgentLoop 执行，不再区分 DAG multi-mode 路径。
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple, Callable, Awaitable

from ..tool.registry import ToolRegistry
from .registry import SkillRegistry
from .base import Skill, SkillResult

from ..agent_loop import AgentLoop

logger = logging.getLogger(__name__)


class SkillRouter:
    """
    Skill 路由器 — 统一走 AgentLoop

    流程:
    1. 路由 → 匹配 Skill
    2. 构建 system_prompt（base + Skill domain）
    3. AgentLoop.run() 执行
    """

    def __init__(
        self,
        llm_client: Any,
        skill_registry: Optional[SkillRegistry] = None,
        tool_registry: Optional[ToolRegistry] = None,
    ):
        self.llm_client = llm_client
        self.skill_registry = skill_registry or SkillRegistry()
        self.tool_registry = tool_registry or ToolRegistry()

    async def process(
        self,
        user_input: str,
        skill_name: Optional[str] = None,
        history: Optional[Any] = None,
        on_event: Optional[Callable[[str, Dict], Awaitable[None]]] = None,
        **kwargs,
    ) -> SkillResult:
        """
        处理用户输入

        Args:
            user_input: 用户输入
            skill_name: 指定 Skill 名称（None = 自动路由）
            history: 消息历史
            on_event: 事件回调 async fn(event_type, data)
            **kwargs: 传给 AgentLoop.run() 的额外参数

        Returns:
            SkillResult
        """
        # 1. 路由（关键词 + LLM 语义回退）
        if skill_name:
            skill = self.skill_registry.route_exact(skill_name)
            if not skill:
                logger.info(f"🎯 指定 Skill '{skill_name}' 不存在")
                return SkillResult(success=False, error=f"Skill '{skill_name}' 不存在")
            logger.info(f"🎯 指定 Skill: {skill.meta.display_name} ({skill.meta.name})")
        else:
            candidates = await self.skill_registry.route_with_fallback(
                user_input, llm_client=self.llm_client, top_k=1
            )
            skill = candidates[0][0] if candidates else None
            if skill:
                logger.info(
                    f"🎯 Skill 匹配: {skill.meta.display_name} ({skill.meta.name}) | "
                    f"置信度: {candidates[0][1]:.0%}"
                )
            else:
                logger.info("🎯 无匹配 Skill，走 AgentLoop 自主执行")

        # 2. 框架模板在 agent_loop._BASE_TEMPLATE，领域数据由 skill 注入
        system_prompt = ""
        logger.info(f"📝 Skill: {skill.meta.display_name if skill else '无'}")

        # 3. AgentLoop 执行
        loop = AgentLoop(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            system_prompt=system_prompt,
            skill=skill,
        )

        history_list: Optional[List[Dict]] = None
        if history is not None:
            if hasattr(history, "get_for_llm"):
                history_list = history.get_for_llm()
            elif isinstance(history, list):
                history_list = history

        try:
            # AgentLoop.run() 只接受部分参数，过滤掉多余的 kwargs
            loop_kwargs = {}
            for k in ("working_dir",):
                if k in kwargs:
                    loop_kwargs[k] = kwargs[k]
            result = await loop.run(
                task=user_input,
                history=history_list,
                on_event=on_event,
                **loop_kwargs,
            )
        except Exception as e:
            logger.exception(f"AgentLoop 执行失败: {e}")
            return SkillResult(success=False, error=f"AgentLoop 执行失败: {e}")

        return SkillResult(
            success=result.get("success", False),
            content=result.get("content", ""),
            data={"agent_steps": result.get("rounds", 0)},
        )
