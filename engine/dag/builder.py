"""
dag/builder.py — Agent 图构建器

提供高级 API 来构建常用 Agent DAG：
  - standard_agent:  标准「思考→路由→执行→反思」循环
  - simple_agent:    单次 LLM 调用（无工具）
  - react_agent:     ReAct 模式（思考→行动→观察→思考...）
  - plan_execute:    规划→批量执行→验证
  - self_reflection: 生成→自评→修正→生成...
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from .node import (
    Node, LLMNode, ToolNode, RouterNode, ToolDispatchNode,
    ListFilesNode, FileProcessorNode, FileRenameNode, MapNode,
    CodeSearchNode, CodeEditorNode, HumanInLoopNode,
)
from .edge import Edge, ConditionalEdge
from .graph import WorkflowGraph
from ..message.message_list import MessageList
from .batch_processors import get_processor, list_processors

logger = logging.getLogger(__name__)


class AgentGraphBuilder:
    """
    Agent 图构建器

    提供预定义的 DAG 模板，同时支持自定义扩展。
    """

    def __init__(
        self,
        llm_client: Any,
        tool_registry: Any,
        system_prompt: str = "",
        max_steps: int = 10,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.system_prompt = system_prompt
        self.max_steps = max_steps

    # ─────────────────────────────────────────
    #  模板：标准 Agent 循环
    # ─────────────────────────────────────────

    def build_standard_agent(
        self,
        user_input: str = "",
        history: Optional[MessageList] = None,
    ) -> WorkflowGraph:
        """
        构建标准 Agent 循环图

        流程:
          think → router → [tools] → think (循环)
                          → complete (出口)

        think: LLM 调用
        router: 判断 LLM 返回了 tool_calls 还是最终回答
        tools: 自动为每个注册工具创建 ToolNode
        complete: 输出最终回答
        """
        graph = WorkflowGraph("standard_agent")

        think = LLMNode(name="think", system_prompt=self.system_prompt)
        router = RouterNode(name="router", routes={
            "executing": "tool_dispatch",
            "completed": "complete",
        })
        dispatch = ToolDispatchNode(name="tool_dispatch")
        tool_nodes = self._create_tool_nodes()
        complete = LLMNode(name="complete", system_prompt=self.system_prompt)

        graph.add_node(think)
        graph.add_node(router)
        graph.add_node(dispatch)
        for tn in tool_nodes:
            graph.add_node(tn)
        graph.add_node(complete)

        # 边（包含循环边标记）
        graph.add_edge("think", "router")
        graph.add_conditional_edge("router", "executing", "tool_dispatch")
        graph.add_conditional_edge("router", "completed", "complete")

        for tn in tool_nodes:
            graph.add_edge(
                "tool_dispatch", tn.name,
                condition=lambda outputs, n=tn.tool_name: self._check_tool_condition(outputs, n),
            )
            edge = graph.add_edge(tn.name, "think")
            graph.mark_as_loop_edge(edge)  # 标记为合法循环

        graph.set_entry("think")
        graph.set_exit("complete")
        graph._meta = self._meta(user_input, history)
        return graph

    # ─────────────────────────────────────────
    #  模板：单次 LLM 调用
    # ─────────────────────────────────────────

    def build_simple_agent(self) -> WorkflowGraph:
        """简单 Agent：一次 LLM 调用直接返回"""
        graph = WorkflowGraph("simple_agent")
        think = LLMNode(name="think", system_prompt=self.system_prompt)
        complete = LLMNode(name="complete", system_prompt=self.system_prompt)
        graph.add_node(think)
        graph.add_node(complete)
        graph.add_edge("think", "complete")
        graph.set_entry("think")
        graph.set_exit("complete")
        return graph

    # ─────────────────────────────────────────
    #  模板：ReAct Agent
    # ─────────────────────────────────────────

    def build_react_agent(
        self,
        user_input: str = "",
        history: Optional[MessageList] = None,
        max_iterations: int = 5,
    ) -> WorkflowGraph:
        """
        构建 ReAct Agent

        流程:
          think → router → [tool_action] → observe → think (循环)
                          → complete (出口)

        与 standard_agent 的区别：
        - 有 observe 节点专门汇总工具结果
        - 显式区分"思考→行动→观察"每一步
        """
        graph = WorkflowGraph("react_agent")

        think = LLMNode(name="think", system_prompt=self.system_prompt)
        router = RouterNode(name="router", routes={
            "executing": "tool_action",
            "completed": "complete",
        })
        tool_action = ToolDispatchNode(name="tool_action")
        tool_nodes = self._create_tool_nodes()
        observe = LLMNode(
            name="observe",
            system_prompt="根据工具执行结果进行观察和总结，然后继续推理。",
        )
        complete = LLMNode(name="complete", system_prompt=self.system_prompt)

        graph.add_node(think)
        graph.add_node(router)
        graph.add_node(tool_action)
        for tn in tool_nodes:
            graph.add_node(tn)
        graph.add_node(observe)
        graph.add_node(complete)

        # 边
        graph.add_edge("think", "router")
        graph.add_conditional_edge("router", "executing", "tool_action")
        graph.add_conditional_edge("router", "completed", "complete")

        for tn in tool_nodes:
            graph.add_edge(
                "tool_action", tn.name,
                condition=lambda outputs, n=tn.tool_name: self._check_tool_condition(outputs, n),
            )
            graph.add_edge(tn.name, "observe")

        edge = graph.add_edge("observe", "think")
        graph.mark_as_loop_edge(edge)

        graph.set_entry("think")
        graph.set_exit("complete")
        graph._meta = self._meta(user_input, history, max_iterations=max_iterations)
        return graph

    # ─────────────────────────────────────────
    #  模板：Plan-Execute
    # ─────────────────────────────────────────

    def build_plan_execute(
        self,
        user_input: str = "",
        history: Optional[MessageList] = None,
    ) -> WorkflowGraph:
        """
        构建 Plan-Execute Agent

        流程:
          plan → dispatch → [tool_1, tool_2, ...] → aggregate → verify
              → [success] → complete
              → [retry] → plan (重新规划)

        plan: LLM 将需求拆解为多步计划
        dispatch: 分发所有步骤到对应工具
        aggregate: 汇总所有工具结果
        verify: 检查执行结果是否符合预期
        """
        graph = WorkflowGraph("plan_execute")

        plan = LLMNode(
            name="plan",
            system_prompt=self.system_prompt + (
                "\n\n请将用户需求拆解为具体的执行步骤。"
                "输出格式为可执行的 tool_calls 列表。"
            ),
        )
        dispatch = ToolDispatchNode(name="dispatch")
        tool_nodes = self._create_tool_nodes()
        aggregate = LLMNode(
            name="aggregate",
            system_prompt="汇总所有工具的执行结果。",
        )
        verify = LLMNode(
            name="verify",
            system_prompt="验证所有工具执行结果是否符合预期。如果符合输出 success，否则输出 retry（附带原因）。",
        )
        plan_router = RouterNode(name="plan_router", routes={
            "success": "complete",
            "retry": "plan",
        })
        complete = LLMNode(name="complete", system_prompt=self.system_prompt)

        graph.add_node(plan)
        graph.add_node(dispatch)
        for tn in tool_nodes:
            graph.add_node(tn)
        graph.add_node(aggregate)
        graph.add_node(verify)
        graph.add_node(plan_router)
        graph.add_node(complete)

        # 边
        graph.add_edge("plan", "dispatch")
        for tn in tool_nodes:
            graph.add_edge(
                "dispatch", tn.name,
                condition=lambda outputs, n=tn.tool_name: self._check_tool_condition(outputs, n),
            )
            graph.add_edge(tn.name, "aggregate")
        graph.add_edge("aggregate", "verify")
        graph.add_edge("verify", "plan_router")
        graph.add_conditional_edge("plan_router", "success", "complete")
        edge = graph.add_conditional_edge("plan_router", "retry", "plan")
        graph.mark_as_loop_edge(edge)

        graph.set_entry("plan")
        graph.set_exit("complete")
        graph._meta = self._meta(user_input, history)
        return graph

    # ─────────────────────────────────────────
    #  模板：Self-Reflection
    # ─────────────────────────────────────────

    def build_self_reflection(
        self,
        user_input: str = "",
        history: Optional[MessageList] = None,
        max_reflections: int = 3,
    ) -> WorkflowGraph:
        """
        构建 Self-Reflection Agent

        流程:
          generate → reflect → [pass] → complete
                              → [fail] → revise → generate (循环)

        generate: 生成初始回答
        reflect: 审查质量（输出 pass 或 fail + 修改建议）
        revise: 根据反馈修改
        complete: 输出最终结果
        """
        graph = WorkflowGraph("self_reflection")

        generate = LLMNode(name="generate", system_prompt=self.system_prompt)
        reflect = LLMNode(
            name="reflect",
            system_prompt=(
                "请审查上一步的输出质量。从以下维度评估：\n"
                "1. 准确性\n2. 完整性\n3. 清晰度\n\n"
                "输出格式：\n"
                "- 如果通过：pass\n"
                "- 如果需要修改：fail（附带具体的修改建议）"
            ),
        )
        revise = LLMNode(
            name="revise",
            system_prompt="根据反思中的修改建议改进输出。",
        )
        reflection_router = RouterNode(name="reflection_router", routes={
            "pass": "complete",
            "fail": "revise",
        })
        complete = LLMNode(name="complete", system_prompt=self.system_prompt)

        graph.add_node(generate)
        graph.add_node(reflect)
        graph.add_node(revise)
        graph.add_node(reflection_router)
        graph.add_node(complete)

        graph.add_edge("generate", "reflect")
        graph.add_edge("reflect", "reflection_router")
        graph.add_conditional_edge("reflection_router", "pass", "complete")
        graph.add_conditional_edge("reflection_router", "fail", "revise")
        edge = graph.add_edge("revise", "generate")
        graph.mark_as_loop_edge(edge)

        graph.set_entry("generate")
        graph.set_exit("complete")
        graph._meta = self._meta(user_input, history, max_reflections=max_reflections)
        return graph

    # ─────────────────────────────────────────
    #  模板：代码编辑工作流
    # ─────────────────────────────────────────

    def build_code_edit_workflow(
        self,
        task_description: str = "",
        base_dir: str = ".",
    ) -> WorkflowGraph:
        """
        构建代码编辑工作流

        自动生成:
          search_code → editor → [human_review] → confirm

        流程:
          1. code_search: 搜索相关代码
          2. editor: 读取/差异/编辑/回滚
          3. human_review: 人工审查修改
          4. confirm: 确认完成

        Args:
            task_description: 任务描述（元数据）
            base_dir: 项目根目录
        """
        graph = WorkflowGraph("code_edit")

        search = CodeSearchNode(name="search_code", base_dir=base_dir)
        editor = CodeEditorNode(name="editor", base_dir=base_dir)
        review = HumanInLoopNode(name="review_changes")
        confirm = LLMNode(
            name="confirm",
            system_prompt="确认所有修改已完成。",
        )

        graph.add_node(search)
        graph.add_node(editor)
        graph.add_node(review)
        graph.add_node(confirm)

        graph.add_edge("search_code", "editor")
        graph.add_edge("editor", "review_changes")
        graph.add_edge("review_changes", "confirm")

        graph.set_entry("search_code")
        graph.set_exit("confirm")
        graph._meta = self._meta(task_description)

        return graph

    # ─────────────────────────────────────────
    #  内部帮助方法
    # ─────────────────────────────────────────

    def _create_tool_nodes(self) -> List[ToolNode]:
        """为所有注册工具创建 ToolNode 列表"""
        return [
            ToolNode(name=f"tool_{name}", tool_name=name)
            for name in self.tool_registry.list_tools()
        ]

    @staticmethod
    def _check_tool_condition(outputs: dict, tool_name: str) -> bool:
        """检查工具分发条件"""
        route_val = outputs.get("route")
        if hasattr(route_val, 'data'):
            route_val = route_val.data
        return str(route_val) == tool_name

    # ─────────────────────────────────────────
    #  模板：批量文件处理
    # ─────────────────────────────────────────

    def build_batch_process(
        self,
        folder_path: str = ".",
        processor_name: str = "file_exists",
        post_action: str = "rename",
        file_patterns: str = ".jpg,.png,.pdf",
        dry_run: bool = True,
        max_parallel: int = 10,
    ) -> WorkflowGraph:
        """
        构建批量文件处理 DAG

        自动生成:
          list_files → map(processor) → aggregate → [rename] → report

        流程:
          1. list_files: 列出目录中匹配的文件
          2. map(processor): 对每个文件并行执行处理器
          3. aggregate: 汇总所有结果
          4. rename: 根据结果重命名文件（条件执行）
          5. report: 生成总结报告

        Args:
            folder_path: 目标文件夹
            processor_name: 已注册的处理器名称
            post_action: 后置操作（"rename" / "none"）
            file_patterns: 文件匹配模式，逗号分隔
            dry_run: 试运行模式（不实际重命名）
            max_parallel: 最大并行处理数
        """
        graph = WorkflowGraph(f"batch_{processor_name}")

        # ── 获取处理器 ──
        processor_func = get_processor(processor_name)
        if processor_func is None:
            available = list(list_processors().keys())
            raise ValueError(
                f"处理器 '{processor_name}' 未注册. "
                f"可选: {available}"
            )

        # ── 节点定义 ──
        list_files = ListFilesNode(name="list_files")

        file_processor = FileProcessorNode(
            name="process_file",
            processor_func=processor_func,
            timeout=120.0,
        )

        map_node = MapNode(
            name="map_process",
            sub_node=file_processor,
            map_key="item",
        )

        aggregate = LLMNode(
            name="aggregate_results",
            system_prompt=(
                "汇总所有文件处理结果，统计成功/失败数量，"
                "列出关键信息（文件名、状态）。"
            ),
        )

        rename = FileRenameNode(name="rename_files")

        report = LLMNode(
            name="generate_report",
            system_prompt=self.system_prompt,
        )

        # ── 注册节点 ──
        graph.add_node(list_files)
        graph.add_node(map_node)
        graph.add_node(aggregate)

        if post_action == "rename":
            graph.add_node(rename)

        graph.add_node(report)

        # ── 边 ──
        graph.add_edge("list_files", "map_process")
        graph.add_edge("map_process", "aggregate_results")

        if post_action == "rename":
            graph.add_edge("aggregate_results", "rename_files")
            graph.add_edge("rename_files", "generate_report")
        else:
            graph.add_edge("aggregate_results", "generate_report")

        graph.set_entry("list_files")
        graph.set_exit("generate_report")

        # ── 元数据 ──
        graph._meta = {
            "folder_path": folder_path,
            "processor_name": processor_name,
            "post_action": post_action,
            "dry_run": dry_run,
            "max_parallel": max_parallel,
            "file_patterns": file_patterns,
        }

        return graph

    @staticmethod
    def _meta(user_input: str = "", history=None, **extra) -> dict:
        """构建元数据字典"""
        meta = {"user_input": user_input}
        if history:
            meta["history"] = history
        meta.update(extra)
        return meta
