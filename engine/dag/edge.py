"""
dag/edge.py — DAG 执行图的边定义

Edge 连接两个节点，支持：
  - 条件边：只有条件满足时才走这条边
  - 条件值边：与 RouterNode 的 route 输出匹配
  - 端口映射：将源节点的某端口映射到目标节点的某端口
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional


class Edge:
    """连接两个节点"""

    def __init__(
        self,
        source: str,
        target: str,
        condition: Optional[Callable[[Dict[str, Any]], bool]] = None,
        port_map: Optional[Dict[str, str]] = None,
    ):
        """
        Args:
            source: 源节点名
            target: 目标节点名
            condition: 条件函数（接收源节点的 outputs，返回 True 才走这条边）
            port_map: 端口映射 {源端口: 目标端口}
        """
        self.source = source
        self.target = target
        self.condition = condition or (lambda _: True)
        self.port_map = port_map or {}

    def evaluate(self, source_outputs: Dict[str, Any]) -> bool:
        """判断这条边是否应该激活"""
        try:
            return self.condition(source_outputs)
        except Exception:
            return False

    def map_ports(self, source_outputs: Dict[str, Any]) -> Dict[str, str]:
        """
        将源输出映射为 (目标端口, 源端口) 信息。
        返回: {目标端口: 源端口}
        """
        if self.port_map:
            return {v: k for k, v in self.port_map.items()}
        # 默认：将所有非 None 输出按同名端口透传
        mapping = {}
        for port_name in source_outputs:
            if source_outputs[port_name] is not None:
                mapping[port_name] = port_name
        return mapping

    def __repr__(self) -> str:
        return f"Edge({self.source} -> {self.target})"


class ConditionalEdge(Edge):
    """条件边——与 RouterNode 配合，根据 route 值匹配"""

    def __init__(
        self,
        source: str,
        condition_value: str,
        target: str,
        port_map: Optional[Dict[str, str]] = None,
        route_port: str = "route",
    ):
        super().__init__(source=source, target=target, port_map=port_map)
        self.condition_value = condition_value
        self.route_port = route_port

    def evaluate(self, source_outputs: Dict[str, Any]) -> bool:
        """检查 route 端口的值是否匹配"""
        route_data = source_outputs.get(self.route_port)
        if route_data is None:
            return False
        if hasattr(route_data, "data"):
            value = route_data.data
        else:
            value = route_data
        return str(value) == self.condition_value

    def __repr__(self) -> str:
        port_info = f"@{self.route_port}" if self.route_port != "route" else ""
        return f"ConditionalEdge({self.source}[{self.condition_value}]{port_info} -> {self.target})"


class AllPassEdge(Edge):
    """聚合边——等待所有上游节点完成后才触发（并行屏障）"""

    def __init__(
        self,
        sources: list[str],
        target: str,
        port_map: Optional[Dict[str, str]] = None,
    ):
        super().__init__(source=",".join(sources), target=target, port_map=port_map)
        self.sources = sources

    def evaluate(self, source_outputs: Dict[str, Any]) -> bool:
        return True  # 由执行器检查所有 sources 是否都完成

    def __repr__(self) -> str:
        return f"AllPassEdge({','.join(self.sources)} -> {self.target})"
