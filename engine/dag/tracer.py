"""
dag/tracer.py — Agent 轻量级可观测性追踪器

零外部依赖，基于 dataclass + time 实现。
全局单例 tracer 供 GraphExecutor 和节点注入。

用法:
  from .tracer import tracer
  tracer.start_trace(request_id)
  tracer.start_span(request_id, name, type)
  ... do work ...
  tracer.end_span(request_id, tokens_prompt=..., tokens_completion=..., model=...)
  tracer.print_summary(request_id)
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── 模型定价表 ──────────────────────────────────
# (input_price_per_1M, output_price_per_1M) 美元
MODEL_PRICES: Dict[str, tuple[float, float]] = {
    # DeepSeek
    "deepseek-chat": (0.14, 0.28),
    "deepseek-reasoner": (0.55, 2.19),
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
    "gpt-3.5-turbo": (0.50, 1.50),
    # 通用 fallback
    "default": (0.50, 1.50),
}


@dataclass
class TraceSpan:
    """追踪的一个 Span（节点、LLM 调用、工具调用等）"""
    name: str
    type: str                         # llm | tool | node | graph | router
    start_time: float                 # time.time()
    end_time: Optional[float] = None
    tokens_prompt: int = 0
    tokens_completion: int = 0
    cost: float = 0.0
    error: Optional[str] = None
    model: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return 0.0

    @property
    def total_tokens(self) -> int:
        return self.tokens_prompt + self.tokens_completion


class AgentTracer:
    """
    轻量级 Agent 追踪器

    用法：
        tracer = AgentTracer()

        # 开始一次完整追踪
        tracer.start_trace("req_abc123")

        # 开始一个 Span
        tracer.start_span("req_abc123", "think", "llm")

        # ... 执行 LLM 调用 ...

        # 结束 Span
        tracer.end_span("req_abc123", model="gpt-4o",
                        tokens_prompt=100, tokens_completion=50)

        # 打印摘要
        tracer.print_summary("req_abc123")
    """

    def __init__(self):
        self.traces: Dict[str, List[TraceSpan]] = {}
        self._active_stacks: Dict[str, List[TraceSpan]] = defaultdict(list)
        # request_id -> 创建时间，用于总时长计算
        self._started_at: Dict[str, float] = {}

    # ── 生命周期 ──

    def start_trace(self, request_id: str) -> None:
        """开始一次完整追踪"""
        self.traces[request_id] = []
        self._started_at[request_id] = time.time()

    def start_span(self, request_id: str, name: str, span_type: str) -> None:
        """
        开始一个 Span

        Args:
            request_id: 追踪 ID
            name: Span 名称（如节点名）
            span_type: 类型（"llm"、"tool"、"node"、"router"、"graph"）
        """
        span = TraceSpan(name=name, type=span_type, start_time=time.time())
        self._active_stacks[request_id].append(span)

    def end_span(self, request_id: str, **kwargs) -> None:
        """
        结束当前活跃的 Span

        可接受的 kwargs：
            model: str               — LLM 模型名
            tokens_prompt: int       — 输入 token 数
            tokens_completion: int   — 输出 token 数
            error: str               — 错误信息
            metadata: dict           — 附加元数据
        """
        stack = self._active_stacks.get(request_id)
        if not stack:
            logger.warning(f"[tracer] end_span 无活跃 span: {request_id}")
            return

        span = stack.pop()
        span.end_time = time.time()

        # 注入 kwargs
        for key, val in kwargs.items():
            if key == "metadata" and isinstance(val, dict):
                span.metadata.update(val)
            elif hasattr(span, key):
                setattr(span, key, val)

        # 自动计算成本
        if span.type == "llm":
            span.cost = self._calculate_cost(
                span.model or "default",
                span.tokens_prompt,
                span.tokens_completion,
            )

        self.traces[request_id].append(span)
        logger.debug(f"[tracer] span done: {span.type}:{span.name} "
                     f"{span.duration_ms:.0f}ms tokens={span.total_tokens} "
                     f"cost=${span.cost:.6f}")

    # ── 查询 ──

    def get_summary(self, request_id: str) -> Optional[Dict[str, Any]]:
        """生成追踪摘要（可用于前端渲染或日志输出）"""
        spans = self.traces.get(request_id)
        if spans is None:
            return None

        started = self._started_at.get(request_id, 0)
        total_duration = (time.time() - started) * 1000 if started else 0

        total_cost = sum(s.cost for s in spans)
        total_tokens = sum(s.total_tokens for s in spans)
        errors = [s for s in spans if s.error]

        # 按类型分组
        by_type: Dict[str, list] = defaultdict(list)
        for s in spans:
            by_type[s.type].append(s)

        span_entries = []
        for s in spans:
            span_entries.append({
                "name": s.name,
                "type": s.type,
                "duration_ms": round(s.duration_ms, 1),
                "tokens_prompt": s.tokens_prompt,
                "tokens_completion": s.tokens_completion,
                "total_tokens": s.total_tokens,
                "cost": round(s.cost, 6),
                "error": s.error,
                "model": s.model,
            })

        return {
            "request_id": request_id,
            "total_duration_ms": round(total_duration, 1),
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 6),
            "total_cost_display": f"${total_cost:.6f}",
            "span_count": len(spans),
            "error_count": len(errors),
            "by_type": {t: len(v) for t, v in by_type.items()},
            "spans": span_entries,
        }

    def print_summary(self, request_id: str) -> None:
        """将追踪摘要打印到控制台"""
        summary = self.get_summary(request_id)
        if not summary:
            return

        # 计算各类型的图标
        icons = {
            "llm": "🧠",
            "tool": "🔧",
            "node": "📦",
            "router": "🔀",
            "graph": "🌐",
        }

        print(f"\n{'=' * 60}")
        print(f"📊 Agent Trace: {summary['request_id']}")
        print(f"  总耗时: {summary['total_duration_ms']:.0f}ms")
        print(f"  总Token: {summary['total_tokens']:,}")
        print(f"  总成本: {summary['total_cost_display']}")
        print(f"  Spans: {summary['span_count']} | Errors: {summary['error_count']} | "
              f"类型: {summary['by_type']}")
        print(f"{'─' * 60}")

        for span in summary["spans"]:
            icon = icons.get(span["type"], "•")
            status = "❌" if span["error"] else "✅"
            token_info = f" tokens={span['total_tokens']}" if span["total_tokens"] else ""
            model_info = f" [{span['model']}]" if span["model"] else ""
            error_info = f" | {span['error'][:40]}" if span["error"] else ""

            print(f"  {icon} {span['name']:20s} "
                  f"{span['duration_ms']:6.0f}ms "
                  f"${span['cost']:.6f}{token_info}{model_info}"
                  f" {status}{error_info}")

        print(f"{'=' * 60}\n")

    def to_json(self, request_id: str) -> Optional[str]:
        """导出为 JSON 字符串"""
        summary = self.get_summary(request_id)
        if not summary:
            return None
        return json.dumps(summary, ensure_ascii=False, indent=2)

    # ── 内部方法 ──

    @staticmethod
    def _calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """计算 LLM 调用成本"""
        prices = MODEL_PRICES.get(model) or MODEL_PRICES.get("default")
        if not prices:
            return 0.0
        input_price, output_price = prices
        cost = (
            (prompt_tokens / 1_000_000) * input_price
            + (completion_tokens / 1_000_000) * output_price
        )
        return round(cost, 6)


# 全局单例
tracer = AgentTracer()
