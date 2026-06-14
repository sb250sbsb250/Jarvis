"""
engine/core/script_detector.py — 脚本建议检测器

Claude Code 风格：当 LLM 连续多次用同一类工具失败时，
建议它把操作写入脚本文件再执行，而非反复在工具调用中嵌入复杂代码。

触发条件（任一）:
1. 连续 3 次同一工具名 + 全部失败
2. 连续 3 次 shell_run 失败（各种命令）
"""

import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


class ScriptSuggestionDetector:
    """
    脚本建议检测器

    集成在 agent_loop 的工具执行结果处理后。
    """

    # 工具名 -> 分类标签
    TOOL_CATEGORIES = {
        "shell_run": "shell",
        "shell_execute": "shell",
        "code_write": "code_edit",
        "code_append": "code_edit",
        "file_write": "file_edit",
    }

    SUGGESTION_TEMPLATE = (
        "[系统建议] 你已经连续 {count} 次使用 `{tool_name}` 失败。\n"
        "建议: 将操作写入一个临时脚本文件（如 `_tmp_script.py` 或 `_tmp_script.sh`），\n"
        "然后用 `shell_run(command=\"python _tmp_script.py\")` 执行。\n"
        "这样可以避免 JSON 转义问题，也便于调试。执行完毕后记得清理临时文件。"
    )

    def __init__(self, threshold: int = 3):
        self._threshold = threshold
        # 追踪: [(tool_name, is_success), ...]
        self._recent_calls: List[Tuple[str, bool]] = []
        self._suggestion_injected: bool = False

    def record(self, tool_name: str, is_success: bool) -> None:
        """记录一次工具调用结果"""
        self._recent_calls.append((tool_name, is_success))
        # 只保留最近 10 条
        if len(self._recent_calls) > 10:
            self._recent_calls = self._recent_calls[-10:]
        # 成功后重置建议标记
        if is_success:
            self._suggestion_injected = False

    def should_suggest(self) -> Optional[str]:
        """
        检查是否应该注入脚本建议。

        Returns:
            建议文本（str），或 None（不需要建议）
        """
        if self._suggestion_injected:
            return None
        if len(self._recent_calls) < self._threshold:
            return None

        recent = self._recent_calls[-self._threshold:]

        names = [n for n, _ in recent]
        successes = [s for _, s in recent]

        if all(not s for s in successes):
            # 全部失败
            if len(set(names)) == 1:
                # 条件1: 连续 N 次同一工具 + 全部失败
                self._suggestion_injected = True
                return self.SUGGESTION_TEMPLATE.format(
                    count=self._threshold,
                    tool_name=names[0],
                )

            # 条件2: 连续 shell 类工具失败（命令不同但都是 shell）
            categories = [self.TOOL_CATEGORIES.get(n, "") for n in names]
            if all(c == "shell" for c in categories):
                self._suggestion_injected = True
                return self.SUGGESTION_TEMPLATE.format(
                    count=self._threshold,
                    tool_name="shell_run",
                )

        return None

    def reset(self) -> None:
        """重置状态（新 run 开始时调用）"""
        self._recent_calls.clear()
        self._suggestion_injected = False
