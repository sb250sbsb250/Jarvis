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
        todo_manager: Optional[Any] = None,
        llm_client: Optional[Any] = None,
    ):
        self._loop_factory = loop_factory
        self.session_id: str = session_id
        self._messages: List[Dict] = []       # LLM 格式的完整对话历史
        self._turn_count: int = 0
        self._todo_manager = todo_manager  # 会话级别的 TodoManager
        self._llm_client = llm_client  # 用于任务完成后压缩历史

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
        mode: str = "coding",
    ) -> Dict[str, Any]:
        """
        发送一条消息，自动携带前面所有对话历史。

        Args:
            task: 用户输入
            working_dir: 工作目录
            on_event: 事件回调
            model: 指定使用的模型名称（可选）
            mode: 工作模式（"coding" / "workbuddy" / "video"）

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
            mode=mode,
        )

        # ── 保存本轮完整消息列表 ──
        new_messages = result.get("messages", [])
        if new_messages:
            self._messages = new_messages
            self._update_token_estimate()

        # ── 持久化压缩状态 ──
        self.compressed_until = result.get("compressed_until", self.compressed_until)
        self.compressed_summary = result.get("compressed_summary", self.compressed_summary)

        # ── 任务完成后压缩历史 ──
        if result.get("success") and self._llm_client:
            await self._compress_completed_turn()

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

    # ── 任务完成压缩 ──

    async def _compress_completed_turn(self) -> None:
        """
        任务完成后压缩历史：将已完成的轮次压缩为摘要，只保留最近 1 轮完整消息。

        流程：
          1. 找到最后一个 user 消息的位置（当前轮起点）
          2. 找到倒数第二个 user 消息的位置（上一轮起点）
          3. 将上一轮之前的所有消息发给 LLM 生成摘要
          4. 用摘要替换那些消息，更新 compressed_until/compressed_summary
        """
        msgs = self._messages
        if len(msgs) < 6:
            return  # 太少，不压缩

        # 找所有 user 消息的位置
        user_indices = [
            i for i, m in enumerate(msgs) if m.get("role") == "user"
        ]
        if len(user_indices) < 2:
            return  # 只有一轮，不压缩

        # 要压缩的范围：[0, 倒数第二轮的起点)
        # 保留：最近一轮 [倒数第二轮的起点, end]
        keep_from = user_indices[-2]
        to_compress = msgs[:keep_from]

        if not to_compress:
            return

        # 如果已经压缩过到这个位置，跳过
        if self.compressed_until >= keep_from:
            return

        # 构建压缩输入（只发要压缩的消息，不传完整对话）
        dialogue_parts = []
        for m in to_compress:
            role = m.get("role", "?")
            content = str(m.get("content", ""))[:300]
            if m.get("tool_calls"):
                tools = [tc.get("function", {}).get("name", "?") for tc in m.get("tool_calls", [])]
                content += f"【工具: {', '.join(tools)}】"
            if role in ("user", "assistant"):
                dialogue_parts.append(f"[{role}] {content}")

        if not dialogue_parts:
            return

        # LLM 压缩调用（轻量级：只传要压缩的部分）
        prompt = (
            "将以下对话记录压缩为**操作日志**。每条包含：做了什么 → 结果。\n\n"
            "输出格式：每行一条，格式为：\n"
            "  - {状态} {操作}: {做了什么} → {结果/发现}\n\n"
            "状态: ✅成功 ❌失败 ⚠️部分\n\n"
            "要求：\n"
            "1. 每条一句话，保留关键信息（文件路径、数据量、错误原因）\n"
            "2. 丢弃冗余过程（重试、调试、重复操作）\n"
            "3. 控制在100-200字\n\n"
            + "\n".join(dialogue_parts)
            + "\n\n操作日志:"
        )

        try:
            resp = await self._llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
                temperature=0.1,
            )
            summary = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            logger.warning(f"压缩 LLM 调用失败: {e}")
            summary = ""

        if not summary or len(summary) < 10:
            # fallback: 规则摘要
            summary = self._rule_compress(to_compress)

        if not summary:
            return

        # 更新压缩状态
        prefix = "## 📜 已完成任务摘要\n" if not self.compressed_summary else ""
        if self.compressed_summary:
            self.compressed_summary = f"{self.compressed_summary}\n\n{summary}"
        else:
            self.compressed_summary = f"{prefix}{summary}"
        self.compressed_until = keep_from

        # 截断消息列表：只保留最近一轮
        self._messages = msgs[keep_from:]
        self._update_token_estimate()

        compressed_count = len(to_compress)
        remaining = len(self._messages)
        logger.info(
            f"📐 任务完成压缩: {compressed_count}条 → 摘要{len(summary)}字 | "
            f"保留 {remaining} 条 (最近1轮)"
        )

    @staticmethod
    def _rule_compress(messages: List[Dict]) -> str:
        """规则压缩 — LLM 不可用时的 fallback。"""
        turns: List[List[Dict]] = []
        current: List[Dict] = []
        for m in messages:
            if m.get("role") == "user" and current:
                turns.append(current)
                current = []
            current.append(m)
        if current:
            turns.append(current)

        lines = []
        for i, turn in enumerate(turns, 1):
            user_text = ""
            tools = []
            result_text = ""
            for m in turn:
                role = m.get("role", "")
                if role == "user":
                    user_text = str(m.get("content", ""))[:80].replace("\n", " ")
                elif role == "assistant":
                    for tc in m.get("tool_calls", []):
                        tools.append(tc.get("function", {}).get("name", "?"))
                    content = m.get("content", "")
                    if content:
                        result_text = str(content)[:100].replace("\n", " ")
            indicator = f"[{', '.join(tools[:3])}]" if tools else "[直接回答]"
            line = f"- R{i} {indicator} {user_text}"
            if result_text:
                line += f" → {result_text}"
            lines.append(line)
        return "\n".join(lines) if lines else ""

    # ── 管理 ──

    async def reset(self) -> None:
        """重置对话"""
        self._messages.clear()
        self._turn_count = 0
        self._estimated_tokens = 0
        self.compressed_until = 0
        self.compressed_summary = ""
        # 重置 Todo 状态
        if self._todo_manager is not None:
            self._todo_manager.reset()
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
