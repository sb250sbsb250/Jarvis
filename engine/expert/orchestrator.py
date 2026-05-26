"""
多专家编排器 — 自动路由和执行领域专家 DAG

ExpertOrchestrator 负责:
  1. 分析用户意图 → 路由到最合适的专家
  2. 为专家构建专属 DAG（精简工具集）
  3. 执行并返回结果

三种协作模式:
  - single:     单专家模式（默认），找一个最合适的专家执行
  - sequential: 顺序协作，多个专家依次处理
  - debate:     辩论模式，多专家并行回答，Supervisor 汇总
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, AsyncIterator

from engine.dag.graph import WorkflowGraph
from engine.dag.executor import GraphExecutor
from engine.dag.node import (
    LLMNode,
    RouterNode,
    ToolDispatchNode,
    ToolNode,
)
from engine.tool.registry import ToolRegistry
from engine.dag.planner import DAGPlanner, DAGFactory, describe_tools
from engine.longterm import TopicStore, Injector, compress_dialogue, should_compress
from .registry import ExpertRegistry
from .base import ExpertAgent

logger = logging.getLogger(__name__)


class ExpertOrchestrator:
    """
    多专家编排器（带 Topic 记忆系统）

    Args:
        llm_client: LLM 客户端实例
        expert_registry: 专家注册中心
        default_mode: 默认协作模式（single/sequential/debate）
        topic_store: TopicStore 实例（None = 自动创建）
        memory_enabled: 是否启用记忆注入
    """

    def __init__(
        self,
        llm_client: Any,
        expert_registry: ExpertRegistry,
        default_mode: str = "single",
        topic_store: Optional[TopicStore] = None,
        memory_enabled: bool = True,
    ):
        self.llm_client = llm_client
        self.expert_registry = expert_registry
        self.default_mode = default_mode
        self.memory_enabled = memory_enabled

        # Topic 记忆系统
        # DAG 规划器（增强模式）
        self.planner = DAGPlanner(llm_client)
        self.dag_factory = DAGFactory()

        # Topic 记忆系统
        self.topic_store = topic_store or TopicStore(
            db_path=os.path.join(
                os.path.dirname(__file__), "..", "..", "data", "topics.db"
            )
        )
        self.injector = Injector(self.topic_store)
        self._step_count = 0  # 用于判断何时压缩

    async def process(
        self,
        user_input: str,
        history: Optional[Any] = None,
        mode: Optional[str] = None,
        enable_tracing: bool = True,
    ) -> Dict[str, Any]:
        """
        处理用户输入，自动路由到最合适的专家。

        Args:
            user_input: 用户输入文本
            history: 可选的消息历史（MessageList 或 dict 列表）
            mode: 协作模式，None 使用 default_mode
            enable_tracing: 是否启用追踪

        Returns:
            {
                "success": bool,
                "expert": str,
                "confidence": float,
                "content": str,
                "trace": dict,
                "mode": str,
            }
        """
        mode = mode or self.default_mode

        # 1. 路由：找最合适的专家
        candidates = self.expert_registry.route(user_input, top_k=3)
        if not candidates:
            return {"success": False, "error": "无法找到合适的专家", "mode": mode}

        best_expert, confidence = candidates[0]
        logger.info(
            f"路由: {best_expert.domain.display_name} "
            f"(置信度: {confidence:.0%}, 模式: {mode})"
        )

        if mode == "single" or len(candidates) == 1:
            return await self._run_single(best_expert, user_input, history, enable_tracing)

        if mode == "sequential":
            return await self._run_sequential(candidates, user_input, history, enable_tracing)

        if mode == "debate":
            return await self._run_debate(candidates, user_input, history, enable_tracing)

        return {"success": False, "error": f"未知模式: {mode}", "mode": mode}

    async def _run_single(
        self,
        expert: ExpertAgent,
        user_input: str,
        history: Optional[Any],
        enable_tracing: bool,
    ) -> Dict[str, Any]:
        """单专家执行——构建专属 DAG 并运行（带 Topic 记忆系统）"""
        # ⭐ 记忆注入：检索相关 Topic 并注入到 LLM 上下文
        effective_input = user_input
        memory_block = ""
        if self.memory_enabled:
            # 处理负反馈
            was_feedback, remaining = self.injector.handle_feedback(user_input)
            if was_feedback:
                self.injector.apply_feedback(remaining)
                effective_input = remaining
                logger.info(f"Feedback detected, adjusted input: '{effective_input}'")

            # 准备记忆注入
            memory_block = self.injector.prepare_injection(effective_input)
            if memory_block:
                logger.info(f"Memory injection: {len(memory_block)} chars")

        # 构建精简工具注册表（只注册该专家的工具）
        mini_registry = ToolRegistry()
        for tool_cls in expert.tools:
            mini_registry.register(tool_cls)

        # 构建专属 DAG（Builder 默认 + Planner 增强）
        if DAGPlanner.should_plan(effective_input):
            tools_desc = describe_tools(mini_registry)
            plan = await self.planner.plan(
                user_input=effective_input,
                tools_description=tools_desc,
                system_prompt=expert.system_prompt,
            )
            graph = self.dag_factory.build(plan)
            logger.info(f"📐 LLM 规划 DAG: {len(graph.nodes)} 节点, {len(graph.edges)} 边")
        else:
            graph = self._build_expert_graph(expert)

        # 准备输入（注入记忆块到 system 位置）
        messages = self._prepare_messages(effective_input, history, memory_block)
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

        # 提取结果
        content = self._extract_content(ctx)

        # ⭐ 对话压缩：积累到阈值时自动提取 Topic
        self._step_count += 1
        if self.memory_enabled and history is not None:
            try:
                hist_len = len(history) if hasattr(history, '__len__') else 0
                if should_compress(hist_len, self._step_count):
                    history_dicts = self._history_to_dicts(history)
                    await compress_dialogue(
                        history_dicts, self.llm_client, self.topic_store
                    )
                    self._step_count = 0
            except Exception as e:
                logger.warning(f"Auto-compress failed: {e}")

        return {
            "success": True,
            "expert": expert.domain.display_name,
            "confidence": self.expert_registry.route(effective_input, top_k=1)[0][1],
            "content": content,
            "trace": ctx.get_summary() if enable_tracing else {},
            "mode": "single",
            "memory_injected": bool(memory_block),
        }

    async def _run_sequential(
        self,
        candidates: List[tuple],
        user_input: str,
        history: Optional[Any],
        enable_tracing: bool,
    ) -> Dict[str, Any]:
        """顺序协作——每个专家依次处理，前一个输出作为后一个输入"""
        results = []
        current_input = user_input
        all_traces = []

        for expert, _ in candidates:
            result = await self._run_single(expert, current_input, history, enable_tracing)
            results.append(
                {"expert": result["expert"], "content": result.get("content", "")}
            )
            if result.get("trace"):
                all_traces.append(result["trace"])
            # 将上一专家的输出传递给下一专家
            prev_content = result.get("content", "")
            current_input = (
                f"【上一个专家 - {result['expert']} 的处理结果】\n"
                f"{prev_content[:2000]}\n\n"
                f"请基于以上结果继续处理。原始用户需求: {user_input}"
            )

        return {
            "success": True,
            "mode": "sequential",
            "experts": [e.domain.display_name for e, _ in candidates],
            "results": results,
            "final_content": results[-1]["content"] if results else "",
            "traces": all_traces,
        }

    async def _run_debate(
        self,
        candidates: List[tuple],
        user_input: str,
        history: Optional[Any],
        enable_tracing: bool,
    ) -> Dict[str, Any]:
        """辩论模式——最多 3 个专家并行回答，Supervisor 汇总"""
        top_k = min(3, len(candidates))

        # 并行执行所有专家
        tasks = [
            self._run_single(expert, user_input, history, enable_tracing)
            for expert, _ in candidates[:top_k]
        ]
        expert_results = await asyncio.gather(*tasks, return_exceptions=True)

        # 收集有效的专家意见
        opinions = []
        for (expert, confidence), result in zip(candidates[:top_k], expert_results):
            if isinstance(result, dict) and result.get("success"):
                opinions.append(
                    {
                        "expert": expert.domain.display_name,
                        "confidence": confidence,
                        "opinion": result.get("content", ""),
                    }
                )

        if not opinions:
            return {"success": False, "error": "所有专家回答失败", "mode": "debate"}

        # Supervisor 汇总——让 LLM 综合多个专家的意见
        final_content = await self._supervisor_summarize(user_input, opinions)

        return {
            "success": True,
            "mode": "debate",
            "experts": [o["expert"] for o in opinions],
            "opinions": opinions,
            "final_content": final_content,
        }

    # ── 内部辅助方法 ──

    def _build_expert_graph(self, expert: ExpertAgent) -> WorkflowGraph:
        """构建专家专属 DAG（复用 AgentGraphBuilder 的标准 agent 模式）"""
        graph = WorkflowGraph(f"expert_{expert.domain.name}")

        think = LLMNode(name="think", system_prompt=expert.system_prompt)
        router = RouterNode(
            name="router",
            routes={"executing": "tool_dispatch", "completed": "complete"},
        )
        dispatch = ToolDispatchNode(name="tool_dispatch")
        complete = LLMNode(name="complete", system_prompt=expert.system_prompt)

        for node in [think, router, dispatch, complete]:
            graph.add_node(node)

        # 核心链
        graph.add_edge("think", "router")
        graph.add_conditional_edge("router", "executing", "tool_dispatch")
        graph.add_conditional_edge("router", "completed", "complete")

        # 循环边（think → ... → tool → think）
        graph.add_edge("tool_dispatch", "think")
        graph.set_entry("think")
        graph.set_exit("complete")

        return graph

    def _prepare_messages(
        self, user_input: str, history: Optional[Any],
        memory_block: str = "",
    ) -> List[Dict]:
        """准备 LLM 输入消息（支持记忆注入）"""
        if history is None:
            msgs = []
        elif hasattr(history, "get_for_llm"):
            msgs = history.get_for_llm()
        elif isinstance(history, list):
            msgs = list(history)
        else:
            msgs = []

        # 注入记忆块到 system 消息之后，第一条 user 消息之前
        if memory_block:
            msgs.insert(0, {"role": "system", "content": memory_block})

        msgs.append({"role": "user", "content": user_input})
        return msgs

    def _extract_content(self, ctx) -> str:
        """从执行上下文中提取 LLM 输出内容"""
        try:
            output = ctx.get_node_output("think", "output")
            if output is None:
                return ""
            data = output.data if hasattr(output, "data") else output
            if isinstance(data, dict):
                return data.get("content", "") or data.get("text", "") or str(data)
            return str(data)
        except Exception:
            return ""

    @staticmethod
    def _history_to_dicts(history: Any) -> List[Dict]:
        """将历史转换为 dict 列表（供 compress_dialogue 使用）"""
        if hasattr(history, "get_all"):
            return [
                {"role": m.role.value if hasattr(m, "role") else "user",
                 "content": m.content or ""}
                for m in history.get_all()
                if m.content
            ]
        if isinstance(history, list):
            return [
                {"role": m.get("role", "user"), "content": m.get("content", "")}
                for m in history if m.get("content")
            ]
        return []

    async def _supervisor_summarize(
        self, user_input: str, opinions: List[dict]
    ) -> str:
        """Supervisor LLM 综合多个专家意见"""
        opinions_text = "\n\n".join(
            f"### {o['expert']} (置信度: {o['confidence']:.0%})\n{o['opinion'][:1500]}"
            for o in opinions
        )

        supervisor_prompt = f"""你是一个 Supervisor Agent，负责协调多个领域专家的意见。

## 用户问题
{user_input}

## 各专家意见
{opinions_text}

请综合以上意见，给出一个统一、全面、准确的最终答案。
如果专家意见有分歧，说明分歧点并给出你的判断。
如果专家意见一致，给出完整的综合答案。
"""

        messages = [{"role": "user", "content": supervisor_prompt}]
        response = await self.llm_client.chat_completion(messages=messages)
        content = (
            response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        return content
