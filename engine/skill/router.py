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
        # 1. 路由
        if skill_name:
            skill = self.skill_registry.route_exact(skill_name)
            if not skill:
                logger.info(f"🎯 指定 Skill '{skill_name}' 不存在")
                return SkillResult(success=False, error=f"Skill '{skill_name}' 不存在")
            logger.info(f"🎯 指定 Skill: {skill.meta.display_name} ({skill.meta.name})")
        else:
            candidates = self.skill_registry.route(user_input, top_k=1)
            skill = candidates[0][0] if candidates else None
            if skill:
                logger.info(
                    f"🎯 Skill 匹配: {skill.meta.display_name} ({skill.meta.name}) | "
                    f"置信度: {candidates[0][1]:.0%} | "
                    f"候选: {[(s.meta.name, f'{c:.0%}') for s, c in candidates[:3]]}"
                )
            else:
                logger.info("🎯 无匹配 Skill，走 AgentLoop 自主执行")

        # 2. 构建 system_prompt
        system_prompt = self._get_base_system_prompt()
        skill_prompt = ""
        if skill and hasattr(skill, 'get_system_prompt'):
            skill_prompt = skill.get_system_prompt()
            if skill_prompt:
                system_prompt = (
                    f"你是 {skill.meta.display_name} 专家。\n\n"
                    f"{skill_prompt}\n\n"
                    f"{system_prompt}"
                )
                logger.debug(f"  合并技能提示: {len(skill_prompt)} 字符")
            else:
                logger.debug(f"  Skill {skill.meta.name}: skill.md 无 system 段")
        else:
            logger.debug(f"  Skill 无 get_system_prompt 方法")

        logger.info(f"📝 最终 system_prompt: ~{len(system_prompt)} 字符 | Skill: {skill.meta.display_name if skill else '无'}")

        # 3. AgentLoop 执行
        loop = AgentLoop(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            system_prompt=system_prompt,
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

    def _get_base_system_prompt(self) -> str:
        """获取基础系统提示词"""
        return """你是 Jarvis，一个强大的智能助手。

## 核心能力
你可以使用各种工具来完成任务。工具调用使用 OpenAI function calling 格式。

## 工具使用指南
- 当需要信息时，先使用搜索/读取工具获取信息
- 当需要修改文件时，使用编辑/写入工具
- 工具调用后，分析结果再决定下一步
- 如果工具失败，分析错误信息并尝试其他方式

## 效率原则（严格遵守）
- 一次只做一件事
- 不重复读同一个文件
- 能一步完成的不要分两步
- 工具调用后必须检查结果

## 常见任务模式
### 代码匹配/查找
1. 用 project_search 或 code_search 查找目标代码
2. 用 read_file 读取确认
3. 用 edit 或 write_file 修改

### Excel 数据处理
1. 用 excel 工具读取/分析
2. 处理完成后用 write_file 保存结果

### 文件分析
1. 用 read_file / read_pdf / read_image 读文件
2. 分析内容后给出结论

### 图片处理
1. 用 read_image 确认图片内容
2. 用 image_recognize 识别文字
3. 根据需求用工具处理

## 回答要求
- 用中文回答
- 给出清晰、具体的结论
- 列出操作步骤和结果

请根据用户的问题，逐步思考并使用合适的工具。"""
