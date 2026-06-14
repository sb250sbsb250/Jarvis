"""
engine/skill/matcher.py — 技能匹配

从 agent_loop.py 提取的技能匹配逻辑。
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MATCH_THRESHOLD = 3.0


def match_skill(task: str) -> Optional[Any]:
    """
    根据任务描述自动匹配最合适的 Skill。

    在 skills 目录中的每一个 Skill 都有一个 match_score(task) 方法，
    返回 0-10 的分数。选出最高分且 >= MATCH_THRESHOLD 的 Skill。
    """
    if not task or not task.strip():
        return None

    try:
        from skills import get_all_skills
    except ImportError:
        logger.debug("skills 包未安装，跳过技能匹配")
        return None
    except Exception as e:
        logger.debug(f"skills 加载失败: {e}")
        return None

    try:
        all_skills = get_all_skills()
    except Exception as e:
        logger.debug(f"get_all_skills() 失败: {e}")
        return None

    if not all_skills:
        return None

    best_score = 0.0
    best_skill = None

    for skill in all_skills:
        try:
            if not hasattr(skill, 'match_score'):
                continue
            score = skill.match_score(task)
            if score > best_score:
                best_score = score
                best_skill = skill
        except Exception as e:
            logger.debug(f"Skill {getattr(skill, 'name', '?')} 匹配失败: {e}")
            continue

    if best_score >= MATCH_THRESHOLD:
        logger.info(
            f"🎯 技能匹配: {getattr(best_skill.meta, 'display_name', '?')} "
            f"(score={best_score:.1f})"
        )
        return best_skill

    return None


def get_filtered_tools(tool_registry: Any, skill: Optional[Any] = None) -> List[Dict]:
    """获取工具定义列表，交给 LLM 的 tools 参数。

    如果当前有匹配的 Skill，只暴露该 Skill 需要的工具；
    否则暴露全部可用工具。
    """
    if hasattr(tool_registry, 'get_openai_tools'):
        all_tools = tool_registry.get_openai_tools()
    elif hasattr(tool_registry, 'get_tool_defs_for_llm'):
        all_tools = tool_registry.get_tool_defs_for_llm()
    else:
        all_tools = []

    if not skill:
        return all_tools

    allowed = set()
    try:
        if hasattr(skill, 'get_allowed_tools'):
            allowed = set(skill.get_allowed_tools() or [])
    except Exception:
        return all_tools

    if not allowed:
        return all_tools

    filtered = [t for t in all_tools if t.get("function", {}).get("name") in allowed]

    if not filtered:
        return all_tools

    return filtered
