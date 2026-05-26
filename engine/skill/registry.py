"""
skill/registry.py — Skill 注册中心

负责：
 1. 注册/注销 Skill
 2. 根据用户输入路由到最合适的 Skill
 3. 收集 Skill 需要的工具
 4. 维护 Skill 统计信息
"""

import logging
from typing import Dict, List, Optional, Tuple, Type, Set

from .base import Skill, SkillMeta

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Skill 注册中心"""

    def __init__(self):
        self._skills: Dict[str, Skill] = {}

    # ── 注册 ──

    def register(self, skill: Skill) -> "SkillRegistry":
        """注册一个 Skill"""
        name = skill.meta.name

        if name in self._skills:
            logger.warning(f"Skill '{name}' 已存在，将被覆盖")

        self._skills[name] = skill
        logger.info(f"✅ 注册 Skill: {skill.meta.icon} {skill.meta.display_name} "
                     f"({name}) level={skill.experience_level.name}")
        return self

    def register_many(self, skills: List[Skill]) -> "SkillRegistry":
        """批量注册"""
        for skill in skills:
            self.register(skill)
        return self

    def unregister(self, name: str) -> bool:
        """注销 Skill"""
        if name in self._skills:
            del self._skills[name]
            logger.info(f"🗑️ 注销 Skill: {name}")
            return True
        return False

    # ── 查询 ──

    def get(self, name: str) -> Optional[Skill]:
        """按名称获取 Skill"""
        return self._skills.get(name)

    def list_all(self) -> List[SkillMeta]:
        """列出所有 Skill 元数据"""
        return [s.meta for s in self._skills.values()]

    def list_skills(self) -> List[Skill]:
        """列出所有 Skill 实例"""
        return list(self._skills.values())

    def count(self) -> int:
        """已注册 Skill 数量"""
        return len(self._skills)

    # ── 路由 ──

    def route(self, user_input: str, top_k: int = 3) -> List[Tuple[Skill, float]]:
        """
        根据用户输入路由到最合适的 Skill

        路由策略:
        1. 每个 Skill 评估自己的置信度（can_handle）
        2. 经验等级加成
        3. 按置信度降序排序
        4. 返回 top_k 个

        优化: MASTER 级别的 Skill 如果置信度 >= 0.8，直接返回（早停）。
        """
        scored: List[Tuple[Skill, float]] = []

        for skill in self._skills.values():
            confidence = skill.can_handle(user_input)

            if confidence > 0:
                scored.append((skill, confidence))

                # MASTER 级别高置信度 → 早停
                if skill.experience_level.value >= 3 and confidence >= 0.8:
                    break

        # 排序：置信度 → 经验等级 → 成功率
        scored.sort(
            key=lambda x: (
                x[1],                          # 置信度
                x[0].experience_level.value,    # 经验等级
                x[0].success_rate,              # 成功率
            ),
            reverse=True,
        )

        return scored[:top_k]

    def route_exact(self, skill_name: str) -> Optional[Skill]:
        """精确路由（用户明确指定 Skill 名称）"""
        return self._skills.get(skill_name)

    # ── 工具收集 ──

    def collect_tools(self, skill_names: Optional[List[str]] = None) -> Set[Type]:
        """
        收集指定 Skill（或全部）的工具类（去重）
        """
        tools: Dict[int, Type] = {}

        targets = (
            [self._skills[n] for n in skill_names if n in self._skills]
            if skill_names
            else self._skills.values()
        )

        for skill in targets:
            for tc in skill.required_tools:
                tools[id(tc)] = tc

        return set(tools.values())

    # ── 统计 ──

    def get_stats(self) -> List[Dict]:
        """获取所有 Skill 的统计信息"""
        stats = []
        for skill in self._skills.values():
            stats.append(skill.get_stats())
        stats.sort(key=lambda x: x["success_count"], reverse=True)
        return stats

    def print_stats(self):
        """打印统计摘要"""
        print(f"\n{'='*60}")
        print(f"📊 Skill 统计 ({self.count()} 个)")
        print(f"{'─'*60}")

        for stat in self.get_stats():
            level_icon = {
                "NOVICE": "🌱",
                "PRACTITIONER": "🌿",
                "EXPERT": "🌳",
                "MASTER": "👑",
            }.get(stat["level"], "❓")

            print(f" {level_icon} {stat['display_name']:20s} "
                  f"{stat['level']:15s} "
                  f"✅{stat['success_count']:3d} ❌{stat['failure_count']:2d} "
                  f"成功率:{stat['success_rate']} "
                  f"平均耗时:{stat['avg_duration_ms']}ms")

        print(f"{'='*60}\n")
