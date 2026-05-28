"""
skill/registry.py — Skill 注册中心（支持按需加载）

负责：
 1. 注册/注销 Skill
 2. 按目录绑定延迟加载
 3. 根据用户输入路由到最合适的 Skill
 4. 收集 Skill 需要的工具
 5. 维护 Skill 统计信息
"""

import importlib
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Type, Set, Callable

from .base import Skill, SkillMeta

logger = logging.getLogger(__name__)


class SkillDirectory:
    """
    按目录绑定的延迟加载 Skill 组。

    用法:
      dir = SkillDirectory("./my_skills")
      dir.load_all()  # 加载 my_skills/ 下的所有 Skill 类
    """

    def __init__(self, directory: str, label: str = "", enabled: bool = True):
        self.directory = Path(directory).resolve()
        self.label = label or self.directory.name
        self.enabled = enabled
        self._loaded = False
        self._skills: List[Skill] = []

    def load_all(self) -> List[Skill]:
        """加载目录下的所有 Skill"""
        if self._loaded:
            return self._skills

        if not self.directory.exists():
            logger.warning(f"Skill 目录不存在: {self.directory}")
            return []

        self._skills = []
        sys_path_backup = list(type.__module__ for type in type.__subclasses__(type))  # 无关

        # 遍历目录下的 .py 文件
        for child in sorted(self.directory.iterdir()):
            if child.suffix == ".py" and not child.name.startswith("__"):
                try:
                    # 动态导入
                    spec = importlib.util.spec_from_file_location(
                        f"skill_lazy_{self.directory.name}.{child.stem}",
                        child
                    )
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)

                        # 找到模块中所有 Skill 子类
                        for attr_name in dir(module):
                            attr = getattr(module, attr_name)
                            if (isinstance(attr, type) and
                                issubclass(attr, Skill) and
                                attr is not Skill):
                                try:
                                    skill = attr()
                                    self._skills.append(skill)
                                    logger.info(
                                        f"  按需加载 [{self.label}]: "
                                        f"{skill.meta.icon} {skill.meta.display_name}"
                                    )
                                except Exception as e:
                                    logger.warning(
                                        f"  ⚠️ 实例化失败 {attr_name}: {e}"
                                    )
                except Exception as e:
                    logger.warning(f"  ⚠️ 加载失败 {child.name}: {e}")

        self._loaded = True
        logger.info(f"📂 Skill 目录 [{self.label}]: "
                     f"已加载 {len(self._skills)} 个 Skill")
        return self._skills


class SkillRegistry:
    """Skill 注册中心（支持按需加载）"""

    def __init__(self):
        self._skills: Dict[str, Skill] = {}
        self._directories: List[SkillDirectory] = []
        self._load_callbacks: List[Callable] = []

    # ── 按目录注册（延迟加载） ──

    def register_directory(
        self,
        directory: str,
        label: str = "",
        enabled: bool = True,
        auto_load: bool = False,
    ) -> SkillDirectory:
        """
        按目录注册 Skill（延迟加载）。

        Args:
            directory: Skill 目录路径
            label: 目录标签（用于日志）
            enabled: 是否启用
            auto_load: 注册后立即加载

        Returns:
            SkillDirectory 实例
        """
        sd = SkillDirectory(directory, label, enabled)
        self._directories.append(sd)

        if auto_load:
            for skill in sd.load_all():
                self.register(skill)

        logger.info(f"📂 注册 Skill 目录: {sd.label} ({sd.directory})")
        return sd

    def load_directory(self, label: str) -> int:
        """
        延迟加载指定标签的目录。

        Returns:
            加载的 Skill 数量
        """
        count = 0
        for sd in self._directories:
            if sd.label == label and sd.enabled and not sd._loaded:
                for skill in sd.load_all():
                    self.register(skill)
                    count += 1
        return count

    def load_all_directories(self) -> int:
        """加载所有已注册的目录"""
        count = 0
        for sd in self._directories:
            if sd.enabled and not sd._loaded:
                for skill in sd.load_all():
                    self.register(skill)
                    count += 1
        return count

    def list_directories(self) -> List[Dict]:
        """列出所有注册的目录"""
        return [
            {
                "label": sd.label,
                "directory": str(sd.directory),
                "enabled": sd.enabled,
                "loaded": sd._loaded,
                "skill_count": len(sd._skills),
            }
            for sd in self._directories
        ]

    # ── 普通注册 ──

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
        """按名称获取 Skill"""
        if name in self._skills:
            return self._skills[name]

        # 尝试从目录延迟加载
        for sd in self._directories:
            if sd.enabled and not sd._loaded:
                for skill in sd.load_all():
                    self.register(skill)
                    if skill.meta.name == name:
                        return skill
        return None

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
        text_lower = text.lower()
        max_score = 0.0
        words = set(re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}', text.lower()))

        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in text_lower:
                score = 1.0 + (len(kw_lower) / max(len(text_lower), 1))
                max_score = max(max_score, score)
            else:
                # 模糊：用户输入词与关键词子串匹配
                for w in words:
                    if kw_lower in w or w in kw_lower:
                        max_score = max(max_score, 0.5)
                        break

        return max_score

    def route(self, user_input: str, top_k: int = 3) -> List[Tuple[Skill, float]]:
        """
        根据用户输入路由到最合适的 Skill

        策略：
          1. 关键词预筛快速排除不匹配的 Skill
          2. 对候选 Skill 调用 can_handle() 精确评分
          3. 按经验等级 + 成功率排序
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

        # 显示目录信息
        if self._directories:
            print(f"{'─'*60}")
            print(f"📂 按需加载目录 ({len(self._directories)} 个)")
            for sd in self._directories:
                status = "✅ 已加载" if sd._loaded else "💤 延迟加载"
                enabled = "开启" if sd.enabled else "关闭"
                print(f"  {status} [{sd.label}] ({enabled}) → {sd.directory}")

        print(f"{'='*60}\n")
