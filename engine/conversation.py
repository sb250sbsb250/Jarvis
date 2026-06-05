"""
engine/conversation.py — 连贯对话管理器

核心思想：messages 列表就是最完整的记忆。
每次 run() 之后保存完整消息列表，下次 run() 直接传回。
不需要额外的记忆抽象层。
"""

import logging
from typing import Any, Dict, List, Optional, Callable, Awaitable

from .agent_loop import AgentLoop

logger = logging.getLogger(__name__)


class ConversationSession:
    """
    连贯对话管理器。

    让 AgentLoop 的 messages 在多次 run() 之间保持连贯。
    会话内的每一轮对话自动继承前面的完整上下文，
    包括所有中间工具调用结果。

    用法:
        session = ConversationSession(loop_factory=create_agent_loop)
        await session.chat("提取这个PDF的内容")
        await session.chat("把内容转成Word")  # 自动带上前面的上下文
        await session.reset()  # 可选：重置对话
    """

    def __init__(
        self,
        loop_factory: Callable[[], AgentLoop],
        session_id: str = "",
    ):
        """
        Args:
            loop_factory: 返回新 AgentLoop 实例的工厂函数
            session_id: 可选会话标识
        """
        self._loop_factory = loop_factory
        self.session_id: str = session_id
        self._messages: List[Dict] = []       # 累积的完整对话历史
        self._turn_count: int = 0

        # Token 统计
        self._estimated_tokens: int = 0
        self._TOTAL_TOKEN_WARN = 100_000      # 超 10K 警告
        self._TOTAL_TOKEN_HARD = 110_000      # 超 11K 强制摘要

    @property
    def messages(self) -> List[Dict]:
        """当前完整消息列表（深拷贝，外部不应修改）"""
        import copy
        return copy.deepcopy(self._messages)

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def token_estimate(self) -> int:
        return self._estimated_tokens

    async def chat(
        self,
        task: str,
        working_dir: str = ".",
        on_event: Optional[Callable[[str, Dict], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """
        发送一条消息，自动携带前面所有对话历史。

        Args:
            task: 用户输入
            working_dir: 工作目录
            on_event: 事件回调

        Returns:
            AgentLoop.run() 的完整返回值
            (包含 "messages": 本轮完整消息列表)
        """
        self._turn_count += 1
        loop = self._loop_factory()

        # ── 第 1 轮: 不传 history，AgentLoop 自己构建 ──
        # ── 第 N 轮: 传前面所有 messages ──
        history = self._messages if self._turn_count > 1 else None

        logger.info(
            f"🧵 [会话 {self.session_id[:8]}] 第 {self._turn_count} 轮 | "
            f"历史 {len(self._messages)} 条消息 | "
            f"估算 {self._estimated_tokens} tokens"
        )

        result = await loop.run(
            task=task,
            working_dir=working_dir,
            history=history,
            on_event=on_event,
            skip_last_user=False,  # history 是前轮完整对话，最后一条 user 不是本次任务
        )

        # ── 关键！保存本轮完整消息列表 ──
        new_messages = result.get("messages", [])
        if new_messages:
            self._messages = new_messages
            self._update_token_estimate()
            logger.info(
                f"🧵 第 {self._turn_count} 轮结束: "
                f"AgentLoop 返回 {len(new_messages)} 条, "
                f"结果 success={result.get('success')} rounds={result.get('rounds')}"
            )
        else:
            logger.warning(
                f"🧵 第 {self._turn_count} 轮: AgentLoop 未返回 messages! "
                f"result keys={list(result.keys())}"
            )

        return result

    def _update_token_estimate(self) -> None:
        """更新 token 估算并检测是否需要压缩"""
        try:
            from .core.token_estimator import estimate_message_dict
            total = sum(estimate_message_dict(m) for m in self._messages)
        except Exception:
            # 粗略估算: 1 token ≈ 4 字符
            total = sum(len(str(m.get("content", ""))) for m in self._messages) // 4
            total += len(self._messages) * 15  # 每条消息开销

        self._estimated_tokens = total

        if total > self._TOTAL_TOKEN_HARD:
            logger.warning(
                f"💰 Token 水位 {total}，强制压缩历史"
            )
        elif total > self._TOTAL_TOKEN_WARN:
            logger.info(
                f"💰 Token 水位 {total}/{self._TOTAL_TOKEN_HARD}"
            )

    async def reset(self) -> None:
        """重置对话（清空历史，新对话从头开始）"""
        self._messages.clear()
        self._turn_count = 0
        self._estimated_tokens = 0
        logger.info(f"🧵 [会话 {self.session_id[:8]}] 已重置")

    def import_history(self, messages: List[Dict]) -> None:
        """从消息列表导入已有对话历史"""
        self._messages = list(messages)
        self._turn_count = sum(1 for m in messages if m.get("role") == "user")
        self._update_token_estimate()
        logger.info(
            f"🧵 [会话 {self.session_id[:8]}] 导入 {len(self._messages)} 条历史"
        )

    def summary(self) -> Dict[str, Any]:
        """会话摘要"""
        return {
            "session_id": self.session_id,
            "turn_count": self._turn_count,
            "message_count": len(self._messages),
            "estimated_tokens": self._estimated_tokens,
        }
