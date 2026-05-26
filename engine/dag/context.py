"""
dag/context.py — DAG 执行上下文

ExecutionContext 管理：
  - 节点间数据传递
  - 执行追踪 (NodeTrace)
  - 全局共享数据
  - 检查点保存/恢复
  - 中断/恢复
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .node import NodeOutput

logger = logging.getLogger(__name__)


@dataclass
class NodeTrace:
    """节点执行追踪"""
    node_name: str
    node_type: str = ""
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    duration_ms: float = 0.0
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    children: List["NodeTrace"] = field(default_factory=list)

    def complete(self, outputs: Dict[str, Any] = None, error: Optional[str] = None) -> None:
        self.end_time = datetime.now()
        self.duration_ms = (self.end_time - self.start_time).total_seconds() * 1000
        if outputs:
            self.outputs = outputs
        if error:
            self.error = error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node": self.node_name,
            "type": self.node_type,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
            "events": self.events,
        }


@dataclass
class EventRecord:
    """DAG 执行事件记录"""
    type: str
    node: str
    timestamp: datetime = field(default_factory=datetime.now)
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionContextConfig:
    """执行上下文配置

    - max_traces: 最多保留的追踪记录数（超出后自动裁剪旧记录）
    - max_events: 最多保留的事件记录数（超出后自动裁剪）
    - max_output_size: 单个输出值的最大字符长度（超出后截断）
    - max_checkpoints: 最多保留的检查点数
    """
    max_traces: int = 1000
    max_events: int = 5000
    max_output_size: int = 5000
    max_checkpoints: int = 10


class ExecutionContext:
    """
    DAG 执行上下文

    - 节点间数据流（端口传递）
    - 执行追踪（每个节点一个 Trace）
    - 全局共享数据（global_data）
    - 检查点（用于断点恢复）
    - 内存管理：自动裁剪 old 追踪和事件
    """

    def __init__(
        self,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        config: Optional[ExecutionContextConfig] = None,
    ):
        self.request_id = request_id or f"dag_{uuid4().hex[:8]}"
        self.session_id = session_id
        self.created_at = datetime.now()

        # 内存管理配置
        self.config = config or ExecutionContextConfig()

        # 外部依赖（由 GraphExecutor 注入）
        self.llm_client: Any = None
        self.tool_registry: Any = None

        # 节点间数据传递（自动管理）
        self._node_outputs: Dict[str, Dict[str, Any]] = {}

        # 全局共享数据
        self.global_data: Dict[str, Any] = {}

        # 追踪系统
        self.traces: List[NodeTrace] = []
        self._current_trace: Optional[NodeTrace] = None
        self.events: List[EventRecord] = []

        # 编译计划（由 GraphExecutor 注入）
        self.compiled_plan: Any = None

        # 中断/审批
        self.pending_approval: Optional[Dict] = None

        # 检查点
        self.checkpoints: List[Dict] = []

    # ── 数据流 ──

    def get_node_output(
        self,
        node_name: str,
        port: Optional[str] = None,
    ) -> Optional[Any]:
        """
        获取指定节点的输出。

        如果 port 指定了端口名，返回该端口的输出值；
        如果 port 为 None，返回整个输出 dict（所有端口）。
        """
        outputs = self._node_outputs.get(node_name)
        if outputs is None:
            return None
        if port is None:
            return outputs  # 返回整个 dict
        return outputs.get(port)

    def get_first_node_output(self, node_name: str) -> Optional[Any]:
        """获取指定节点的第一个非空输出"""
        outputs = self._node_outputs.get(node_name)
        if not outputs:
            return None
        for port_data in outputs.values():
            if port_data is not None:
                return port_data
        return None

    def set_node_output(self, node_name: str, outputs: Dict[str, Any]) -> None:
        """保存节点输出"""
        self._node_outputs[node_name] = outputs

    def get_all_node_outputs(self) -> Dict[str, Any]:
        """获取所有节点输出"""
        return dict(self._node_outputs)

    # ── 事件记录 ──

    def record(self, event_type: str, node: str, **data) -> None:
        """记录执行事件"""
        event = EventRecord(type=event_type, node=node, data=data)
        self.events.append(event)

        # 也添加到当前 trace
        if self._current_trace:
            self._current_trace.events.append({
                "type": event_type,
                "data": data,
            })

        logger.debug(f"[{event_type}] {node}: {data}")

        # 自动裁剪
        self._maybe_crop_events()

    def _maybe_crop_events(self) -> None:
        """如果事件数超过上限，裁剪掉最旧的 50%"""
        if len(self.events) > self.config.max_events:
            crop_count = len(self.events) // 2
            self.events = self.events[crop_count:]
            logger.debug(f"事件裁剪: 移除 {crop_count} 条旧事件")

    def _maybe_crop_traces(self) -> None:
        """如果追踪数超过上限，裁剪掉最旧的 50%"""
        if len(self.traces) > self.config.max_traces:
            crop_count = len(self.traces) // 2
            self.traces = self.traces[crop_count:]
            logger.debug(f"追踪裁剪: 移除 {crop_count} 条旧追踪")

    # ── 追踪管理 ──

    def _truncate_str(self, value: Any, max_len: Optional[int] = None) -> str:
        """截断字符串表示"""
        max_len = max_len or self.config.max_output_size
        s = str(value)
        if len(s) > max_len:
            return s[:max_len] + f"... [truncated, {len(s)} total]"
        return s

    def start_node(self, node: Any, inputs: Dict[str, Any]) -> None:
        """开始追踪一个节点"""
        self._current_trace = NodeTrace(
            node_name=node.name,
            node_type=node.node_type,
            inputs={
                k: self._truncate_str(v.data if hasattr(v, 'data') else v, 200)
                for k, v in inputs.items()
            },
        )

    def end_node(
        self,
        outputs: Dict[str, Any],
        error: Optional[Exception] = None,
    ) -> None:
        """结束节点追踪"""
        if self._current_trace:
            self._current_trace.complete(
                outputs={
                    k: self._truncate_str(v.data if hasattr(v, 'data') else v, 200)
                    for k, v in outputs.items()
                },
                error=str(error) if error else None,
            )
            self.traces.append(self._current_trace)
            self._current_trace = None
            # 自动裁剪
            self._maybe_crop_traces()

    # ── 节点耗时统计（供编译器校准用）──

    def get_node_timing(self) -> Dict[str, Dict]:
        """获取各节点耗时（用于编译器历史校准）"""
        return {
            t.node_name: {"duration_ms": t.duration_ms, "error": t.error}
            for t in self.traces
        }

    # ── 检查点 ──

    def save_checkpoint(self) -> Dict:
        """保存检查点"""
        cp = {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "global_data": dict(self.global_data),
            "node_outputs": {
                name: {
                    port: data
                    for port, data in outputs.items()
                }
                for name, outputs in self._node_outputs.items()
            },
            "pending_approval": self.pending_approval,
        }
        self.checkpoints.append(cp)
        # 检查点裁剪
        if len(self.checkpoints) > self.config.max_checkpoints:
            self.checkpoints = self.checkpoints[-self.config.max_checkpoints:]
        return cp

    def restore_checkpoint(self, checkpoint: Dict) -> None:
        """恢复检查点"""
        self.global_data = dict(checkpoint.get("global_data", {}))
        self.pending_approval = checkpoint.get("pending_approval")

        # 恢复节点输出（仅恢复可 JSON 序列化的）
        node_outputs = checkpoint.get("node_outputs", {})
        for name, outputs in node_outputs.items():
            if name not in self._node_outputs:
                self._node_outputs[name] = {}
            for port, data in outputs.items():
                self._node_outputs[name][port] = data

        logger.debug(f"检查点恢复: {len(node_outputs)} 个节点")

    # ── 汇总 ──

    def get_summary(self) -> Dict[str, Any]:
        """获取执行摘要"""
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "duration_ms": round((datetime.now() - self.created_at).total_seconds() * 1000, 1),
            "node_count": len(self.traces),
            "event_count": len(self.events),
            "traces": [t.to_dict() for t in self.traces],
            "has_error": any(t.error for t in self.traces),
        }

    def to_dict(self) -> Dict[str, Any]:
        """完整序列化"""
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "summary": self.get_summary(),
            "global_data_keys": list(self.global_data.keys()),
        }
