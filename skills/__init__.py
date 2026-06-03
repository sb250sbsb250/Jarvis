"""
skills/__init__.py — 自动发现所有 Skill

扫描 skills/ 下子目录：
  - 包含 skill.yaml + skill.md = 标准 Skill（StandardSkill）
  - 以 _ 开头或 . 开头的目录跳过

同时保留旧式 .py Skill（逐步迁移过渡期）
"""

import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from engine.skill.base import Skill

logger = logging.getLogger(__name__)
_SKILLS_DIR = Path(__file__).parent


# ═══════════════════════════════════════
#  发现标准 Skill（目录式）
# ═══════════════════════════════════════

def _discover_standard_skills() -> Dict[str, Skill]:
    """扫描子目录，加载标准 Skill（{name}.yaml + {name}.md）"""
    from engine.skill.loader import StandardSkill

    skills: Dict[str, Skill] = {}
    for item in sorted(_SKILLS_DIR.iterdir()):
        if not item.is_dir():
            continue
        if item.name.startswith("_") or item.name.startswith("."):
            continue
        dirname = item.name
        yaml_path = item / f"{dirname}.yaml"
        md_path = item / f"{dirname}.md"
        if not yaml_path.exists() or not md_path.exists():
            continue
        try:
            skill = StandardSkill.from_dir(str(item))
            skills[skill.meta.name] = skill
            logger.info(f"✅ [{skill.meta.icon}] {skill.meta.display_name}")
        except Exception as e:
            logger.warning(f"⚠️ {item.name}: {e}")
    return skills


# ═══════════════════════════════════════
#  发现旧式 Skill（.py 文件）
# ═══════════════════════════════════════

def _discover_legacy_skills() -> Dict[str, Skill]:
    """扫描 skills/*.py 中的 Skill 子类（向后兼容）"""
    import importlib
    import inspect

    skills: Dict[str, Skill] = {}

    # 只扫描 skills 目录下的 .py 文件
    for py_file in sorted(_SKILLS_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = f"skills.{py_file.stem}"
        try:
            module = importlib.import_module(module_name)
        except Exception as e:
            logger.debug(f"跳过 {module_name}: {e}")
            continue

        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Skill) and obj is not Skill:
                try:
                    instance = obj()
                    name = instance.meta.name
                    skills[name] = instance
                    logger.debug(f"✅ [旧] {instance.meta.display_name}")
                except Exception as e:
                    logger.warning(f"⚠️ {obj.__name__}: {e}")

    return skills


# ═══════════════════════════════════════
#  预热加载
# ═══════════════════════════════════════

_standard: Dict[str, Skill] = {}
_legacy: Dict[str, Skill] = {}

def _warmup():
    global _standard, _legacy
    _standard = _discover_standard_skills()
    _legacy = _discover_legacy_skills()
    logger.info(
        f"技能加载完成: {len(_standard)} 标准 + {len(_legacy)} 旧式"
    )

_warmup()


# ═══════════════════════════════════════
#  合并查询（标准优先）
# ═══════════════════════════════════════

def get_skill(name: str) -> Optional[Skill]:
    """获取 Skill（标准优先，旧式作为回退）"""
    s = _standard.get(name)
    if s:
        return s
    return _legacy.get(name)


def get_all_skills() -> List[Skill]:
    """获取所有 Skill（标准优先 + 旧式去重）"""
    seen = set()
    result = []
    for s in _standard.values():
        result.append(s)
        seen.add(s.meta.name)
    for s in _legacy.values():
        if s.meta.name not in seen:
            result.append(s)
            seen.add(s.meta.name)
    return result


def reload():
    """重新加载所有 Skill（热更新用）"""
    global _standard, _legacy
    _standard = _discover_standard_skills()
    _legacy = _discover_legacy_skills()
    logger.info(
        f"技能重新加载完成: {len(_standard)} 标准 + {len(_legacy)} 旧式"
    )
