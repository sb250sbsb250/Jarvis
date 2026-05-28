"""
skill/base.py — Skill 基类
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from enum import Enum
import time

if TYPE_CHECKING:
    from ..context import ExecutionContext


class SkillLevel(Enum):
    """经验等级（根据成功次数自动升级）"""
    NOVICE = 1       # < 5 次
    PRACTITIONER = 2 # 5-20 次
    EXPERT = 3       # 20-100 次
    MASTER = 4       # > 100 次（自动优先路由）


@dataclass
class SkillMeta:
    """Skill 元数据"""
    name: str                       # 唯一标识（如 "code_review"）
    display_name: str               # 显示名称（如 "代码审查"）
    description: str                # 一句话描述
    icon: str = "⚡"                 # 图标
    tags: List[str] = field(default_factory=list)  # 标签（用于检索）


@dataclass
class SkillResult:
    """Skill 执行结果"""
    success: bool
    content: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: float = 0.0


class Skill(ABC):
    """
    Skill 基类 — 纯配置驱动的 system prompt 提供者

    子类必须实现：
    - meta: SkillMeta

    可选实现：
    - get_system_prompt: 返回 domain prompt 给 AgentLoop
    - can_handle: 自定义触发判断
    - on_success / on_failure: 执行回调
    - trigger_keywords: 触发关键词列表
    """

    def __init__(self):
        self._success_count: int = 0
        self._failure_count: int = 0
        self._total_duration_ms: float = 0.0
        self._last_used_at: float = 0.0

    # ── 必须实现的属性 ──

    @property
    @abstractmethod
    def meta(self) -> SkillMeta:
        """Skill 元数据"""
        ...

    # ── 可选覆盖的方法 ──

    def get_system_prompt(self) -> str:
        """返回领域系统提示（AgentLoop system_prompt 的一部分）"""
        return ""

    @property
    def trigger_keywords(self) -> List[str]:
        """触发关键词列表（子类可选覆盖）"""
        return []

    def can_handle(self, user_input: str) -> float:
        """
        判断是否能处理用户输入

        策略（两阶段匹配）：
        1. 完整关键词匹配 — 关键词原文出现在用户输入中
        2. 子词匹配 — 将关键词拆成单个中文字词，看有多少命中

        两阶段结果取最大值，加上经验加成。

        Returns:
            0.0-1.0 的置信度
        """
        if not self.trigger_keywords:
            return 0.0

        user_lower = user_input.lower()

        # 阶段1：完整关键词匹配
        exact_matched = sum(1 for kw in self.trigger_keywords if kw.lower() in user_lower)

        # 阶段2：子词匹配 — 仅对 >=4 字的关键词拆成 3 字窗
        sub_matched = 0
        for kw in self.trigger_keywords:
            kw_lower = kw.lower()
            if len(kw_lower) < 4:
                continue
            for i in range(len(kw_lower) - 2):
                sub = kw_lower[i:i+3]
                if len(sub) >= 3 and sub in user_lower:
                    sub_matched += 1
                    break

        matched = max(exact_matched, sub_matched)
        if matched == 0:
            return 0.0

        safe_matched = min(matched, len(self.trigger_keywords))
        base_score = safe_matched / len(self.trigger_keywords)
        experience_bonus = min(0.3, self.experience_level.value * 0.08)
        recency_bonus = 0.1 if self.is_recently_used(3600) else 0.0

        return min(1.0, base_score + experience_bonus + recency_bonus)

    async def on_success(self, ctx: "ExecutionContext", result: SkillResult):
        self._success_count += 1
        self._total_duration_ms += result.duration_ms
        self._last_used_at = time.time()

    async def on_failure(self, error: Exception):
        self._failure_count += 1

    # ── 统计属性 ──

    @property
    def experience_level(self) -> SkillLevel:
        if self._success_count >= 100:
            return SkillLevel.MASTER
        elif self._success_count >= 20:
            return SkillLevel.EXPERT
        elif self._success_count >= 5:
            return SkillLevel.PRACTITIONER
        return SkillLevel.NOVICE

    @property
    def success_rate(self) -> float:
        total = self._success_count + self._failure_count
        if total == 0:
            return 0.0
        return self._success_count / total

    @property
    def avg_duration_ms(self) -> float:
        if self._success_count == 0:
            return 0.0
        return self._total_duration_ms / self._success_count

    def is_recently_used(self, seconds: float = 3600) -> bool:
        if self._last_used_at == 0:
            return False
        return (time.time() - self._last_used_at) < seconds

    def get_stats(self) -> Dict[str, Any]:
        return {
            "name": self.meta.name,
            "display_name": self.meta.display_name,
            "level": self.experience_level.name,
            "success_count": self._success_count,
            "failure_count": self._failure_count,
            "success_rate": f"{self.success_rate:.1%}",
            "avg_duration_ms": f"{self.avg_duration_ms:.0f}",
            "recently_used": self.is_recently_used(),
        }

    def __repr__(self) -> str:
        return f"<Skill '{self.meta.name}' level={self.experience_level.name}>"
