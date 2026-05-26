"""
会话压缩器 - 当消息过长时自动压缩
"""

import logging
from typing import Optional, List, Dict, Any, Callable, Awaitable

from ..message.message_list import MessageList
from ..core.types import Message, Role

logger = logging.getLogger(__name__)


class Compactor:
    """
    会话压缩器

    支持多种压缩策略：
    - sliding_window: 滑动窗口，保留最近 N 条
    - summary: 使用 LLM 生成摘要
    - importance: 基于重要性评分保留
    """

    def __init__(
        self,
        llm_client: Optional[Any] = None,
        max_tokens: int = 8000,
        keep_recent: int = 10,
        strategy: str = "sliding_window",
    ):
        """
        Args:
            llm_client: LLM 客户端（用于 summary 策略）
            max_tokens: 最大 token 数
            keep_recent: 保留最近的消息数
            strategy: 压缩策略 (sliding_window, summary, importance)
        """
        self.llm_client = llm_client
        self.max_tokens = max_tokens
        self.keep_recent = keep_recent
        self.strategy = strategy

    async def compact(self, messages: MessageList) -> bool:
        """
        压缩消息列表

        Returns:
            是否进行了压缩
        """
        tokens = messages.estimate_tokens()
        if tokens <= self.max_tokens:
            return False

        logger.info(f"Compacting messages: {tokens} tokens > {self.max_tokens}")

        if self.strategy == "sliding_window":
            return await self._compact_sliding_window(messages)
        elif self.strategy == "summary":
            return await self._compact_summary(messages)
        else:
            return await self._compact_sliding_window(messages)

    async def _compact_sliding_window(self, messages: MessageList) -> bool:
        """滑动窗口压缩"""
        # 保留系统消息
        system_msgs = messages.get_system_messages()

        # 保留最近的 N 条非系统消息
        other_msgs = [m for m in messages.get_all() if m.role != Role.SYSTEM]
        if len(other_msgs) > self.keep_recent:
            other_msgs = other_msgs[-self.keep_recent:]

        # 重建消息列表
        messages.clear()
        for msg in system_msgs:
            messages.add(msg)
        for msg in other_msgs:
            messages.add(msg)

        logger.info(f"Sliding window compacted to {len(messages)} messages")
        return True

    async def _compact_summary(self, messages: MessageList) -> bool:
        """使用摘要压缩"""
        if not self.llm_client:
            logger.warning("No LLM client for summary compaction, falling back to sliding window")
            return await self._compact_sliding_window(messages)

        # 分离系统消息和需要压缩的消息
        system_msgs = messages.get_system_messages()
        other_msgs = [m for m in messages.get_all() if m.role != Role.SYSTEM]

        # 保留最近的 keep_recent 条
        keep_msgs = other_msgs[-self.keep_recent:] if len(other_msgs) > self.keep_recent else other_msgs
        compact_msgs = other_msgs[:-self.keep_recent] if len(other_msgs) > self.keep_recent else []

        if not compact_msgs:
            return False

        # 生成摘要
        summary = await self._generate_summary(compact_msgs)

        # 创建摘要消息
        summary_message = Message.system(
            f"[Previous conversation summary]\n{summary}"
        )

        # 重建消息列表
        messages.clear()
        for msg in system_msgs:
            messages.add(msg)
        messages.add(summary_message)
        for msg in keep_msgs:
            messages.add(msg)

        logger.info(f"Summary compacted to {len(messages)} messages")
        return True

    async def _generate_summary(self, messages: List[Message]) -> str:
        """生成消息摘要"""
        if not self.llm_client:
            return f"[{len(messages)} messages omitted due to length]"

        # 构建摘要提示
        conversation_text = "\n".join([
            f"{m.role.value}: {m.content or '[tool call]'}"
            for m in messages
            if m.content
        ])

        prompt = f"""Please summarize the following conversation concisely:

{conversation_text}

Summary:"""

        try:
            response = await self.llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                stream=False,
            )
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content.strip()
        except Exception as e:
            logger.error(f"Failed to generate summary: {e}")
            return f"[{len(messages)} messages omitted]"


class AutoCompactor:
    """自动压缩器 - 定期检查和压缩"""

    def __init__(
        self,
        compactor: Compactor,
        check_interval_steps: int = 5,
        auto_compact: bool = True,
    ):
        self.compactor = compactor
        self.check_interval_steps = check_interval_steps
        self.auto_compact = auto_compact
        self._step_counter = 0

    async def check_and_compact(self, messages: MessageList) -> bool:
        """检查并压缩"""
        if not self.auto_compact:
            return False

        self._step_counter += 1
        if self._step_counter >= self.check_interval_steps:
            self._step_counter = 0
            return await self.compactor.compact(messages)

        return False

    def reset_counter(self) -> None:
        """重置计数器"""
        self._step_counter = 0
