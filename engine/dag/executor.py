"""
dag/executor.py — DAG 执行器（核心引擎）

GraphExecutor 负责：
  1. 接收 WorkflowGraph + 初始输入
  2. 按拓扑顺序执行节点
  3. 管理节点间数据流（端口映射）
  4. 支持条件边和并行执行
  5. 追踪每个节点的执行记录
  6. 检查点/断点恢复
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from typing import (
    Any, Callable, Dict, List, Optional, Set,
    Tuple, AsyncIterator, Coroutine,
)
from uuid import uuid4

from .node import Node, NodeInput, NodeOutput, HumanInLoopNode
from .edge import Edge, ConditionalEdge, AllPassEdge
from .graph import WorkflowGraph
from .context import ExecutionContext
from .tracer import tracer as _tracer

logger = logging.getLogger(__name__)


class HumanInterruptError(Exception):
    """人工中断异常（用于 HumanInLoopNode 等待审批时）"""
    pass


class GraphExecutor:
    """
    DAG 图执行器

    使用基于就绪条件的异步调度算法：
    - 节点就绪后立即启动，支持并行
    - 使用 FIRST_COMPLETED 等待策略
    - 自动管理节点间数据流

    流式与非流式使用同一调度核心 _schedule_graph，
    通过 event_callback 参数控制是否发送事件。
    """

    def __init__(
        self,
        llm_client: Any,
        tool_registry: Any,
        max_parallel: int = 10,
        default_node_timeout: float = 60.0,
        enable_compiler: bool = True,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.max_parallel = max_parallel
        self.default_node_timeout = default_node_timeout
        self._compiler = None
        self._compile_history: Dict[str, float] = {}
        self.enable_compiler = enable_compiler

    def _get_compiler(self):
        """懒加载编译器"""
        if self._compiler is None and self.enable_compiler:
            from .compiler import GraphCompiler
            self._compiler = GraphCompiler(history=self._compile_history)
        return self._compiler

    # ═══════════════════════════════════════
    #  公共 API
    # ═══════════════════════════════════════

    async def run(
        self,
        graph: WorkflowGraph,
        initial_input: Any,
        session: Optional[Any] = None,
        checkpoint: Optional[Dict] = None,
        timeout: Optional[float] = None,
        enable_tracing: bool = True,
    ) -> ExecutionContext:
        """
        执行 DAG（非流式）

        Args:
            graph: 要执行的图
            initial_input: 初始输入
            session: 可选的会话（用于存储检查点）
            checkpoint: 可选的检查点（用于恢复执行）
            timeout: 整体超时（秒）
            enable_tracing: 是否启用追踪摘要输出

        Returns:
            ExecutionContext（包含所有节点输出和追踪）
        """
        # 编译优化
        compiler = self._get_compiler()
        if compiler:
            compiled = compiler.compile(graph)
            if compiled.dead_nodes:
                logger.info(f"编译: 移除 {len(compiled.dead_nodes)} 个死代码节点")
            if compiled.fused_chains:
                logger.info(f"编译: 折叠 {len(compiled.fused_chains)} 条纯函数链")
        else:
            compiled = None

        ctx = self._prepare_context(graph, session, checkpoint)
        run_timeout = timeout or 300.0

        # 启动追踪
        _tracer.start_trace(ctx.request_id)
        _tracer.start_span(ctx.request_id, graph.name, "graph")

        try:
            result = await asyncio.wait_for(
                self._schedule_graph(ctx, graph, initial_input, session, event_callback=None),
                timeout=run_timeout,
            )

            # 编译历史校准（记录实际耗时）
            self._update_compile_history(result)

            _tracer.end_span(ctx.request_id)
            if enable_tracing:
                _tracer.print_summary(ctx.request_id)
            return result

        except asyncio.TimeoutError:
            logger.error(f"DAG 执行超时 ({run_timeout}s)")
            ctx.record("timeout", node="__graph__", timeout=run_timeout)
            _tracer.end_span(ctx.request_id, error=f"timeout ({run_timeout}s)")
            return ctx

    async def run_stream(
        self,
        graph: WorkflowGraph,
        initial_input: Any,
        session: Optional[Any] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        流式执行 DAG，逐步 yield 事件。

        事件类型:
          - node_start: 节点开始执行
          - node_complete: 节点执行完成
          - node_error: 节点出错
          - thought: 思考状态（LLM 节点）
          - tool_call: 工具调用事件
          - complete: 全部完成
        """
        ctx = self._prepare_context(graph, session)

        _tracer.start_trace(ctx.request_id)
        _tracer.start_span(ctx.request_id, graph.name, "graph")

        errors = graph.validate()
        if errors:
            yield {"type": "error", "content": "图验证失败: " + "; ".join(errors)}
            return

        event_queue: asyncio.Queue = asyncio.Queue()

        async def execute_and_enqueue():
            try:
                async def event_callback(event_type: str, **kwargs):
                    await event_queue.put({"type": event_type, **kwargs})
                await self._schedule_graph(ctx, graph, initial_input, session, event_callback=event_callback)
            except Exception as e:
                await event_queue.put({"type": "error", "content": str(e)})
            finally:
                await event_queue.put({"type": "__done__"})

        exec_task = asyncio.create_task(execute_and_enqueue())

        while True:
            event = await event_queue.get()
            if event.get("type") == "__done__":
                break
            yield event

        await exec_task
        _tracer.end_span(ctx.request_id)
        _tracer.print_summary(ctx.request_id)
        yield {"type": "complete", "summary": ctx.get_summary()}

    # ═══════════════════════════════════════
    #  统一的调度核心
    # ═══════════════════════════════════════

    async def _schedule_graph(
        self,
        ctx: ExecutionContext,
        graph: WorkflowGraph,
        initial_input: Any,
        session: Optional[Any],
        event_callback: Optional[Callable[..., Coroutine]] = None,
    ) -> ExecutionContext:
        """
        统一的 DAG 调度核心。

        Args:
            event_callback: 可选的事件回调，async def cb(event_type: str, **kwargs)
                           为 None 时不发送事件（非流式模式）
        """
        # ── 状态管理 ──
        pending: Set[str] = set(graph.nodes.keys())
        completed: Set[str] = set()
        running: Dict[str, asyncio.Task] = {}
        node_inputs: Dict[str, Dict[str, Tuple[str, str]]] = defaultdict(dict)
        node_outputs: Dict[str, Dict[str, NodeOutput]] = {}

        # 构建邻接表和反向依赖
        adjacency, reverse_deps = self._build_adjacency(graph)

        # 就绪队列（元素为 (node_name, is_reentry)）
        ready: deque = deque()

        # 初始化入口节点
        for entry in graph.entry_points:
            if entry in pending:
                if isinstance(initial_input, dict):
                    for key in initial_input:
                        node_inputs[entry][key] = ("__input__", key)
                else:
                    node_inputs[entry]["default"] = ("__input__", "default")
                ready.append((entry, False))

        # 注入初始输入
        if isinstance(initial_input, dict):
            ctx.set_node_output("__input__", dict(initial_input))
        else:
            ctx.set_node_output("__input__", {"default": initial_input})

        # ── 主调度循环 ──
        try:
            while ready or running:
                # 启动所有就绪节点
                while ready:
                    node_name, is_reentry = ready.popleft()
                    if node_name in running:
                        continue
                    # 对于首次执行，从 pending 移除；重入执行直接跑
                    if not is_reentry:
                        if node_name not in pending:
                            continue
                        pending.discard(node_name)
                    elif node_name not in completed and node_name in pending:
                        # 重入时首次跑补移除
                        pending.discard(node_name)

                    inputs = self._collect_inputs(ctx, graph, node_name, node_inputs, node_outputs)

                    if event_callback:
                        await event_callback(
                            "node_start",
                            node=node_name,
                            node_type=graph.nodes[node_name].node_type,
                        )

                    sem = asyncio.Semaphore(self.max_parallel)

                    async def run_node(n_name: str, n_inputs: Dict[str, NodeInput]):
                        async with sem:
                            return await self._execute_node(
                                ctx, graph, n_name, n_inputs, event_callback,
                            )

                    task = asyncio.create_task(run_node(node_name, inputs))
                    running[node_name] = task

                if not running:
                    break

                # 等待任意节点完成
                done, _ = await asyncio.wait(
                    running.values(),
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in done:
                    node_name = self._find_node_name(running, task)
                    if node_name is None:
                        continue
                    del running[node_name]

                    try:
                        outputs = task.result()
                    except HumanInterruptError as e:
                        ctx.pending_approval = {"message": str(e), "node": node_name}
                        if session and hasattr(session, 'checkpoint'):
                            session.checkpoint = ctx.save_checkpoint()
                        raise
                    except Exception as e:
                        logger.exception(f"节点 '{node_name}' 异常: {e}")
                        outputs = {"output": NodeOutput(data=None, error=e)}
                        if event_callback:
                            await event_callback("node_error", node=node_name, error=str(e))

                    # 保存节点输出
                    node_outputs[node_name] = outputs
                    ctx.set_node_output(node_name, {
                        port: out.data if hasattr(out, 'data') else out
                        for port, out in outputs.items()
                    })
                    completed.add(node_name)

                    # 处理递归/循环场景：移除出环的节点 deb 集合
                    # 如果后续有边要循环回去，_activate_downstream 会标记为重入

                    # 人工审批节点自动保存检查点
                    node = graph.get_node(node_name)
                    if isinstance(node, HumanInLoopNode):
                        ctx.checkpoint = ctx.save_checkpoint()

                    # 激活下游节点
                    self._activate_downstream(
                        ctx, graph, node_name, outputs,
                        adjacency, reverse_deps,
                        node_inputs, completed, running, ready,
                    )

            # ── 执行完成 ──
            logger.info(f"DAG 执行完成: {len(completed)}/{len(graph.nodes)} 个节点")
            ctx.record("graph_complete", node="__graph__",
                       node_count=len(completed), total=len(graph.nodes))
            return ctx

        except asyncio.CancelledError:
            logger.warning("DAG 执行被取消")
            ctx.record("cancelled", node="__graph__")
            for task in running.values():
                task.cancel()
            return ctx

    # ═══════════════════════════════════════
    #  辅助方法
    # ═══════════════════════════════════════

    def _prepare_context(
        self,
        graph: WorkflowGraph,
        session: Optional[Any] = None,
        checkpoint: Optional[Dict] = None,
    ) -> ExecutionContext:
        """创建并初始化执行上下文"""
        errors = graph.validate()
        if errors:
            raise ValueError(f"图验证失败:\n" + "\n".join(f"  - {e}" for e in errors))

        ctx = ExecutionContext(
            request_id=f"dag_{uuid4().hex[:8]}",
            session_id=session.session_id if session and hasattr(session, 'session_id') else None,
        )
        ctx.llm_client = self.llm_client
        ctx.tool_registry = self.tool_registry

        if checkpoint:
            ctx.restore_checkpoint(checkpoint)
            if ctx.pending_approval:
                logger.info(f"恢复执行: 等待审批 {ctx.pending_approval}")

        return ctx

    def _update_compile_history(self, ctx: ExecutionContext):
        """从执行结果更新编译历史，校准预估耗时"""
        for node_name, record in ctx.get_node_timing().items():
            avg = self._compile_history.get(node_name, 0.0)
            dur = record.get("duration_ms", 0.0)
            if dur > 0:
                # 指数移动平均（EMA），α=0.3
                self._compile_history[node_name] = 0.3 * dur + 0.7 * avg if avg > 0 else dur

    @staticmethod
    def _build_adjacency(graph: WorkflowGraph) -> Tuple[Dict[str, List[Edge]], Dict[str, Set[str]]]:
        """构建邻接表和反向依赖"""
        adjacency: Dict[str, List[Edge]] = defaultdict(list)
        reverse_deps: Dict[str, Set[str]] = defaultdict(set)

        for edge in graph.edges:
            adjacency[edge.source].append(edge)
            if isinstance(edge, AllPassEdge):
                for s in edge.sources:
                    reverse_deps[edge.target].add(s)
            else:
                reverse_deps[edge.target].add(edge.source)

        return adjacency, reverse_deps

    def _activate_downstream(
        self,
        ctx: ExecutionContext,
        graph: WorkflowGraph,
        node_name: str,
        outputs: Dict[str, NodeOutput],
        adjacency: Dict[str, List[Edge]],
        reverse_deps: Dict[str, Set[str]],
        node_inputs: Dict[str, Dict[str, Tuple[str, str]]],
        completed: Set[str],
        running: Dict[str, asyncio.Task],
        ready: deque,
    ) -> None:
        """激活下游节点（同步方法，不 await）"""
        for edge in adjacency.get(node_name, []):
            # 条件边检查
            if isinstance(edge, ConditionalEdge):
                route_output = outputs.get(edge.route_port)
                route_val = route_output.data if hasattr(route_output, 'data') else route_output
                if str(route_val) != edge.condition_value:
                    continue
            elif not edge.evaluate(outputs):
                continue

            # 端口映射
            output_data = {
                port: out.data if hasattr(out, 'data') else out
                for port, out in outputs.items()
            }
            port_mapping = edge.map_ports(output_data)
            for target_port, source_port in port_mapping.items():
                node_inputs[edge.target][target_port] = (node_name, source_port)

            # 检查目标节点是否所有上游都已完成
            if self._all_upstream_completed(edge.target, reverse_deps, completed):
                if edge.target not in running:
                    is_reentry = edge.target in completed
                    ready.append((edge.target, is_reentry))

    async def _execute_node(
        self,
        ctx: ExecutionContext,
        graph: WorkflowGraph,
        node_name: str,
        inputs: Dict[str, NodeInput],
        event_callback: Optional[Callable[..., Coroutine]] = None,
    ) -> Dict[str, NodeOutput]:
        """执行单个节点（带追踪、超时、可选事件回调）"""
        node = graph.nodes[node_name]
        start = asyncio.get_event_loop().time()

        # 映射节点类型到 span 类型
        span_type_map = {
            "LLMNode": "llm",
            "ToolNode": "tool",
            "RouterNode": "router",
            "ToolDispatchNode": "tool",
            "HumanInLoopNode": "node",
            "ParallelNode": "node",
            "MapNode": "node",
        }
        span_type = span_type_map.get(node.node_type, "node")

        # 开始追踪 Span
        _tracer.start_span(ctx.request_id, node_name, span_type)

        ctx.start_node(node, inputs)
        ctx.record("node_start", node=node_name)

        # LLMNode 发送思考事件
        if event_callback and node.node_type == "LLMNode":
            await event_callback("thought", node=node_name, content="思考中...")

        try:
            outputs = await asyncio.wait_for(
                node.execute(ctx, inputs),
                timeout=self.default_node_timeout,
            )

            # 标准化输出
            if not isinstance(outputs, dict):
                outputs = {"output": NodeOutput.ok(outputs)}
            else:
                normalized = {}
                for key, val in outputs.items():
                    normalized[key] = val if isinstance(val, NodeOutput) else NodeOutput.ok(val)
                outputs = normalized

            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            ctx.record("node_complete", node=node_name, duration_ms=round(elapsed, 1))
            ctx.end_node(outputs)

            # 提取追踪元数据
            tracer_kwargs = {}
            if node.node_type == "LLMNode":
                # 从 LLMNode 输出提取 token 和模型信息
                usage_data = self._extract_llm_usage(outputs)
                tracer_kwargs.update(usage_data)
                model = getattr(node, 'model', None) or getattr(ctx, 'model', None)
                if model:
                    tracer_kwargs["model"] = model

            if node.node_type == "RouterNode":
                route_out = outputs.get("route")
                route_val = route_out.data if hasattr(route_out, 'data') else route_out
                tracer_kwargs["metadata"] = {"route": str(route_val)}

            # 结束追踪 Span
            _tracer.end_span(ctx.request_id, **tracer_kwargs)

            # 发送节点特定事件
            if event_callback:
                await self._emit_node_events(event_callback, node, node_name, outputs, duration_ms=elapsed)

            return outputs

        except asyncio.TimeoutError:
            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            error = f"节点超时 ({self.default_node_timeout}s)"
            ctx.record("node_timeout", node=node_name, duration_ms=round(elapsed, 1))
            ctx.end_node({}, error=TimeoutError(error))
            await node.on_error(ctx, TimeoutError(error))
            _tracer.end_span(ctx.request_id, error=error)
            if event_callback:
                await event_callback("node_error", node=node_name, error=error)
            return {"output": NodeOutput(data=None, error=TimeoutError(error))}

        except Exception as e:
            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            ctx.record("node_error", node=node_name, error=str(e))
            ctx.end_node({}, error=e)
            await node.on_error(ctx, e)
            _tracer.end_span(ctx.request_id, error=str(e))
            if event_callback:
                await event_callback("node_error", node=node_name, error=str(e))
            return {"output": NodeOutput(data=None, error=e)}

    async def _emit_node_events(
        self,
        event_callback: Callable[..., Coroutine],
        node: Node,
        node_name: str,
        outputs: Dict[str, NodeOutput],
        duration_ms: float = 0.0,
    ) -> None:
        """发送节点类型特定的事件"""
        # ToolNode → tool_call 事件
        if node.node_type == "ToolNode":
            out = outputs.get("output")
            await event_callback(
                "tool_call",
                node=node_name,
                tool=getattr(node, 'tool_name', node_name),
                status="done" if (out and out.is_ok) else "error",
                result=str(out.data)[:500] if out and out.data else "",
            )

        # LLMNode → content 事件
        if node.node_type == "LLMNode":
            content_out = outputs.get("content") or outputs.get("output")
            if content_out and content_out.data:
                content = content_out.data
                if isinstance(content, dict):
                    content = content.get("content", "")
                await event_callback("content", node=node_name, content=content)

        # 所有节点发送完成事件
        await event_callback(
            "node_complete",
            node=node_name,
            duration_ms=round(duration_ms, 1),
        )

    def _collect_inputs(
        self,
        ctx: ExecutionContext,
        graph: WorkflowGraph,
        node_name: str,
        node_inputs: Dict[str, Dict[str, Tuple[str, str]]],
        node_outputs: Dict[str, Dict[str, NodeOutput]],
    ) -> Dict[str, NodeInput]:
        """收集节点的所有输入"""
        result = {}
        port_sources = node_inputs.get(node_name, {})

        for target_port, (source_node, source_port) in port_sources.items():
            source_val = None

            # 从已完成的节点输出中取
            source_outputs = node_outputs.get(source_node, {})
            node_out = source_outputs.get(source_port)

            if node_out is not None:
                source_val = node_out.data if hasattr(node_out, 'data') else node_out
            elif source_node == "__input__":
                input_outputs = ctx.get_node_output("__input__")
                if isinstance(input_outputs, dict):
                    source_val = input_outputs.get(source_port)

            if source_val is not None:
                result[target_port] = NodeInput(
                    data=source_val,
                    source_node=source_node,
                    source_port=source_port,
                )

        # 回退逻辑
        if not result and port_sources:
            if len(port_sources) == 1:
                tp, (sn, sp) = next(iter(port_sources.items()))
                src_out = node_outputs.get(sn, {})
                val = src_out.get(sp)
                if val is not None:
                    d = val.data if hasattr(val, 'data') else val
                    result["default"] = NodeInput(data=d, source_node=sn, source_port=sp)
            if not result:
                all_input_outs = ctx.get_node_output("__input__")
                if isinstance(all_input_outs, dict):
                    for val in all_input_outs.values():
                        result["default"] = NodeInput(data=val, source_node="__input__")
                        break

        if "default" not in result:
            if len(result) == 1:
                only_val = next(iter(result.values()))
                result["default"] = only_val
            elif "output" in result:
                result["default"] = result["output"]

        return result

    @staticmethod
    def _all_upstream_completed(
        node_name: str,
        reverse_deps: Dict[str, Set[str]],
        completed: Set[str],
    ) -> bool:
        """检查目标节点的所有上游节点是否都已完成"""
        deps = reverse_deps.get(node_name, set())
        if not deps:
            return True
        return deps.issubset(completed)

    @staticmethod
    def _extract_llm_usage(outputs: Dict[str, NodeOutput]) -> Dict[str, Any]:
        """
        从 LLMNode 的输出中提取 token 使用量。

        支持多种输出格式：
          - {"output": NodeOutput(data={"usage": {...}})}
          - {"usage": NodeOutput(data={"prompt_tokens": 100})}
          - {"content": NodeOutput(data="...")}  # fallback: 大致估算
        """
        result: Dict[str, Any] = {"tokens_prompt": 0, "tokens_completion": 0}

        for port_name in ["output", "usage", "metadata"]:
            out = outputs.get(port_name)
            if out is None:
                continue
            data = out.data if hasattr(out, 'data') else out
            if not isinstance(data, dict):
                continue

            usage = data.get("usage", {})
            if isinstance(usage, dict):
                result["tokens_prompt"] = usage.get("prompt_tokens", 0)
                result["tokens_completion"] = usage.get("completion_tokens", 0)
                return result

            # 直接指定
            if "prompt_tokens" in data or "completion_tokens" in data:
                result["tokens_prompt"] = data.get("prompt_tokens", 0)
                result["tokens_completion"] = data.get("completion_tokens", 0)
                return result

            # OpenAI 风格的 response
            if "usage" in data and isinstance(data["usage"], dict):
                u = data["usage"]
                result["tokens_prompt"] = u.get("prompt_tokens", 0)
                result["tokens_completion"] = u.get("completion_tokens", 0)
                return result

        return result

    @staticmethod
    def _find_node_name(
        running_tasks: Dict[str, asyncio.Task],
        task: asyncio.Task,
    ) -> Optional[str]:
        """根据 Task 对象找到对应的节点名"""
        for name, t in running_tasks.items():
            if t is task:
                return name
        return None
