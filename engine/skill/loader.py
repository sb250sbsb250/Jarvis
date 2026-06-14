"""
engine/skill/loader.py — 标准 Skill 加载器

一个文件夹 = 一个 Skill = {name}.yaml + {name}.md

skills/code_review/
├── code_review.yaml ← 元数据
└── code_review.md   ← system prompt（按 ## 分段）
"""

import os
import re
import logging
from pathlib import Path
from typing import Dict, List, Optional

from .base import Skill, SkillMeta

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


class SkillPromptParser:
    """
    Markdown 提示词解析器。

    解析 skill.md，按 ## 标题分段存储。
    支持文件变更检测（热重载）。
    """

    SECTION_PATTERN = re.compile(r'^##\s+(.+)$', re.MULTILINE)

    def __init__(self, md_path: str):
        self._path = Path(md_path)
        self._sections: Dict[str, str] = {}
        self._mtime: float = 0.0  # 文件修改时间戳
        self._load()

    def _load(self):
        if not self._path.exists():
            self._sections["system"] = ""
            self._mtime = 0.0
            return

        self._mtime = self._path.stat().st_mtime
        content = self._path.read_text(encoding="utf-8")
        matches = list(self.SECTION_PATTERN.finditer(content))

        if not matches:
            self._sections["system"] = content.strip()
            return

        for i, match in enumerate(matches):
            section_name = match.group(1).strip().lower()
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            self._sections[section_name] = content[start:end].strip()

    def _check_reload(self):
        """检测文件是否变更，自动重载"""
        if self._path.exists():
            current_mtime = self._path.stat().st_mtime
            if current_mtime > self._mtime:
                logger.debug(f"检测到文件变更，自动重载: {self._path}")
                self._sections.clear()
                self._load()

    def get(self, section: str, default: str = "") -> str:
        self._check_reload()
        return self._sections.get(section.lower(), default)

    def has(self, section: str) -> bool:
        self._check_reload()
        return section.lower() in self._sections

    def reload(self):
        self._sections.clear()
        self._load()

    def get_full_prompt(self) -> str:
        """返回所有节内容，按文件顺序拼接"""
        self._check_reload()
        if not self._sections:
            return ""

        parts = []
        for section_name, content in self._sections.items():
            if not content:
                continue
            # system 节直接拼接，其他节带标题
            if section_name == "system":
                parts.append(content)
            elif section_name == "examples":
                parts.append(f"## 参考示例\n{content}")
            elif section_name == "constraints":
                parts.append(f"## 约束条件\n{content}")
            else:
                parts.append(f"## {section_name.capitalize()}\n{content}")

        return "\n\n".join(parts)


class StandardSkill(Skill):
    """
    标准 Skill — 从 skill.yaml + skill.md 加载的纯配置 Skill。

    不再构建 DAG，改为为 AgentLoop 提供 system prompt。
    """

    def __init__(self, skill_dir: str):
        super().__init__()
        self._dir = Path(skill_dir)
        self._config = self._load_yaml()
        self._cached_keywords: Optional[List[str]] = None  # 关键词缓存

        skill_name = self._config.get("name", self._dir.name)
        md_path = self._dir / f"{skill_name}.md"
        self._prompts = SkillPromptParser(str(md_path))

    @classmethod
    def from_dir(cls, skill_dir: str) -> "StandardSkill":
        return cls(skill_dir)

    def _load_yaml(self) -> dict:
        if yaml is None:
            raise ImportError("需要 PyYAML，请运行: pip install pyyaml")
        skill_name = self._dir.name
        yaml_path = self._dir / f"{skill_name}.yaml"
        if not yaml_path.exists():
            raise FileNotFoundError(f"Skill 配置文件不存在: {yaml_path}")
        with open(yaml_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    # ── Skill 接口 ──

    @property
    def meta(self) -> SkillMeta:
        return SkillMeta(
            name=self._config["name"],
            display_name=self._config.get("display_name", self._config["name"]),
            description=self._config.get("description", ""),
            icon=self._config.get("icon", "⚡"),
            tags=self._config.get("tags", []),
            tool_hints=self.get_tool_hints(),
            fallback=self.get_fallback(),
        )

    @property
    def trigger_keywords(self) -> List[str]:
        """从 tags + when_to_use + description 中提取关键词（带缓存）"""
        if self._cached_keywords is not None:
            return self._cached_keywords
        self._cached_keywords = self._extract_keywords()
        return self._cached_keywords

    def _extract_keywords(self) -> List[str]:
        """实际提取关键词的逻辑"""
        keywords = list(self._config.get("tags", []))

        when = self._config.get("when_to_use", "")
        if when:
            # 提取中文关键词（2-6字短语）
            import re
            words = re.findall(r'[\u4e00-\u9fff]{2,6}', when)
            keywords.extend(words)

        desc = self._config.get("description", "")
        if desc:
            # 提取描述中的中文关键词
            words = re.findall(r'[\u4e00-\u9fff]{2,4}', desc)
            keywords.extend(words)

        return list(set(keywords))

    def can_handle(self, user_input: str) -> float:
        """
        StandardSkill 的增强匹配：
        - tags 精确匹配 → 高置信度
        - when_to_use 匹配 → 中置信度
        - description 匹配 → 低置信度
        - 精确 name/display_name 匹配 → 最高置信度
        """
        user_lower = user_input.lower().strip()
        config = self._config

        # 1. 精确名称匹配
        name = config.get("name", "").lower()
        display = config.get("display_name", "").lower()
        if user_lower == name or user_lower == display:
            return 1.0
        if name in user_lower or display in user_lower:
            return 0.9

        # 2. Tag 匹配（精确）
        tags = config.get("tags", [])
        for tag in tags:
            if tag.lower() in user_lower:
                return 0.85

        # 3. when_to_use 场景匹配
        when = config.get("when_to_use", "")
        if when:
            # 提取关键场景词
            import re
            scenes = re.findall(r'[\u4e00-\u9fff]{2,6}', when)
            matches = sum(1 for s in scenes if s in user_lower)
            if matches >= 2:
                return 0.75
            if matches == 1:
                return 0.5

        # 4. 回退到基类匹配
        return super().can_handle(user_input)

    def get_tool_hints(self) -> List[str]:
        """返回本 Skill 推荐使用的工具列表"""
        return self._config.get("tools", [])

    def get_fallback(self) -> Optional[str]:
        """返回降级 Skill 名称，None 表示无降级"""
        return self._config.get("fallback")

    def get_system_prompt(self) -> str:
        """为 AgentLoop 提供领域 system prompt"""
        return self._prompts.get_full_prompt()

    def get_config_value(self, key: str, default: str = "") -> str:
        """获取 yaml 配置中的任意值"""
        return str(self._config.get(key, default))

    def get_all_config(self) -> dict:
        """获取所有 yaml 配置"""
        return dict(self._config)

    def __repr__(self):
        return f"<StandardSkill '{self._config['name']}' from {self._dir.name}>"
