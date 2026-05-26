"""
dag/compiler.py — DAG 图编译器

编译时分析 DAG 结构，生成优化执行计划。

四大优化：
  1. 工具预取分析 — 提前识别需要加载的工具
  2. 并行分组 — 自动发现可并行执行的节点组
  3. 死代码消除 — 移除从入口不可达的节点
  4. 纯函数链折叠 — 合并连续无副作用的节点（降低调度开销）

编译器不修改原图，返回 CompiledGraph（含优化元数据）。
GraphExecutor 在 _schedule_graph 前调用 compile() 获取计划。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional, Tuple, Any

from .graph import WorkflowGraph
from .edge import Edge, ConditionalEdge

logger = logging.getLogger(__name__)

# ── 数据类型 ──

@dataclass
class CompiledGraph:
    """
    编译后的 DAG 执行计划

    不修改原图，仅附加优化元数据。
    executor 在执行前检查这些元数据来调整行为。
    """
    # 优化后的节点执行顺序（拓扑排序 + 并行组排序）
    node_order: List[str] = field(default_factory=list)

    # 节点 → 并行组 ID（同一 ID 的节点可并行执行）
    parallel_groups: Dict[str, int] = field(default_factory=dict)

    # 需要预取的工具名列表（executor 提前 import）
    prefetch_tools: List[str] = field(default_factory=list)

    # 被移除的死代码节点（原图有但不可达）
    dead_nodes: List[str] = field(default_factory=list)

    # 被折叠的纯函数链（[A,B,C] → A 保留，B,C 移除）
    fused_chains: List[List[str]] = field(default_factory=list)

    # 每个节点的预估耗时（ms），基于历史或类型经验值
    estimated_cost_ms: Dict[str, float] = field(default_factory=dict)

    # 编译统计
    original_node_count: int = 0
    effective_node_count: int = 0
    max_parallelism: int = 1
    critical_path_length: int = 0
    estimated_speedup: float = 1.0


class GraphCompiler:
    """
    DAG 图编译器

    用法:
        compiler = GraphCompiler()
        plan = compiler.compile(graph)
        executor.run(graph, ..., compiled_plan=plan)
    """

    # 节点类型到预估耗时基准（ms）
    # 只包含 V3 node.py 中实际存在的类型
    _BASE_COST: Dict[str, float] = {
        "LLMNode": 1500.0,
        "ToolNode": 500.0,
        "ToolDispatchNode": 0.5,
        "RouterNode": 0.3,
        "HumanInLoopNode": float('inf'),
        "ParallelNode": 100.0,
        "MapNode": 200.0,
        "ListFilesNode": 20.0,
        "FileProcessorNode": 100.0,
        "FileRenameNode": 10.0,
        "CodeSearchNode": 200.0,
        "CodeEditorNode": 300.0,
    }

    def __init__(self, history: Optional[Dict[str, float]] = None):
        """
        Args:
            history: 历史执行记录 {node_name: avg_duration_ms}
                     用于校准预估耗时
        """
        self._history = history or {}

    def compile(self, graph: WorkflowGraph) -> CompiledGraph:
        """
        编译图，返回优化执行计划

        执行流程:
          1. 死代码消除（标记不可达节点）
          2. 纯函数链折叠
          3. 并行度分析
          4. 工具预取分析
          5. 执行顺序优化 + 路径估算
        """
        if not graph.entry_points:
            return CompiledGraph(
                node_order=list(graph.nodes.keys()),
                original_node_count=len(graph.nodes),
                effective_node_count=0,
            )

        original_count = len(graph.nodes)

        # 1. 死代码消除
        reachable = self._compute_reachable(graph)
        dead = set(graph.nodes.keys()) - reachable
        dead_list = sorted(dead)

        # 2. 纯函数链折叠（不影响可达性）
        fused_chains = self._find_fuse_chains(graph, reachable)

        # 3. 并行度分析（基于拓扑层级）
        levels = self._topological_levels(graph, reachable)
        by_level = defaultdict(list)
        for node, level in levels.items():
            by_level[level].append(node)

        # 4. 工具预取分析
        prefetch = self._analyze_prefetch(graph, reachable)

        # 5. 执行顺序 + 并行组分配
        parallel_groups = {}
        node_order = []
        group_id = 0
        max_parallel = 1

        for level in sorted(by_level.keys()):
            nodes = by_level[level]
            if not nodes:
                continue

            # 同层节点排序：预取工具优先，LLM 次之，工具再次之，其他最后
            sorted_nodes = sorted(nodes, key=lambda n: (
                0 if n in prefetch else 1,
                0 if graph.nodes.get(n) and graph.nodes[n].node_type == "LLMNode" else 1,
                0 if graph.nodes.get(n) and graph.nodes[n].node_type == "ToolNode" else 2,
            ))

            for node in sorted_nodes:
                parallel_groups[node] = group_id

            # 同层节点如果不在同一依赖路径上，可以并行
            subgroups = self._split_independent_subgroups(graph, nodes)
            max_parallel = max(max_parallel, len(subgroups))

            node_order.extend(sorted_nodes)
            group_id += 1

        # 6. 关键路径长度 + 加速比估算
        critical_path = self._compute_critical_path(graph, levels, reachable)
        total_cost, parallel_cost = self._estimate_costs(graph, levels, reachable)
        speedup = total_cost / parallel_cost if parallel_cost > 0 else 1.0

        # 预估每个节点的耗时
        cost_ms = {}
        for name in reachable:
            node = graph.nodes[name]
            cost_ms[name] = self._history.get(
                name,
                self._BASE_COST.get(node.node_type, 50.0)
            )

        return CompiledGraph(
            node_order=node_order,
            parallel_groups=parallel_groups,
            prefetch_tools=prefetch,
            dead_nodes=dead_list,
            fused_chains=fused_chains,
            estimated_cost_ms=cost_ms,
            original_node_count=original_count,
            effective_node_count=len(reachable),
            max_parallelism=max_parallel,
            critical_path_length=critical_path,
            estimated_speedup=round(speedup, 2),
        )

    # ── 1. 死代码消除 ──

    def _compute_reachable(self, graph: WorkflowGraph) -> Set[str]:
        """广度优先搜索可达节点（入口 + 条件边 + tool_ 前缀节点）"""
        # 构建邻接表
        adjacency = defaultdict(list)
        for edge in graph.edges:
            adjacency[edge.source].append(edge.target)

        reachable = set(graph.entry_points)
        queue = list(graph.entry_points)

        while queue:
            current = queue.pop(0)
            for target in adjacency.get(current, []):
                if target not in reachable:
                    reachable.add(target)
                    queue.append(target)

        # tool_ 前缀节点可能通过 ToolDispatchNode 动态调度，标记为可达
        for name in graph.nodes:
            if name.startswith("tool_"):
                reachable.add(name)

        return reachable

    # ── 2. 纯函数链折叠 ──

    def _find_fuse_chains(self, graph: WorkflowGraph,
                          reachable: Set[str]) -> List[List[str]]:
        """
        找到可折叠的纯函数链

        V3 的 node.py 中没有纯函数节点类型（如 PassThroughNode、MergeNode），
        所以这个方法当前返回空列表。保留结构供后续扩展。
        """
        return []

    # ── 3. 并行度分析 ──

    def _topological_levels(self, graph: WorkflowGraph,
                            reachable: Set[str]) -> Dict[str, int]:
        """计算每个节点到入口的最远拓扑距离"""
        levels = {}

        # 初始化
        for entry in graph.entry_points:
            if entry in reachable:
                levels[entry] = 0

        # 动态规划：BFS 遍历所有边
        changed = True
        while changed:
            changed = False
            for edge in graph.edges:
                if edge.source in levels and edge.target in reachable:
                    new_level = levels[edge.source] + 1
                    if edge.target not in levels or levels[edge.target] < new_level:
                        levels[edge.target] = new_level
                        changed = True

        # tool_ 节点的层级修正（通过 ToolDispatchNode 调度，层级至少比 dispatch 高 1）
        for name in reachable:
            if name.startswith("tool_") and name not in levels:
                levels[name] = 0  # 兜底

        return levels

    def _split_independent_subgroups(self, graph: WorkflowGraph,
                                     nodes: List[str]) -> List[List[str]]:
        """
        将同层节点拆分为无相互依赖的并行子组

        同一子组内的节点可能相互依赖（通过边），必须串行。
        不同子组间的节点无依赖，可以并行。
        """
        if len(nodes) <= 1:
            return [nodes] if nodes else []

        # 构建依赖图（只考虑节点间的边）
        dep_matrix: Dict[str, Set[str]] = defaultdict(set)
        for edge in graph.edges:
            if edge.source in nodes and edge.target in nodes:
                dep_matrix[edge.target].add(edge.source)

        # 贪心分组
        groups = []
        remaining = set(nodes)

        while remaining:
            group = []
            for node in list(remaining):
                # 如果该节点的所有依赖都不在 remaining 中 → 可以放在当前组
                if dep_matrix[node].isdisjoint(remaining):
                    group.append(node)
            if group:
                for node in group:
                    remaining.discard(node)
                groups.append(group)
            else:
                # 循环依赖 → 全部放一组
                groups.append(list(remaining))
                break

        return groups

    # ── 4. 工具预取分析 ──

    def _analyze_prefetch(self, graph: WorkflowGraph,
                          reachable: Set[str]) -> List[str]:
        """分析需要预取的工具"""
        tools = set()
        for name in reachable:
            node = graph.nodes.get(name)
            if node and node.node_type == "ToolNode":
                tool_name = getattr(node, 'tool_name', None) or name
                tools.add(tool_name)
        return sorted(tools)

    # ── 5. 性能估算 ──

    def _compute_critical_path(self, graph: WorkflowGraph,
                               levels: Dict[str, int],
                               reachable: Set[str]) -> int:
        """计算关键路径长度（节点数）"""
        if not levels:
            return 0
        return max(levels.values()) + 1

    def _estimate_costs(self, graph: WorkflowGraph,
                        levels: Dict[str, int],
                        reachable: Set[str]) -> Tuple[float, float]:
        """
        估算串行总成本和并行总成本

        Returns:
            (total_cost, parallel_cost)
            total_cost: 串行执行总耗时
            parallel_cost: 理想并行执行耗时（关键路径）
        """
        # 每个节点的成本
        node_cost = {}
        for name in reachable:
            node = graph.nodes.get(name)
            node_cost[name] = self._history.get(
                name, self._BASE_COST.get(node.node_type if node else "", 50.0)
            )

        # 串行总成本
        total = sum(node_cost.values())

        # 并行成本 = 关键路径上各层最大成本之和
        by_level = defaultdict(list)
        for name, level in levels.items():
            by_level[level].append(name)

        parallel = 0.0
        for level in sorted(by_level.keys()):
            level_cost = max(node_cost[n] for n in by_level[level])
            parallel += level_cost

        return total, parallel


# ── 快捷函数 ──

def compile_graph(graph: WorkflowGraph,
                  history: Optional[Dict[str, float]] = None) -> CompiledGraph:
    """快捷编译图"""
    return GraphCompiler(history=history).compile(graph)
