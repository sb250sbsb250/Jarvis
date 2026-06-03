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
        top_k: int = 1,
        min_confidence: float = 0.4,
    ) -> List[Tuple[Skill, float]]:
        """
        关键词路由 + LLM 语义回退。

        流程：
          1. 先走纯关键词匹配（快、免费）
          2. 如果最佳匹配置信度 >= min_confidence，直接返回
          3. 如果置信度不足或无匹配，调用 LLM 做语义路由

        Args:
            user_input: 用户输入
            llm_client: LLM 客户端（有 chat_completion 方法）
            top_k: 返回前 k 个结果
            min_confidence: 关键词匹配的最低置信度阈值
        """
        # 先走关键词
        results = self.route(user_input, top_k=top_k)

        if results and results[0][1] >= min_confidence:
            return results

        # 置信度不足 → LLM 语义回退
        if llm_client:
            skill = await self._llm_route(user_input, llm_client)
            if skill:
                logger.info(
                    f"🔮 LLM 路由: {skill.meta.display_name} ({skill.meta.name})"
                )
                return [(skill, 0.85)]

        return results

    async def _llm_route(self, user_input: str, llm_client: Any) -> Optional[Skill]:
        """
        LLM 语义路由：用 LLM 选择最合适的 Skill。

        发送所有 Skill 的名称和描述，让 LLM 做语义匹配。
        用 difflib 对 LLM 返回做模糊修正。
        """
        skills = self.list_skills()
        if not skills:
            return None

        # 构建 Skill 列表（取前 15 个，避免 prompt 过长）
        skill_list = "\n".join(
            f"  {s.meta.name}: {s.meta.description[:80]}"
            for s in skills[:15]
        )

        prompt = (
            f"根据用户请求，从以下 Skill 中选择最适合的一个。\n"
            f"如果没有合适的，回答 'none'。\n\n"
            f"Skill 列表:\n{skill_list}\n\n"
            f"用户请求: {user_input[:200]}\n\n"
            f"只回答 Skill 名称（如 'excel_fill'），或 'none':"
        )

        try:
            response = await llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=50,
            )
            answer = (
                response.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
                .lower()
            )

            if answer == "none" or not answer:
                return None

            # 1. 精确匹配
            skill = self.get(answer)
            if skill:
                return skill

            # 2. 格式修正（空格→下划线）
            skill = self.get(answer.replace(" ", "_").replace("-", "_"))
            if skill:
                return skill

            # 3. 编辑距离模糊匹配
            names = [s.meta.name for s in skills]
            matches = difflib.get_close_matches(
                answer, names, n=1, cutoff=0.6
            )
            if matches:
                logger.info(f"🔍 模糊匹配: '{answer}' → '{matches[0]}'")
                return self.get(matches[0])

        except Exception as e:
            logger.debug(f"LLM 路由失败: {e}")

        return None

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
