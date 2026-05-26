"""
message/message_list.py — 消息列表管理
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterator, List, Optional

from ..core.types import Message, Role


class MessageList:
    """消息列表 — 管理消息历史"""

    def __init__(self, max_messages: Optional[int] = None, max_tokens: Optional[int] = None):
        self._messages: List[Message] = []
        self._max_messages = max_messages
        self._max_tokens = max_tokens

    # ── 添加消息 ──

    def add(self, message: Message) -> None:
        self._messages.append(deepcopy(message))
        self._truncate_if_needed()

    def add_many(self, messages: List[Message]) -> None:
        for msg in messages:
            self._messages.append(deepcopy(msg))
        self._truncate_if_needed()

    def add_user(self, content: str) -> None:
        self.add(Message.user(content))

    def add_assistant(self, content: str, tool_calls: Optional[List] = None) -> None:
        self.add(Message.assistant(content, tool_calls))

    def add_tool(self, call_id: str, content: str) -> None:
        self.add(Message.tool(call_id, content))

    def add_system(self, content: str) -> None:
        self.add(Message.system(content))

    # ── 查询 ──

    def get_all(self) -> List[Message]:
        return deepcopy(self._messages)

    def get_last(self, n: int) -> List[Message]:
        return deepcopy(self._messages[-n:])

    def get_for_llm(self, include_system: bool = True) -> List[Dict]:
        """获取供 LLM 使用的格式"""
        msgs = self._messages
        if not include_system:
            msgs = [m for m in msgs if m.role != Role.SYSTEM]
        return [m.to_dict() for m in msgs]

    def get_system_messages(self) -> List[Message]:
        return [m for m in self._messages if m.role == Role.SYSTEM]

    def get_user_messages(self) -> List[Message]:
        return [m for m in self._messages if m.role == Role.USER]

    def get_last_user_message(self) -> Optional[Message]:
        for m in reversed(self._messages):
            if m.role == Role.USER:
                return m
        return None

    # ── 修改 ──

    def replace_last(self, message: Message) -> None:
        if self._messages:
            self._messages[-1] = deepcopy(message)
        else:
            self._messages.append(deepcopy(message))

    def truncate(self, keep_last: int) -> None:
        """保留最后 keep_last 条非系统消息，系统消息始终保留"""
        if keep_last <= 0:
            self._messages = self.get_system_messages()
            return
        system_msgs = self.get_system_messages()
        other_msgs = [m for m in self._messages if m.role != Role.SYSTEM]
        if len(other_msgs) > keep_last:
            other_msgs = other_msgs[-keep_last:]
        self._messages = system_msgs + other_msgs

    def clear(self) -> None:
        self._messages = []

    def remove_last(self) -> Optional[Message]:
        if self._messages:
            return self._messages.pop()
        return None

    # ── 自动截断 ──

    def _truncate_if_needed(self) -> None:
        if self._max_messages:
            system_count = len(self.get_system_messages())
            max_others = self._max_messages - system_count
            if max_others > 0 and len(self._messages) > self._max_messages:
                system_msgs = self.get_system_messages()
                other_msgs = [m for m in self._messages if m.role != Role.SYSTEM]
                if len(other_msgs) > max_others:
                    other_msgs = other_msgs[-max_others:]
                self._messages = system_msgs + other_msgs

        if self._max_tokens and self.estimate_tokens() > self._max_tokens:
            self._truncate_by_tokens()

    def _truncate_by_tokens(self) -> None:
        system_msgs = self.get_system_messages()
        other_msgs = [m for m in self._messages if m.role != Role.SYSTEM]
        kept_others = []
        current_tokens = self._count_tokens_messages(system_msgs)
        for msg in reversed(other_msgs):
            msg_tokens = self._estimate_message_tokens(msg)
            if current_tokens + msg_tokens <= self._max_tokens:
                kept_others.insert(0, msg)
                current_tokens += msg_tokens
            else:
                break
        self._messages = system_msgs + kept_others

    # ── Token 估算 ──

    def estimate_tokens(self) -> int:
        return self._count_tokens_messages(self._messages)

    def _count_tokens_messages(self, messages: List[Message]) -> int:
        return sum(self._estimate_message_tokens(m) for m in messages)

    def _estimate_message_tokens(self, message: Message) -> int:
        """
        估算消息的 token 数（支持中文）

        中文约 1.5 token/字，英文约 0.25 token/字符
        比简单 len//4 更准确，尤其适合中文场景
        """
        import re
        total_tokens = 0

        if message.content:
            chinese = len(re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]', message.content))
            other = len(message.content) - chinese
            total_tokens += int(chinese * 1.5 + other * 0.25)

        if message.tool_calls:
            text = str(message.tool_calls)
            chinese = len(re.findall(r'[\u4e00-\u9fff]', text))
            other = len(text) - chinese
            total_tokens += int(chinese * 1.5 + other * 0.25)

        if message.name:
            total_tokens += len(message.name) // 4

        return total_tokens + 10  # 基础开销

    # ── 容器协议 ──

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self) -> Iterator[Message]:
        return iter(self._messages)

    def __repr__(self) -> str:
        return f"MessageList({len(self._messages)}条)"
