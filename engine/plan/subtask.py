"""
engine/plan/subtask.py — 子任务规划与追踪

借鉴 PentAGI 的 Subtask 分解/执行/追踪模式：
  Agent 收到复杂任务 → LLM 拆成 N 个子任务 → 逐个执行 → 追踪完成状态

用法:
    planner = TaskPlanner(llm_client)
    plan = await planner.decompose(task="分析项目代码质量")
    # → [Subtask("扫描项目结构"), Subtask("审查依赖"), Subtask("质量评分")]

    planner.mark_done(plan[0])
    planner.mark_failed(plan[1], "找不到配置文件")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SubtaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Subtask:
    """单个子任务"""
    id: int
    title: str
    description: str = ""
    status: SubtaskStatus = SubtaskStatus.PENDING
    tools_hint: List[str] = field(default_factory=list)
    result: str = ""
    error: str = ""

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "title": self.title, "description": self.description,
            "status": self.status.value, "tools": self.tools_hint,
            "result": self.result[:200], "error": self.error[:200],
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "Subtask":
        return cls(
            id=d["id"], title=d["title"], description=d.get("description", ""),
            status=SubtaskStatus(d.get("status", "pending")),
            tools_hint=d.get("tools", []),
            result=d.get("result", ""), error=d.get("error", ""),
        )


class TaskPlanner:
    """
    任务规划器 — LLM 分解 + 人工/Agent 追踪。

    借鉴 PentAGI SubtaskList 设计：
      - decompose() — LLM 把复杂任务拆成子任务
      - get_plan_prompt() — 注入到 AgentLoop system prompt
      - mark_*() — 追踪执行状态
    """

    def __init__(self, llm_client: Any = None):
        self.llm_client = llm_client
        self._subtasks: List[Subtask] = []
        self._current_idx: int = 0

    async def decompose(self, task: str, max_subtasks: int = 5) -> List[Subtask]:
        """
        用 LLM 分解任务为子任务列表。

        Args:
            task: 主任务描述
            max_subtasks: 最多拆几个

        Returns:
            子任务列表（已排序）
        """
        if not self.llm_client:
            # 无 LLM → 单子任务模式
            self._subtasks = [Subtask(id=1, title=task, description=task)]
            return self._subtasks

        prompt = (
            f"将以下任务分解为 {max_subtasks} 个以内的子任务。\n"
            f"每个子任务应该是独立可完成的一步操作。\n\n"
            f"任务: {task}\n\n"
            f"返回 JSON 格式:\n"
            f'[{{"title": "子任务名", "description": "具体做什么", "tools": ["工具1", "工具2"]}}]\n\n'
            f"只返回 JSON 数组，不要其他内容:"
        )

        try:
            response = await self.llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3, max_tokens=1000,
            )
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "[]")
            data = json.loads(self._extract_json(content))
        except Exception as e:
            logger.warning(f"子任务分解失败，使用单任务模式: {e}")
            self._subtasks = [Subtask(id=1, title=task, description=task)]
            return self._subtasks

        self._subtasks = []
        for i, item in enumerate(data[:max_subtasks], 1):
            self._subtasks.append(Subtask(
                id=i,
                title=item.get("title", f"Step {i}"),
                description=item.get("description", ""),
                tools_hint=item.get("tools", []),
            ))

        # ⭐ 简单任务检测：只有1步且标题和任务基本一致 → 不分解
        if len(self._subtasks) == 1:
            t1 = self._subtasks[0].title[:30].lower()
            t2 = task[:30].lower()
            if t1 == t2 or t1 in t2 or t2 in t1:
                logger.info(f"📋 任务简单，无需分解")
                self._subtasks = []
                return self._subtasks

        logger.info(f"📋 任务分解: {len(self._subtasks)} 个子任务")
        for st in self._subtasks:
            logger.info(f"  [{st.id}] {st.status.value} {st.title}")

        return self._subtasks

    def get_plan_prompt(self) -> str:
        """生成注入到 system prompt 的计划文本"""
        if not self._subtasks:
            return ""

        lines = ["## 📋 任务计划", ""]
        for st in self._subtasks:
            icon = {"pending": "⬜", "in_progress": "🔄", "done": "✅",
                    "failed": "❌", "skipped": "⏭️"}.get(st.status.value, "❓")
            lines.append(f"{icon} [{st.id}/{len(self._subtasks)}] {st.title}")
            if st.error:
                lines.append(f"   失败原因: {st.error[:80]}")
        lines.append("")
        lines.append("按顺序完成每个子任务。完成一个后继续下一个。")
        return "\n".join(lines)

    # ── 状态追踪 ──

    def mark_in_progress(self, subtask_id: int) -> None:
        self._set_status(subtask_id, SubtaskStatus.IN_PROGRESS)
        self._current_idx = max(self._current_idx, subtask_id)

    def mark_done(self, subtask_id: int, result: str = "") -> None:
        self._set_status(subtask_id, SubtaskStatus.DONE, result=result)

    def mark_failed(self, subtask_id: int, error: str = "") -> None:
        self._set_status(subtask_id, SubtaskStatus.FAILED, error=error)

    def mark_skipped(self, subtask_id: int) -> None:
        self._set_status(subtask_id, SubtaskStatus.SKIPPED)

    def _set_status(self, subtask_id: int, status: SubtaskStatus,
                    result: str = "", error: str = ""):
        for st in self._subtasks:
            if st.id == subtask_id:
                st.status = status
                if result:
                    st.result = result
                if error:
                    st.error = error
                return

    # ── 查询 ──

    def get_current(self) -> Optional[Subtask]:
        """获取当前应执行的子任务（第一个 PENDING）"""
        for st in self._subtasks:
            if st.status in (SubtaskStatus.PENDING, SubtaskStatus.IN_PROGRESS):
                return st
        return None

    def get_next(self) -> Optional[Subtask]:
        """获取下一个待执行子任务"""
        for st in self._subtasks:
            if st.status == SubtaskStatus.PENDING:
                return st
        return None

    def is_all_done(self) -> bool:
        return all(st.status == SubtaskStatus.DONE for st in self._subtasks) if self._subtasks else True

    def progress(self) -> Dict:
        total = len(self._subtasks)
        done = sum(1 for st in self._subtasks if st.status == SubtaskStatus.DONE)
        return {"total": total, "done": done, "percent": f"{done/total:.0%}" if total else "0%"}

    def to_dict(self) -> Dict:
        return {
            "subtasks": [st.to_dict() for st in self._subtasks],
            "current_idx": self._current_idx,
            "progress": self.progress(),
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "TaskPlanner":
        planner = cls()
        planner._subtasks = [Subtask.from_dict(d) for d in data.get("subtasks", [])]
        planner._current_idx = data.get("current_idx", 0)
        return planner

    @staticmethod
    def _extract_json(text: str) -> str:
        """从 LLM 响应中提取 JSON，支持多种格式"""
        text = text.strip()
        # Markdown 代码块
        if text.startswith("```"):
            lines = text.split("\n")
            return "\n".join(lines[1:-1])
        # JSON 数组
        if text.startswith("["):
            return text
        # 纯文本步骤列表: "1. xxx\n2. xxx" 格式
        import re
        items = re.findall(r'^\d+[\.\)、]\s*(.+)', text, re.MULTILINE)
        if items and len(items) >= 2:
            return json.dumps([
                {"title": it.split("：")[0].split(":")[0].strip()[:50],
                 "description": it, "tools": []}
                for it in items
            ])
        # 带 - 或 * 的列表
        items = re.findall(r'^[-*]\s+(.+)', text, re.MULTILINE)
        if items and len(items) >= 2:
            return json.dumps([
                {"title": it[:50], "description": it, "tools": []}
                for it in items
            ])
        return text
