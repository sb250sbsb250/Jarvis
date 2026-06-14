"""
Skill 系统 — Jarvis V3 经验模块

Skill 是经过验证的 DAG 执行经验，固化为可复用的模块。
每个 Skill 可以直接被路由调用，支持：
 - 触发条件匹配（关键词/语义）
 - 经验等级（使用越多越优先）
 - 自动降级（失败时的回退方案）
 - 可组合（Skill 可以调用其他 Skill）
"""

from .base import Skill, SkillMeta, SkillLevel, SkillResult
from .registry import SkillRegistry
from .router import SkillRouter
from .matcher import match_skill, get_filtered_tools

__all__ = [
    "Skill", "SkillMeta", "SkillLevel", "SkillResult",
    "SkillRegistry", "SkillRouter",
    "match_skill", "get_filtered_tools",
]
