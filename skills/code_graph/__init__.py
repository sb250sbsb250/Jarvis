"""
CodeGraphSkill — 代码图谱分析 Skill

三层解耦：
 - code_graph.yaml → 元数据/配置
 - code_graph.md  → system prompt
 - code_graph_tool → 图谱引擎（独立 BaseTool）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import yaml

from ..base import Skill, SkillMeta, SkillResult

logger = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).parent


class CodeGraphSkill(Skill):
    """Code Graph Skill — 从 yaml/md 加载配置，引擎委托给 CodeGraphTool"""

    def __init__(self, project_root: str = "."):
        super().__init__()
        self._project_root = project_root
        self._meta: Optional[SkillMeta] = None
        self._system_prompt: Optional[str] = None

    @property
    def meta(self) -> SkillMeta:
        if self._meta is None:
            self._meta = self._load_meta()
        return self._meta

    @property
    def trigger_keywords(self) -> List[str]:
        return self._load_yaml().get("trigger_keywords", [])

    def get_system_prompt(self) -> str:
        if self._system_prompt is None:
            self._system_prompt = self._load_prompt()
        return self._system_prompt

    # ── 内部 ──

    def _load_yaml(self) -> dict:
        yaml_path = _THIS_DIR / "code_graph.yaml"
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"读取 code_graph.yaml 失败: {e}")
            return {}

    def _load_meta(self) -> SkillMeta:
        cfg = self._load_yaml()
        return SkillMeta(
            name=cfg.get("name", "code_graph"),
            display_name=cfg.get("display_name", "代码图谱分析"),
            description=cfg.get("description", ""),
            icon=cfg.get("icon", "🔗"),
            tags=cfg.get("tags", []),
            tool_hints=cfg.get("tools", []),
        )

    def _load_prompt(self) -> str:
        md_path = _THIS_DIR / "code_graph.md"
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                content = f.read()
            # 提取 ## system 和 ## analyze 之间的内容
            # 跳过 HTML 注释和一级标题
            lines = content.split("\n")
            start_idx = None
            end_idx = len(lines)
            for i, line in enumerate(lines):
                if line.strip().startswith("## system"):
                    start_idx = i + 1
                elif start_idx is not None and (
                    line.strip().startswith("## ") and not line.strip().startswith("## system")
                ):
                    end_idx = i
                    break
            if start_idx is not None:
                return "\n".join(lines[start_idx:end_idx]).strip()
            # 回退：跳过 HTML 注释和 # 标题后返回全部
            body = "\n".join(
                l for l in lines
                if not l.strip().startswith("<!--") and not l.strip().startswith("#")
            ).strip()
            return body or content
        except Exception as e:
            logger.warning(f"读取 code_graph.md 失败: {e}")
            return self._fallback_prompt()

    @staticmethod
    def _fallback_prompt() -> str:
        return """
## 代码图谱分析能力

优先使用 code_graph 工具获取项目结构信息：
- code_graph(action="search_symbol", name="...")
- code_graph(action="trace_callers", name="...")
- code_graph(action="analyze_impact", file="...")

信息不足时再用 code(action="read") 补充。
"""
