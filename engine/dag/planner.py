"""
dag/planner.py — LLM 动态规划 DAG（增强模式）

在 Builder 模板无法覆盖的复杂场景下使用：
  - 条件分支（"如果...否则..."）
  - 多步骤流水线（"先...然后...再..."）
  - 对比分析（"对比A和B"）
  - 并行任务（"分别处理A和B"）

用法:
    planner = DAGPlanner(llm_client)
    if DAGPlanner.should_plan(user_input):
        plan = await planner.plan(user_input, tools_desc)
        graph = DAGFactory().build(plan)
    else:
        graph = builder.build(expert)  # 默认 Builder
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .graph import WorkflowGraph
from .node import LLMNode, ToolNode, RouterNode, ToolDispatchNode

logger = logging.getLogger(__name__)


DAG_PLANNER_PROMPT = """You are a DAG execution graph planner. Given the user's request and available tools, design the optimal execution graph.

## Available Tools
{tools_description}

## Output Format
Return ONLY valid JSON (no markdown code blocks, no explanation):
{{
  "plan": {{
    "nodes": [
      {{"name": "think", "type": "llm", "prompt": "system prompt for this node"}},
      {{"name": "router", "type": "router", "routes": {{"executing": "dispatch", "completed": "complete"}}}},
      {{"name": "dispatch", "type": "dispatch"}},
      {{"name": "complete", "type": "llm", "prompt": "final summarization prompt"}}
    ],
    "edges": [
      {{"from": "think", "to": "router"}},
      {{"from": "router", "to": "dispatch", "condition": "executing"}},
      {{"from": "router", "to": "complete", "condition": "completed"}},
      {{"from": "dispatch", "to": "think"}}
    ],
    "entry": "think",
    "exit": "complete"
  }}
}}

## Rules
1. Every graph MUST have a "think" node (LLM reasoning) and a "complete" node (LLM response)
2. If tools are needed, include router + dispatch + loop edge (dispatch → think)
3. Tool nodes are dynamically dispatched by dispatch node—do NOT create ToolNode manually
4. For multi-step pipelines, create sequential nodes: think → step1 → step2 → ... → complete
5. For conditional branches, add multiple route entries in router
6. For parallel tasks, add a ParallelNode with task descriptions
7. For simple Q&A, just think → complete (no router, no dispatch)

## User Request
{user_input}

JSON:"""


class DAGPlanner:
    """
    LLM 驱动的 DAG 规划器（增强模式）

    用于 Builder 模板无法覆盖的复杂场景。
    简单场景（单工具调用、问答）直接走 Builder，不走这里。
    """

    # 触发模式：匹配到任一模式就启动 LLM 规划
    PLANNER_TRIGGERS = [
        r"先.*然后.*再",       # 先读文件，然后识别，再重命名
        r"如果.*否则",          # 条件分支
        r"或者.*或者",          # 多选项
        r"对比.*和",            # 对比分析
        r"分别.*和",            # 分别处理A和B
        r"步骤",                # 明确的步骤
        r"流程",                # 复杂流程
        r"同时.*和",            # 并行任务
        r"先.*再",              # 先A再B
        r"然后",                # 然后（第二步）
        r"if.*else",            # 英文条件
        r"compare.*and",        # 英文对比
        r"step[s]?\s+\d+",      # Step 1, Step 2
        r"parallel|simultaneously|concurrently",  # 并行关键词
    ]

    def __init__(self, llm_client: Any):
        self.llm_client = llm_client

    @classmethod
    def should_plan(cls, user_input: str) -> bool:
        """
        判断是否需要 LLM 规划 DAG

        匹配触发模式之一 → True（需要 LLM 规划）
        否则 → False（默认 Builder 即可）
        """
        for pattern in cls.PLANNER_TRIGGERS:
            if re.search(pattern, user_input):
                logger.info(f"DAG 规划触发: pattern='{pattern}' input='{user_input[:60]}'")
                return True
        return False

    async def plan(
        self,
        user_input: str,
        tools_description: str = "",
        system_prompt: str = "",
    ) -> Dict[str, Any]:
        """
        让 LLM 规划 DAG 结构

        Args:
            user_input: 用户输入
            tools_description: 可用工具的描述文本
            system_prompt: 系统提示词（传给 think 和 complete 节点）

        Returns:
            {"plan": {...}, "reasoning": "..."}
        """
        prompt = DAG_PLANNER_PROMPT.format(
            tools_description=tools_description or "No tools available",
            user_input=user_input,
        )

        try:
            response = await self.llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                stream=False,
                temperature=0.3,
            )

            content = response.get("choices", [{}])[0].get("message", {}).get("content", "")

            # 清理 markdown 包裹
            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n", 1)
                if len(lines) > 1:
                    content = lines[1]
                if "```" in content:
                    content = content.rsplit("```", 1)[0]

            plan = json.loads(content)
            reasoning = plan.get("reasoning", "") if isinstance(plan, dict) else ""
            logger.info(f"DAG 规划完成: {len(plan.get('plan', plan).get('nodes', []))} 节点, {reasoning[:80]}")
            return plan

        except json.JSONDecodeError as e:
            logger.warning(f"LLM DAG 输出格式异常: {e}, 使用默认规划")
            return self._default_plan(system_prompt)
        except Exception as e:
            logger.error(f"DAG 规划失败: {e}")
            return self._default_plan(system_prompt)

    def _default_plan(self, system_prompt: str) -> Dict:
        """默认规划：标准 think → complete"""
        return {
            "plan": {
                "nodes": [
                    {"name": "think", "type": "llm", "prompt": system_prompt},
                    {"name": "complete", "type": "llm", "prompt": system_prompt},
                ],
                "edges": [
                    {"from": "think", "to": "complete"},
                ],
                "entry": "think",
                "exit": "complete",
            },
            "reasoning": "LLM 规划失败，使用默认简单问答流程",
        }


class DAGFactory:
    """
    DAG 工厂：将 LLM 规划 JSON 转换为 WorkflowGraph

    支持的节点类型：
      - llm:      LLMNode（需要 prompt 参数）
      - router:   RouterNode（需要 routes 参数）
      - dispatch: ToolDispatchNode
      - parallel: ParallelNode（需要 tasks 参数）

    注意：ToolNode 由 dispatch 动态调度，规划中不创建。
    """

    def build(self, plan_data: Dict) -> WorkflowGraph:
        """
        将规划 JSON 转换为可执行的 WorkflowGraph

        Args:
            plan_data: LLM 规划的 JSON，可以是 {"plan": {...}} 或直接 {...}

        Returns:
            WorkflowGraph
        """
        # 兼容两种格式
        if "plan" in plan_data:
            plan_data = plan_data["plan"]

        graph = WorkflowGraph("llm_planned")

        # 1. 创建节点
        for node_def in plan_data.get("nodes", []):
            name = node_def["name"]
            node_type = node_def.get("type", "llm")

            if node_type == "llm":
                prompt = node_def.get("prompt", "")
                graph.add_node(LLMNode(name=name, system_prompt=prompt))

            elif node_type == "router":
                routes = node_def.get("routes", {"executing": "dispatch", "completed": "complete"})
                if "default" not in routes:
                    routes["default"] = "complete"
                graph.add_node(RouterNode(name=name, routes=routes))

            elif node_type == "dispatch":
                graph.add_node(ToolDispatchNode(name=name))

            else:
                logger.warning(f"未知节点类型: {node_type}, 跳过节点 '{name}'")
                continue

        # 2. 创建边
        for edge_def in plan_data.get("edges", []):
            source = edge_def.get("from", "")
            target = edge_def.get("to", "")
            condition = edge_def.get("condition")

            if not source or not target:
                continue
            if source not in graph.nodes or target not in graph.nodes:
                logger.warning(f"跳过边: '{source}->{target}', 节点不存在")
                continue

            if condition:
                graph.add_conditional_edge(source, condition, target)
            else:
                graph.add_edge(source, target)

        # 3. 设置入口和出口
        entry = plan_data.get("entry", "think")
        exit_node = plan_data.get("exit", "complete")

        if entry in graph.nodes:
            graph.set_entry(entry)
        if exit_node in graph.nodes:
            graph.set_exit(exit_node)

        logger.info(f"DAG 构建完成: {len(graph.nodes)} 节点, {len(graph.edges)} 边")
        return graph


def describe_tools(tool_registry: Any) -> str:
    """
    生成工具描述文本（供 LLM 规划使用）

    Args:
        tool_registry: ToolRegistry 实例或任意具有 list_tools/get_schema 方法的对象

    Returns:
        格式化后的工具描述文本
    """
    if not tool_registry:
        return "No tools available"

    try:
        tools = tool_registry.list_tools()
        lines = []
        for name in tools[:20]:  # 最多 20 个工具，避免 token 爆炸
            schema = tool_registry.get_schema(name)
            if schema:
                params_desc = ""
                if hasattr(schema, 'parameters') and schema.parameters:
                    param_names = [p.get('name', p.name) if hasattr(p, 'name') else str(p)[:30] for p in schema.parameters[:5]]
                    params_desc = f" params: {', '.join(param_names)}"
                lines.append(f"- {name}: {schema.description}{params_desc}")
            else:
                lines.append(f"- {name}")
        return "\n".join(lines) if lines else "No tools available"
    except Exception as e:
        logger.warning(f"describe_tools failed: {e}")
        return "Tool description unavailable"