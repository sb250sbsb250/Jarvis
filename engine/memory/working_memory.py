"""
engine/memory/working_memory.py — 工作记忆

LLM 的"便签本"，追踪当前任务中已进行的操作和已获取的信息，
防止重复读取、重复尝试已失败的方法。

用法：
    wm = WorkingMemory()
    wm.record_read("app.py", "第1-50行，用户认证逻辑")
    wm.record_error("excel", {"path": "a.xlsx"}, "文件不存在", round=3)
    prompt = wm.get_reminder()  # → 注入到 system prompt
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class WorkingMemory:
    """轻量级工作记忆 — 追踪当前任务的读取、写入、错误和方案尝试"""

    def __init__(self):
        # 已读取的文件/数据: {path_or_key: summary_or_first_lines}
        self.reads: Dict[str, str] = {}

        # 已写入的文件: {path: summary}
        self.writes: Dict[str, str] = {}

        # 错误记录: [{tool, args_summary, error, round}]
        self.errors: List[Dict[str, Any]] = []

        # 尝试过的方案: [{description, result, round}]
        self.approaches: List[Dict[str, Any]] = []

        # 持续的错误计数（用于自我反思触发）
        self.consecutive_errors: int = 0

    # ── 记录方法 ──

    def record_read(self, path: str, summary: str) -> None:
        """记录一次文件/数据读取"""
        # 如果已经记录过且摘要更长，保留旧的（更完整）
        if path in self.reads and len(self.reads[path]) > len(summary):
            return
        self.reads[path] = summary
        logger.debug(f"[WM] 记录读取: {path} ({len(summary)}字符)")

    def record_write(self, path: str, summary: str = "") -> None:
        """记录一次文件写入"""
        self.writes[path] = summary or f"已写入 {path}"
        logger.debug(f"[WM] 记录写入: {path}")

    def record_error(self, tool: str, args: Dict, error: str, round_idx: int) -> None:
        """记录一次工具调用错误"""
        args_summary = json.dumps(args, ensure_ascii=False)[:120]
        self.errors.append({
            "tool": tool,
            "args": args_summary,
            "error": error[:200],
            "round": round_idx,
        })
        self.consecutive_errors += 1
        logger.debug(f"[WM] 记录错误 [{tool}]: {error[:60]}...")

    def record_approach(self, description: str, result: str, round_idx: int) -> None:
        """记录一次方案尝试"""
        self.approaches.append({
            "description": description[:100],
            "result": result[:200],
            "round": round_idx,
        })
        logger.debug(f"[WM] 记录方案: {description[:40]}... → {result[:40]}...")

    def clear_errors(self) -> None:
        """清空连续错误计数（当某步成功时调用）"""
        self.consecutive_errors = 0

    def clear(self) -> None:
        """清空所有工作记忆（新任务时调用）"""
        self.reads.clear()
        self.writes.clear()
        self.errors.clear()
        self.approaches.clear()
        self.consecutive_errors = 0

    # ── 生成提醒 ──

    def get_reminder(self) -> str:
        """
        生成给 LLM 的"不要重复操作"提醒。

        上下文膨胀控制：
          - 错误只保留最近 2 条
          - 读取/写入只保留最近 3 条
          - 方案只保留最近 3 条
        """
        parts = []

        # — 已读取（最近 3 条）—
        if self.reads:
            recent = list(self.reads.items())[-3:]
            lines = []
            for path, summary in recent:
                s = summary[:60] + "..." if len(summary) > 60 else summary
                lines.append(f"    - {path}: {s}")
            parts.append("📖 已读取:\n" + "\n".join(lines))

        # — 已写入（最近 3 条）—
        if self.writes:
            recent = list(self.writes.items())[-3:]
            lines = [f"    - {path}" for path in recent]
            parts.append("✏️ 已写入:\n" + "\n".join(lines))

        # — 最近错误（最近 2 条）—
        recent_errors = self.errors[-2:]
        if recent_errors:
            lines = []
            for e in recent_errors:
                error_short = e["error"][:60]
                lines.append(f"    - [{e['tool']}] 第{e['round']}轮: {error_short}")
            parts.append("❌ 最近的错误:\n" + "\n".join(lines))

        # — 尝试过的方案（最近 3 条）—
        recent_approaches = self.approaches[-3:]
        if recent_approaches:
            lines = []
            for a in recent_approaches:
                result_short = a["result"][:60]
                lines.append(f"    - {a['description']}: {result_short}")
            parts.append("🔄 尝试过的方案:\n" + "\n".join(lines))

        if not parts:
            return ""

        reminder = "## 📋 工作记忆（已完成的操作，不要重复）\n" + "\n\n".join(parts)
        return reminder

    def need_reflection(self, threshold: int = 3) -> bool:
        """是否需要触发自我反思"""
        return self.consecutive_errors >= threshold

    def get_reflection_prompt(self) -> str:
        """生成自我反思提示"""
        if not self.errors:
            return ""

        recent_errors = self.errors[-3:]
        error_detail = "\n".join(
            f"  {i+1}. 工具 [{e['tool']}] 参数: {e['args'][:80]} → {e['error'][:100]}"
            for i, e in enumerate(recent_errors)
        )

        return (
            f"### 🧠 执行反思\n"
            f"你已经连续 {self.consecutive_errors} 次操作未达到预期。暂停一下，分析原因：\n\n"
            f"最近 3 次操作:\n{error_detail}\n\n"
            f"请思考：\n"
            f"1. 每一步的目的是什么？实际发生了什么？\n"
            f"2. 失败的根本原因是什么？（路径错误？数据格式不对？工具不支持？）\n"
            f"3. 还有哪些完全不同的方式可以达成目标？\n\n"
            f"思考完成后，给出全新的方案并执行。不要重复刚才的失败方式。"
        )

    def to_dict(self) -> Dict:
        return {
            "reads": self.reads,
            "writes": self.writes,
            "errors": self.errors[-10:],
            "approaches": self.approaches[-10:],
            "consecutive_errors": self.consecutive_errors,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "WorkingMemory":
        wm = cls()
        wm.reads = data.get("reads", {})
        wm.writes = data.get("writes", {})
        wm.errors = data.get("errors", [])
        wm.approaches = data.get("approaches", [])
        wm.consecutive_errors = data.get("consecutive_errors", 0)
        return wm
