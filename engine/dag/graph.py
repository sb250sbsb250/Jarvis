"""
dag/graph.py — DAG 执行图定义

WorkflowGraph 是完整的执行图定义，包含：
  - 节点集合
  - 边集合
  - 入口点 / 出口点
  - 验证逻辑
  - Mermaid 可视化导出
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from .node import Node
    from .edge import Edge, ConditionalEdge

logger = logging.getLogger(__name__)


class WorkflowGraph:
    """DAG 执行图"""

    def __init__(self, name: str = "workflow"):
        self.name = name
        self.nodes: Dict[str, "Node"] = {}
        self.edges: List["Edge"] = []
        self.entry_points: Set[str] = set()
        self.exit_points: Set[str] = set()
        # 合法循环边白名单，标记为 (source, target) 的边不会被当作死循环
        self.loop_edges: Set[Tuple[str, str]] = set()

    def mark_as_loop(self, source: str, target: str) -> "WorkflowGraph":
        """标记一条边为合法循环边（不会被死循环检测报告）"""
        self.loop_edges.add((source, target))
        return self

    # ── 构建方法 ──

    def add_node(self, node: "Node") -> "WorkflowGraph":
        """添加节点"""
        if node.name in self.nodes:
            logger.warning(f"节点 '{node.name}' 已存在，将被覆盖")
        self.nodes[node.name] = node
        return self

    def add_edge(
        self,
        source: str,
        target: str,
        condition: Optional[Callable[[Dict[str, Any]], bool]] = None,
        port_map: Optional[Dict[str, str]] = None,
    ) -> "Edge":
        """添加普通边，返回 Edge 对象"""
        from .edge import Edge
        if source not in self.nodes:
            raise ValueError(f"源节点 '{source}' 不存在")
        if target not in self.nodes:
            raise ValueError(f"目标节点 '{target}' 不存在")
        edge = Edge(source, target, condition, port_map)
        self.edges.append(edge)
        return edge

    def add_conditional_edge(
        self,
        source: str,
        condition_value: str,
        target: str,
        port_map: Optional[Dict[str, str]] = None,
        route_port: str = "route",
    ) -> "ConditionalEdge":
        """添加条件边（与 RouterNode 配合），返回 ConditionalEdge 对象"""
        from .edge import ConditionalEdge
        if source not in self.nodes:
            raise ValueError(f"源节点 '{source}' 不存在")
        if target not in self.nodes:
            raise ValueError(f"目标节点 '{target}' 不存在")
        edge = ConditionalEdge(source, condition_value, target, port_map, route_port=route_port)
        self.edges.append(edge)
        return edge

    def add_conditional_edges(
        self,
        router_node: str,
        routes: Dict[str, str],
    ) -> "WorkflowGraph":
        """批量添加从路由节点出发的条件边"""
        for condition_value, target in routes.items():
            self.add_conditional_edge(router_node, condition_value, target)
        return self

    def set_entry(self, node_name: str) -> "WorkflowGraph":
        """设置入口节点"""
        if node_name not in self.nodes:
            raise ValueError(f"入口节点 '{node_name}' 未注册")
        self.entry_points.add(node_name)
        return self

    def set_exit(self, node_name: str) -> "WorkflowGraph":
        """设置出口节点"""
        if node_name not in self.nodes:
            raise ValueError(f"出口节点 '{node_name}' 未注册")
        self.exit_points.add(node_name)
        return self

    # ── 获取方法 ──

    def get_node(self, name: str) -> Optional["Node"]:
        return self.nodes.get(name)

    def get_upstream_edges(self, node_name: str) -> List["Edge"]:
        """获取指向该节点的所有边"""
        result = []
        for edge in self.edges:
            if hasattr(edge, 'sources'):
                if node_name in edge.sources:
                    result.append(edge)
            elif edge.target == node_name:
                result.append(edge)
        return result

    def get_downstream_edges(self, node_name: str) -> List["Edge"]:
        """获取从该节点出发的所有边"""
        return [e for e in self.edges if e.source == node_name]

    def get_downstream_nodes(self, node_name: str) -> List[str]:
        """获取从该节点出发可达的所有下游节点"""
        targets = set()
        for edge in self.get_downstream_edges(node_name):
            targets.add(edge.target)
            if hasattr(edge, 'sources'):
                targets.update(edge.sources)
        return list(targets)

    # ── 构建预定义模式 ──

    @staticmethod
    def create_thinking_execution_loop(
        think_node_name: str = "think",
        router_node_name: str = "router",
        execute_node_name: str = "execute",
        complete_node_name: str = "complete",
    ) -> "WorkflowGraph":
        """
        创建「思考→路由→执行→反思→循环/完成」的标准 Agent 循环图

        适用于需要多步推理+工具调用的场景。
        """
        from .node import LLMNode, RouterNode, ToolNode
        graph = WorkflowGraph("agent_loop")
        return graph

    # ═══════════════════════════════════════
#  语义级验证（升级版）
# ═══════════════════════════════════════

    def validate(self, strict: bool = True) -> List[str]:
        """
        验证图的完整性（语义感知版）。

        Args:
            strict: True=严格模式(生产)，False=宽松模式(开发中)

        返回错误列表，空列表 = 通过

        验证项:
          1. 边引用的节点必须存在
          2. 必须有入口点
          3. 所有节点必须从入口点可达（条件边和循环边也算）
          4. 合法循环 vs 死循环检测
          5. 语义感知的出边检查（动态调度节点豁免）
        """
        errors = []

        errors.extend(self._validate_edge_nodes())
        errors.extend(self._validate_entry_points())
        errors.extend(self._validate_reachability())
        errors.extend(self._validate_cycles())
        errors.extend(self._validate_downstream(strict=strict))

        return errors

    def is_valid(self) -> bool:
        return len(self.validate()) == 0

    # ── 验证子步骤 ──

    def _validate_edge_nodes(self) -> List[str]:
        """验证边引用的节点存在"""
        errors = []
        for edge in self.edges:
            if hasattr(edge, 'sources'):
                for s in edge.sources:
                    if s not in self.nodes:
                        errors.append(f"边引用源节点 '{s}' 不存在")
            elif edge.source not in self.nodes:
                errors.append(f"边引用源节点 '{edge.source}' 不存在")
            if edge.target not in self.nodes:
                errors.append(f"边引用目标节点 '{edge.target}' 不存在")
        return errors

    def _validate_entry_points(self) -> List[str]:
        if not self.entry_points:
            return ["至少需要一个入口点 (set_entry)"]
        return []

    def _validate_reachability(self) -> List[str]:
        """
        语义化可达性检查。

        从入口点出发，通过所有边类型（普通边、条件边、AllPassEdge）传播。
        条件边的目标节点即使没有遍历所有分支，也视为条件可达。
        这样工具节点即使只被某个条件值指向，也算可达。
        """
        errors = []
        reachable = self._compute_reachable()
        for name in self.nodes:
            if name not in reachable and name not in self.entry_points:
                errors.append(
                    f"节点 '{name}' ({self.nodes[name].node_type}) "
                    f"不可达——没有从入口点 {list(self.entry_points)} 到它的路径"
                )
        return errors

    def _compute_reachable(self) -> Set[str]:
        """
        从入口点出发的可达节点集合。

        与旧版区别：遍历 ALL 边类型。变通的边类型通过 source 判断仍能生效：
        - 普通边: edge.source in reachable → edge.target
        - 条件边: edge.source in reachable → edge.target（走到哪个分支运行时才定）
        - 循环边: 不算首次可达传播（防止无限循环），但回边的 target 应先已注册
        """
        reachable = set(self.entry_points)
        changed = True
        while changed:
            changed = False
            for edge in self.edges:
                # 任何边类型的 source 字段都指向源节点
                src = edge.source
                if src in reachable and edge.target not in reachable:
                    reachable.add(edge.target)
                    changed = True
        return reachable

    def _validate_cycles(self) -> List[str]:
        """验证死循环（合法循环 = 有出口的环）"""
        if self._has_cycle_without_exit():
            return ["检测到没有出口的死循环路径"]
        return []

    def _validate_downstream(self, strict: bool = True) -> List[str]:
        """
        语义感知的出边检查。

        节点分类与出边要求:
        ┌─────────────────┬──────────────────────────┐
        │ 节点类型 │ 出边要求 │
        ├─────────────────┼──────────────────────────┤
        │ 出口节点 │ 无要求 │
        │ 动态调度节点 │ 无要求（由上游条件激活） │
        │ LLMNode │ 必须有出边 │
        │ RouterNode │ 必须有出边（路由出口） │
        │ 其他节点 │ 必须有出边 │
        └─────────────────┴──────────────────────────┘

        动态调度节点 = 有 ConditionalEdge 指向它的节点。
        这类节点在运行时由 ToolDispatchNode / RouterNode 条件激活。
        """
        if not strict:
            return []

        errors = []
        dynamic_nodes = self._get_dynamic_activated_nodes()

        for name, node in self.nodes.items():
            if name in self.exit_points:
                continue
            if name in dynamic_nodes:
                continue

            outgoing = self._get_all_outgoing_edges(name)
            if not outgoing:
                errors.append(
                    f"非出口节点 '{name}' ({node.node_type}) 没有出边. "
                    f"请添加边: graph.add_edge('{name}', '<target>')"
                )

        return errors

    def _get_dynamic_activated_nodes(self) -> Set[str]:
        """
        获取所有动态调度节点。

        包括:
        1. 条件边指向的节点（ConditionalEdge target）
        2. 名称以 tool_ 开头的节点（ToolNode，由 ToolDispatchNode 动态调度）
        """
        dynamic = set()
        for edge in self.edges:
            if hasattr(edge, 'condition_value'):
                dynamic.add(edge.target)
        # 工具节点本质上就是动态调度的——由 ToolDispatchNode 激活，不需要静态出边
        for name in self.nodes:
            if name.startswith("tool_"):
                dynamic.add(name)
        return dynamic

    def _get_all_outgoing_edges(self, node_name: str) -> List:
        """获取节点的所有出边（普通边 + 条件边 + AllPassEdge）"""
        outgoing = []
        for edge in self.edges:
            if edge.source == node_name:
                outgoing.append(edge)
            elif hasattr(edge, 'sources') and node_name in edge.sources:
                outgoing.append(edge)
        return outgoing

    def mark_as_loop_edge(self, edge: "Edge") -> "WorkflowGraph":
        """根据 Edge 对象标记合法循环边"""
        self.loop_edges.add((edge.source, edge.target))
        if hasattr(edge, 'sources'):
            for s in edge.sources:
                self.loop_edges.add((s, edge.target))
        return self

    def _has_cycle_without_exit(self) -> bool:
        """
        检查是否有不经过出口节点的死循环。

        改进：区分"合法循环边"（如工具→think 的回边）
        和"死循环"（没有出口的纯环）。
        """
        visited = set()
        rec_stack = set()
        cycle_nodes: Set[str] = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)

            downstream = self.get_downstream_nodes(node)
            for target in downstream:
                # 跳过被标记为合法循环的边
                if (node, target) in self.loop_edges:
                    continue
                if target not in visited:
                    if dfs(target):
                        return True
                elif target in rec_stack:
                    # 发现环，记录环上所有节点
                    cycle_nodes.update(rec_stack)
                    return True

            rec_stack.discard(node)
            return False

        for entry in self.entry_points:
            if entry not in visited:
                dfs(entry)

        if not cycle_nodes:
            return False

        # 检查环上是否有出口节点
        for node in cycle_nodes:
            if node in self.exit_points:
                return False  # 有出口 = 合法循环

        # 检查环上节点是否有到出口的路径（间接出口）
        for node in cycle_nodes:
            if self._has_path_to_exit(node, cycle_nodes):
                return False

        return True  # 纯死循环

    def _has_path_to_exit(self, node: str, cycle_nodes: Set[str]) -> bool:
        """检查节点是否能到达出口（不经过环上节点）"""
        visited = set()
        stack = [node]

        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)

            if current in self.exit_points:
                return True

            for target in self.get_downstream_nodes(current):
                if target not in visited and target not in cycle_nodes:
                    stack.append(target)

        return False

    # ── 详细诊断 ──

    def validate_verbose(self) -> str:
        """返回详细的验证报告（用于调试）"""
        errors = self.validate(strict=True)
        if not errors:
            return f"✅ 图验证通过\n📊 诊断: {self._diagnostic_line()}"

        report = [
            f"❌ 图验证失败 ({len(errors)} 个错误):",
        ]
        for e in errors:
            report.append(f"  - {e}")

        report.append("")
        report.append("📊 诊断信息:")
        report.append(f"  节点: {len(self.nodes)}")
        report.append(f"  边: {len(self.edges)}")
        report.append(f"  入口: {self.entry_points}")
        report.append(f"  出口: {self.exit_points}")
        report.append(f"  合法循环边: {len(self.loop_edges)}")

        # 动态调度节点
        dyn = self._get_dynamic_activated_nodes()
        if dyn:
            report.append(f"  条件激活节点: {len(dyn)} 个")
            for dn in sorted(dyn):
                # 找哪个条件边指向它
                reasons = []
                for e in self.edges:
                    if hasattr(e, 'condition_value') and e.target == dn:
                        reasons.append(f"{e.source}[{e.condition_value}]")
                report.append(f"    - {dn} ({self.nodes[dn].node_type}) via {', '.join(reasons)}")

        # 无出边的节点
        for name in sorted(self.nodes.keys()):
            if name in self.exit_points:
                continue
            if name in dyn:
                continue
            outgoing = self._get_all_outgoing_edges(name)
            if not outgoing:
                nt = self.nodes[name].node_type
                report.append(f"  ⚠️ 无出边: {name} ({nt})")

        return "\n".join(report)

    def analyze(self) -> Dict[str, Any]:
        """
        分析图结构，返回完整诊断字典。

        可在启动时调用，帮助快速了解图结构。
        """
        return {
            "name": self.name,
            "is_valid": self.is_valid(),
            "errors": self.validate(strict=True),
            "stats": {
                "nodes": len(self.nodes),
                "edges": len(self.edges),
                "entry_points": len(self.entry_points),
                "exit_points": len(self.exit_points),
                "loop_edges": len(self.loop_edges),
            },
            "node_types": {
                name: node.node_type
                for name, node in self.nodes.items()
            },
            "topology": {
                "entry": sorted(self.entry_points),
                "exit": sorted(self.exit_points),
                "dynamically_activated": sorted(self._get_dynamic_activated_nodes()),
            },
            "mermaid": self.to_mermaid(),
        }

    def _diagnostic_line(self) -> str:
        """一行诊断摘要"""
        dyn = self._get_dynamic_activated_nodes()
        return (
            f"{len(self.nodes)}节点/{len(self.edges)}边, "
            f"入口={sorted(self.entry_points)}, 出口={sorted(self.exit_points)}, "
            f"条件激活={len(dyn)}, 循环边={len(self.loop_edges)}"
        )

    # ── 可视化 ──

    def to_mermaid(self) -> str:
        """导出为 Mermaid 流程图"""
        lines = ["graph TD"]
        lines.append(f"  title[{self.name}]")

        # 节点定义
        for name, node in self.nodes.items():
            node_id = name.replace("-", "_").replace(" ", "_")
            if name in self.entry_points and name in self.exit_points:
                lines.append(f"  {node_id}([\"{name}\"])")
            elif name in self.entry_points:
                lines.append(f"  {node_id}>\"{name}\"]")
            elif name in self.exit_points:
                lines.append(f"  {node_id}[\"{name}\"]")
            else:
                lines.append(f"  {node_id}[\"{name}\"]")

        # 边定义
        for edge in self.edges:
            src_id = edge.source.replace("-", "_").replace(" ", "_")
            tgt_id = edge.target.replace("-", "_").replace(" ", "_")
            if hasattr(edge, 'condition_value'):
                lines.append(f"  {src_id} -- \"{edge.condition_value}\" --> {tgt_id}")
            elif hasattr(edge, 'sources'):
                for s in edge.sources:
                    s_id = s.replace("-", "_").replace(" ", "_")
                    lines.append(f"  {s_id} -.-> {tgt_id}")
            else:
                lines.append(f"  {src_id} --> {tgt_id}")

        return "\n".join(lines)

    def visualize(self) -> str:
        """获取可视化字符串（方便打印）"""
        lines = [f"=== WorkflowGraph: {self.name} ===", f"节点 ({len(self.nodes)}):"]
        for name in sorted(self.nodes.keys()):
            node = self.nodes[name]
            entry = " [入口]" if name in self.entry_points else ""
            exit_ = " [出口]" if name in self.exit_points else ""
            lines.append(f"  {node.node_type}: {name}{entry}{exit_}")
        lines.append(f"边 ({len(self.edges)}):")
        for edge in self.edges:
            lines.append(f"  {edge}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"WorkflowGraph('{self.name}', {len(self.nodes)} nodes, {len(self.edges)} edges)"
