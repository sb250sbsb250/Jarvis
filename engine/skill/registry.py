"""
skill/registry.py — Skill 注册中心

负责：
 1. 注册/注销 Skill
 2. 根据用户输入路由到最合适的 Skill
 3. 维护 Skill 统计信息
"""

import difflib
import logging
from typing import Dict, List, Optional, Tuple, Set, Callable, Type, Any

from .base import Skill, SkillMeta

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Skill 注册中心（支持按需加载）"""

    def __init__(self):
        self._skills: Dict[str, Skill] = {}
        self._load_callbacks: List[Callable] = []

    def register(self, skill: Skill) -> "SkillRegistry":
        """注册一个 Skill"""
        name = skill.meta.name

        if name in self._skills:
            logger.warning(f"Skill '{name}' 已存在，将被覆盖")

        self._skills[name] = skill
        logger.info(f"✅ 注册 Skill: {skill.meta.icon} {skill.meta.display_name} "
                     f"({name}) level={skill.experience_level.name}")

        # 触发回调
        for cb in self._load_callbacks:
            try:
                cb(skill)
            except Exception as e:
                logger.warning(f"回调异常: {e}")

        return self

    def on_load(self, callback: Callable) -> None:
        """
        注册加载回调。每个 Skill 被注册时都会调用。

        示例:
          registry.on_load(lambda skill: print(f"已加载: {skill.meta.name}"))
        """
        self._load_callbacks.append(callback)

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
        return self._skills.get(name)

    def find(self, keyword: str) -> List[Skill]:
        """按关键词查找 Skill"""
        keyword = keyword.lower()
        results = []
        for skill in self._skills.values():
            if (keyword in skill.meta.name.lower() or
                keyword in skill.meta.display_name.lower() or
                keyword in skill.meta.description.lower() or
                any(keyword in t.lower() for t in skill.meta.tags)):
                results.append(skill)
        return results

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

    @staticmethod
    def _keyword_match(text: str, keywords: List[str]) -> float:
        """关键词预筛：快速过滤不匹配的 Skill"""
        if not keywords:
            return 0.0

        text_lower = text.lower().strip()

        # 计算匹配得分
        max_score = 0.0
        matched_count = 0

        for kw in keywords:
            kw_lower = kw.lower().strip()
            if not kw_lower:
                continue
            if len(kw_lower) < 2:
                continue

            if kw_lower in text_lower:
                # 完整匹配：长度越长权重越大
                score = min(1.0, len(kw_lower) / 10.0)
                max_score = max(max_score, score)
                matched_count += 1
            else:
                # 部分匹配：关键词的某个子串出现在输入中
                for i in range(max(1, len(kw_lower) - 2), len(kw_lower)):
                    sub = kw_lower[:i]
                    if len(sub) >= 2 and sub in text_lower:
                        max_score = max(max_score, 0.4)
                        break

        # 多个关键词匹配加分
        if matched_count >= 2:
            max_score = min(1.0, max_score + 0.15)

        return max_score

    def route(self, user_input: str, top_k: int = 3) -> List[Tuple[Skill, float]]:
        """
        根据用户输入路由到最合适的 Skill（纯关键词匹配，无 LLM 调用）。

        如需 LLM 语义回退，使用 route_with_fallback()。
        """
        # 1. 关键词预筛
        candidates: List[Skill] = []
        for skill in self._skills.values():
            kw_score = self._keyword_match(user_input, skill.trigger_keywords)
            if kw_score > 0:
                candidates.append(skill)

        # 2. 如果没有关键词匹配，fallback 到所有 Skill
        if not candidates:
            candidates = list(self._skills.values())

        # 3. 精确评分
        scored: List[Tuple[Skill, float]] = []
        for skill in candidates:
            confidence = skill.can_handle(user_input)
            if confidence > 0:
                scored.append((skill, confidence))

        scored.sort(
            key=lambda x: (
                x[1],
                x[0].experience_level.value,
                x[0].success_rate,
                x[0].meta.name,
            ),
            reverse=True,
        )

        return scored[:top_k]

    async def route_with_fallback(
        self,
        user_input: str,
        llm_client: Any = None,
        top_k: int = 3,
        min_confidence: float = 0.6,
    ) -> List[Tuple[Skill, float]]:
        """
        关键词预筛 + LLM 语义匹配。

        流程:
          1. 先走纯关键词匹配
          2. 如果最佳匹配置信度 >= min_confidence，直接返回
          3. 否则调用 LLM 做多 Skill 语义匹配

        Args:
            user_input: 用户输入
            llm_client: LLM 客户端
            top_k: 返回前 k 个
            min_confidence: 关键词匹配的最低置信度阈值
        """
        # 先走关键词
        results = self.route(user_input, top_k=top_k)

        if results and results[0][1] >= min_confidence:
            return results[:top_k]

        # 置信度不足 → LLM 语义匹配（多 Skill）
        if llm_client:
            llm_results = await self.route_with_llm(
                user_input, llm_client, top_k=top_k
            )
            if llm_results:
                names = [r[0].meta.name for r in llm_results]
                logger.info(f"🔮 LLM 路由: {', '.join(names)}")
                return llm_results

        return results[:top_k]

    async def route_with_llm(
        self,
        user_input: str,
        llm_client: Any,
        top_k: int = 3,
    ) -> List[Tuple[Skill, float]]:
        """
        用 LLM 一次性匹配多个 Skill，返回排序列表。

        与旧 _llm_route 的区别:
        - 传完整 Skill 信息（tags、when_to_use、description）
        - 返回多个 Skill 而非只选一个
        - 更高的置信度区分（0.9/0.75/0.6）
        """
        skills = self.list_skills()
        if not skills:
            return []

        # 构建完整 skill 描述
        skill_descriptions = []
        for s in skills:
            tags_str = ", ".join(s.meta.tags[:5])
            when = getattr(s, '_config', {}).get('when_to_use', '')
            desc = f"- **{s.meta.name}** ({s.meta.display_name})\n"
            desc += f"  {s.meta.description[:100]}\n"
            desc += f"  标签: {tags_str}"
            if when:
                desc += f"\n  场景: {when}"
            skill_descriptions.append(desc)

        prompt = (
            "根据用户请求，从以下 Skill 中匹配最相关的（可多个，按相关性排序）。\n\n"
            f"用户请求: {user_input}\n\n"
            "可用 Skill:\n" + "\n".join(skill_descriptions) + "\n\n"
            f"返回 JSON 数组，最多 {top_k} 个:\n"
            '[{"name": "skill名", "reason": "原因"}]\n\n'
            "都不匹配返回 []。只返回 JSON:"
        )

        try:
            response = await llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300,
            )
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "[]")
            import json
            matches = json.loads(content) if isinstance(content, str) else content

            results = []
            for i, m in enumerate(matches[:top_k]):
                skill = self.get(m.get("name", ""))
                if skill:
                    confidence = 0.9 - (i * 0.15)
                    results.append((skill, confidence))
            return results
        except Exception as e:
            logger.debug(f"LLM 路由失败: {e}")
            return []

    async def _llm_route(self, user_input: str, llm_client: Any) -> Optional[Skill]:
        """旧版 LLM 路由，保留兼容，内部委托给 route_with_llm"""
        results = await self.route_with_llm(user_input, llm_client, top_k=1)
        return results[0][0] if results else None

    def route_exact(self, skill_name: str) -> Optional[Skill]:
        """精确路由"""
        return self._skills.get(skill_name)

    # ── 工具收集（保留兼容接口，实际工具由 ToolRegistry 统一管理）──

    def collect_tools(self, skill_names: Optional[List[str]] = None) -> Set[Type]:
        """返回空集 — 工具由 ToolRegistry 统一管理"""
        return set()

    # ── 统计 ──

    def get_stats(self) -> List[Dict]:
        """获取所有 Skill 统计信息"""
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
