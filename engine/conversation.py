"""
engine/conversation.py — 连贯对话管理器 (v3.0)

双消息格式:
  _messages (llm) — 完整 LLM 格式（system/tool_call_id/content...）
  display_messages — 前端展示用（去除 system 消息，简化 tool 结果）

压缩追踪:
  compressed_until — 已压缩到的消息索引（避免重复 LLM 摘要）
  compressed_summary — 历史操作日志摘要文本
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
        self._loop_factory = loop_factory
        self.session_id: str = session_id
        self._messages: List[Dict] = []       # LLM 格式的完整对话历史
        self._turn_count: int = 0

        # 压缩状态
        self.compressed_until: int = 0
        self.compressed_summary: str = ""

        # Token 统计
        self._estimated_tokens: int = 0
        self._TOTAL_TOKEN_WARN = 100_000
        self._TOTAL_TOKEN_HARD = 110_000

    # ── 消息访问 ──

    @property
    def messages(self) -> List[Dict]:
        """LLM 格式的消息列表（深拷贝）"""
        import copy
        return copy.deepcopy(self._messages)

    @property
    def display_messages(self) -> List[Dict]:
        """前端展示用消息列表 — 去掉 system/压缩摘要，简化 tool 结果"""
        result = []
        for m in self._messages:
            role = m.get("role", "")
            if role in ("system",):
                continue  # 前端不需要 system 消息

            display = {"role": role}
            if role == "tool":
                # 简化 tool 结果展示
                display["content"] = m.get("content", "")[:500]
                display["tool_call_id"] = m.get("tool_call_id", "")
            elif role == "assistant":
                display["content"] = m.get("content", "")
                if m.get("tool_calls"):
                    display["tool_calls"] = [
                        tc.get("function", {}).get("name", "?")
                        for tc in m.get("tool_calls", [])
                    ]
            else:
                display["content"] = m.get("content", "")

            result.append(display)
        return result

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def token_estimate(self) -> int:
        return self._estimated_tokens

    # ── 对话 ──

    async def chat(
        self,
        task: str,
        working_dir: str = ".",
        on_event: Optional[Callable[[str, Dict], Awaitable[None]]] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        发送一条消息，自动携带前面所有对话历史。

        Args:
            task: 用户输入
            working_dir: 工作目录
            on_event: 事件回调
            model: 指定使用的模型名称（可选）

        Returns:
            AgentLoop.run() 的完整返回值
        """
        self._turn_count += 1
        loop = self._loop_factory()

        history = self._messages if self._turn_count > 1 else None

        logger.info(
            f"🧵 [会话 {self.session_id[:8]}] 第 {self._turn_count} 轮 | "
            f"历史 {len(self._messages)} 条消息 | "
            f"压缩到 {self.compressed_until} | "
            f"估算 {self._estimated_tokens} tokens"
        )

        result = await loop.run(
            task=task,
            working_dir=working_dir,
            history=history,
            on_event=on_event,
            skip_last_user=False,
            compressed_until=self.compressed_until,
            compressed_summary=self.compressed_summary,
            model_override=model,
        )

        # ── 保存本轮完整消息列表 ──
        new_messages = result.get("messages", [])
        if new_messages:
            self._messages = new_messages
            self._update_token_estimate()

        # ── 持久化压缩状态 ──
        self.compressed_until = result.get("compressed_until", self.compressed_until)
        self.compressed_summary = result.get("compressed_summary", self.compressed_summary)

        logger.info(
            f"🧵 第 {self._turn_count} 轮结束: "
            f"AgentLoop 返回 {len(new_messages)} 条消息, "
            f"compressed_until={self.compressed_until}, "
            f"success={result.get('success')}, rounds={result.get('rounds')}"
        )

        return result

    def _update_token_estimate(self) -> None:
        try:
            from .core.token_estimator import estimate_message_dict
            total = sum(estimate_message_dict(m) for m in self._messages)
        except Exception:
            total = sum(len(str(m.get("content", ""))) for m in self._messages) // 4
            total += len(self._messages) * 15

        self._estimated_tokens = total

        if total > self._TOTAL_TOKEN_HARD:
            logger.warning(f"💰 Token 水位 {total}，强制压缩历史")
        elif total > self._TOTAL_TOKEN_WARN:
            logger.info(f"💰 Token 水位 {total}/{self._TOTAL_TOKEN_HARD}")

    # ── 管理 ──

    async def reset(self) -> None:
        """重置对话"""
        self._messages.clear()
        self._turn_count = 0
        self._estimated_tokens = 0
        self.compressed_until = 0
        self.compressed_summary = ""
        logger.info(f"🧵 [会话 {self.session_id[:8]}] 已重置")

    def import_history(self, messages: List[Dict]) -> None:
        """从消息列表导入已有对话历史"""
        self._messages = list(messages)
        self._turn_count = sum(1 for m in messages if m.get("role") == "user")
        self._update_token_estimate()
        logger.info(f"🧵 [会话 {self.session_id[:8]}] 导入 {len(messages)} 条历史")

    def summary(self) -> Dict[str, Any]:
        """会话摘要"""
        return {
            "session_id": self.session_id,
            "turn_count": self._turn_count,
            "message_count": len(self._messages),
            "estimated_tokens": self._estimated_tokens,
            "compressed_until": self.compressed_until,
            "compressed_summary_len": len(self.compressed_summary) if self.compressed_summary else 0,
        }
