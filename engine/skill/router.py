"""
skill/router.py — Skill 路由器

负责：
 1. 接收用户输入 → 路由到最合适的 Skill
 2. 为 Skill 构建专属 DAG
 3. 执行并收集结果
 4. 更新 Skill 统计信息

替代原来的 ExpertOrchestrator，去掉 Expert 层。
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, AsyncIterator, Tuple

from ..dag.graph import WorkflowGraph
from ..dag.executor import GraphExecutor
from ..tool.registry import ToolRegistry
from .registry import SkillRegistry
from .base import Skill, SkillResult

logger = logging.getLogger(__name__)


class SkillRouter:
    """
    Skill 路由器 — 替代 ExpertOrchestrator

    流程:
    1. 用户输入 → SkillRegistry.route()
    2. 找到最佳 Skill → Skill.build_graph()
    3. 构建精简 ToolRegistry → GraphExecutor.run()
    4. 收集结果 → 更新 Skill 统计

    支持模式:
    - single: 单 Skill 执行（默认）
    - sequential: 多 Skill 顺序执行
    - debate: 多 Skill 并行 → LLM 汇总
    """

    def __init__(
        self,
        llm_client: Any,
        skill_registry: Optional[SkillRegistry] = None,
        default_mode: str = "single",
    ):
        self.llm_client = llm_client
        self.skill_registry = skill_registry or SkillRegistry()
        self.default_mode = default_mode

    # ── 公共 API ──

    async def process(
        self,
        user_input: str,
        skill_name: Optional[str] = None,
        mode: Optional[str] = None,
        history: Optional[Any] = None,
        enable_tracing: bool = True,
        **kwargs,
    ) -> SkillResult:
        """
        处理用户输入

        Args:
            user_input: 用户输入
            skill_name: 指定 Skill 名称（None = 自动路由）
            mode: 执行模式（single/sequential/debate）
            history: 消息历史
            enable_tracing: 是否启用追踪
            **kwargs: 传给 Skill.build_graph() 的额外参数

        Returns:
            SkillResult
        """
        mode = mode or self.default_mode

        # 1. 路由
        if skill_name:
            skill = self.skill_registry.route_exact(skill_name)
            if not skill:
                return SkillResult(
                    success=False,
                    error=f"Skill '{skill_name}' 不存在",
                )
            candidates = [(skill, 1.0)]
        else:
            candidates = self.skill_registry.route(user_input, top_k=3)
            if not candidates:
                return SkillResult(
                    success=False,
                    error="未找到合适的 Skill 处理该请求",
                )

        # 2. 执行
        if mode == "single" or len(candidates) == 1:
            return await self._run_single(
                candidates[0][0], user_input, history, enable_tracing, **kwargs
            )
        elif mode == "sequential":
            return await self._run_sequential(
                candidates, user_input, history, enable_tracing, **kwargs
            )
        elif mode == "debate":
            return await self._run_debate(
                candidates, user_input, history, enable_tracing, **kwargs
            )
        else:
            return SkillResult(success=False, error=f"未知模式: {mode}")

    # ── 执行模式 ──

    async def _run_single(
        self,
        skill: Skill,
        user_input: str,
        history: Optional[Any],
        enable_tracing: bool,
        **kwargs,
    ) -> SkillResult:
        """单 Skill 执行"""
        start_time = time.time()

        try:
            # 构建精简工具注册表
            mini_registry = ToolRegistry()
            for tool_cls in skill.required_tools:
                mini_registry.register(tool_cls)

            # 构建 Skill 专属 DAG
            graph = skill.build_graph(user_input=user_input, **kwargs)

            # 准备输入
            messages = self._prepare_messages(user_input, history)
            openai_tools = mini_registry.get_openai_tools()

            # 执行
            executor = GraphExecutor(self.llm_client, mini_registry)
            ctx = await executor.run(
                graph=graph,
                initial_input={
                    "messages": messages,
                    "tools": openai_tools,
                },
                enable_tracing=enable_tracing,
            )

            # 提取内容
            content = self._extract_content(ctx)

            duration_ms = (time.time() - start_time) * 1000

            result = SkillResult(
                success=True,
                content=content,
                data={"ctx": ctx},
                duration_ms=duration_ms,
            )

            # 更新统计
            await skill.on_success(ctx, result)

            logger.info(
                f"✅ Skill '{skill.meta.name}' 执行成功 "
                f"({duration_ms:.0f}ms)"
            )

            return result

        except Exception as e:
            logger.exception(f"❌ Skill '{skill.meta.name}' 执行失败: {e}")
            await skill.on_failure(e)

            return SkillResult(
                success=False,
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )

    async def _run_sequential(
        self,
        candidates: List[Tuple[Skill, float]],
        user_input: str,
        history: Optional[Any],
        enable_tracing: bool,
        **kwargs,
    ) -> SkillResult:
        """顺序执行多个 Skill"""
        results = []
        current_input = user_input

        for skill, _ in candidates[:3]:  # 最多3个
            result = await self._run_single(
                skill, current_input, history, enable_tracing, **kwargs
            )
            results.append(result)

            if not result.success:
                break

            # 前一个 Skill 的输出作为下一个的输入
            current_input = (
                f"上一个 Skill [{skill.meta.display_name}] 的处理结果:\n"
                f"{result.content[:2000]}\n\n"
                f"请基于以上结果继续处理。原始需求: {user_input}"
            )

        # 汇总结果
        if results:
            last = results[-1]
            last.data["sequential_results"] = results
            last.data["skills_used"] = [s.meta.name for s, _ in candidates[:len(results)]]
            return last

        return SkillResult(success=False, error="所有 Skill 执行失败")

    async def _run_debate(
        self,
        candidates: List[Tuple[Skill, float]],
        user_input: str,
        history: Optional[Any],
        enable_tracing: bool,
        **kwargs,
    ) -> SkillResult:
        """辩论模式 — 多 Skill 并行执行，LLM 汇总"""
        top_k = min(3, len(candidates))

        # 并行执行
        tasks = [
            self._run_single(skill, user_input, history, enable_tracing, **kwargs)
            for skill, _ in candidates[:top_k]
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 收集有效意见
        opinions = []
        for (skill, confidence), result in zip(candidates[:top_k], results):
            if isinstance(result, SkillResult) and result.success:
                opinions.append({
                    "skill": skill.meta.display_name,
                    "icon": skill.meta.icon,
                    "level": skill.experience_level.name,
                    "confidence": confidence,
                    "content": result.content,
                })

        if not opinions:
            return SkillResult(success=False, error="所有 Skill 执行失败")

        # LLM 汇总
        try:
            final_content = await self._summarize_opinions(user_input, opinions)
        except Exception as e:
            final_content = f"汇总失败: {e}\n\n" + "\n\n---\n\n".join(
                f"### {o['skill']}\n{o['content'][:500]}" for o in opinions
            )

        return SkillResult(
            success=True,
            content=final_content,
            data={
                "opinions": opinions,
                "skills_used": [o["skill"] for o in opinions],
            },
        )

    # ── 辅助方法 ──

    def _prepare_messages(
        self, user_input: str, history: Optional[Any]
    ) -> List[Dict]:
        """准备 LLM 消息"""
        if history is None:
            msgs = []
        elif hasattr(history, "get_for_llm"):
            msgs = history.get_for_llm()
        elif isinstance(history, list):
            msgs = list(history)
        else:
            msgs = []

        msgs.append({"role": "user", "content": user_input})
        return msgs

    def _extract_content(self, ctx) -> str:
        """从执行上下文提取内容"""
        try:
            # 尝试多个常见节点名称
            for node_name in ["complete", "think", "aggregate_results", "generate_report"]:
                output = ctx.get_node_output(node_name, "output")
                if output is not None:
                    data = output.data if hasattr(output, "data") else output
                    if isinstance(data, dict):
                        content = data.get("content", "")
                        if content:
                            return content
                    elif isinstance(data, str):
                        return data

            # 回退：取最后一个节点的输出
            all_outputs = ctx.get_all_node_outputs()
            if all_outputs:
                last_key = list(all_outputs.keys())[-1]
                last_output = all_outputs[last_key]
                if isinstance(last_output, dict):
                    for val in last_output.values():
                        if isinstance(val, str):
                            return val
                    return str(last_output)
                return str(last_output)

            return ""
        except Exception:
            return ""

    async def _summarize_opinions(
        self, user_input: str, opinions: List[dict]
    ) -> str:
        """LLM 汇总多个 Skill 的意见"""
        opinions_text = "\n\n".join(
            f"### {o['icon']} {o['skill']} ({o['level']}, 置信度:{o['confidence']:.0%})\n"
            f"{o['content'][:1500]}"
            for o in opinions
        )

        prompt = f"""你是 Supervisor，负责综合多个 Skill 的执行结果。

## 用户问题
{user_input}

## 各 Skill 执行结果
{opinions_text}

请综合以上结果，给出最终答案。如果有分歧，说明分歧点并给出判断。"""

        response = await self.llm_client.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            stream=False,
        )

        return (
            response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
