"""
message/message_list.py — 永不截断的消息列表

设计原则：
  1. 永远不截断消息 — 所有消息完整保存
  2. 依赖 LLM 长上下文（128K~1M token）
  3. 按 round_id 分组保证 tool_calls ↔ tool 配对不拆散
  4. 超出 token 预算时，按 round_id 整轮丢弃旧轮次，绝不拆散同一轮
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any, Dict, List, Optional

from ..core.types import Message, Role
from ..core.token_estimator import estimate_message_tokens, estimate_message_dict

logger = logging.getLogger(__name__)


class MessageList:
    """
    永不截断的消息列表。

    职责：
      - 完整存储所有消息（user / assistant / tool / system）
      - 按 round_id 分组保证 tool_calls ↔ tool 配对
      - 提供 get_for_llm() 输出给 LLM API
      - 超出预算时仅警告，不丢弃任何消息
      - 提供 get_recent_summary() 给界面展示
    """

    # 工具操作类型（用于重要性标记，只影响展示排序，不影响存储）
    TOOL_ACTIONS_LOW_IMPORTANCE = {"read", "list", "search", "get"}

    def __init__(self, max_tokens: int = 1_000_000, min_working_reserve: int = 200):
        self._messages: List[Message] = []    # 完整存储（扁平列表）
        self._round_counter: int = 0           # 当前轮次 ID
        self._round_map: Dict[int, List[Message]] = {}  # round_id → [messages]
        self.max_tokens = max_tokens           # 用于 get_for_llm 预算控制
        self.min_working_reserve = min_working_reserve

        # 任务边界（供 TaskContextManager 使用）
        self._task_boundaries: List[int] = []
        self._task_switch_injected: bool = False

        # 延迟导入 TaskContextManager 避免循环依赖
        self.task_manager: Optional[Any] = None
        self._task_manager_loaded: bool = False

    # ── 公开属性 ──

    @property
    def messages(self) -> List[Message]:
        """所有消息（完整列表）"""
        return self._messages

    @messages.setter
    def messages(self, value: List[Message]):
        self._messages = value
        # 重建 round_map
        self._round_map.clear()
        for m in self._messages:
            self._round_map.setdefault(getattr(m, '_round_id', 0), []).append(m)

    # ── 添加消息 ──

    def add(self, message: Message) -> None:
        """添加任意类型消息"""
        message._round_id = self._round_counter
        self._messages.append(message)
        self._round_map.setdefault(self._round_counter, []).append(message)

    def add_user(self, content: str, importance: Optional[float] = None) -> None:
        """用户消息：开启新轮次"""
        self._round_counter += 1
        msg = Message.user(content)
        msg._round_id = self._round_counter
        self._messages.append(msg)
        self._round_map.setdefault(self._round_counter, []).append(msg)

    def add_assistant(
        self,
        content: str,
        tool_calls: Optional[List] = None,
        reasoning_content: Optional[str] = None,
        importance: Optional[float] = None,
    ) -> None:
        """助手消息（含 tool_calls）"""
        msg = Message.assistant(content, tool_calls, reasoning_content)
        msg._round_id = self._round_counter
        self._messages.append(msg)
        self._round_map.setdefault(self._round_counter, []).append(msg)

    def add_tool(
        self,
        call_id: str,
        content: Any,
        action: Optional[str] = None,
    ) -> None:
        """工具结果消息"""
        if not isinstance(content, str):
            try:
                content = json.dumps(content, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                content = str(content)
        msg = Message.tool(call_id, content)
        msg._round_id = self._round_counter
        self._messages.append(msg)
        self._round_map.setdefault(self._round_counter, []).append(msg)

    def add_system(self, content: str) -> None:
        """系统消息"""
        msg = Message.system(content)
        msg._round_id = self._round_counter
        self._messages.append(msg)
        self._round_map.setdefault(self._round_counter, []).append(msg)

    def add_many(self, messages: List[Message]) -> None:
        """批量添加"""
        for msg in messages:
            self.add(msg)

    # ── 查询 ──

    def get_all(self) -> List[Message]:
        """返回所有消息的深拷贝"""
        return deepcopy(self._messages)

    def get_last(self, n: int) -> List[Message]:
        """最近 N 条消息"""
        return deepcopy(self._messages[-n:])

    def get_recent_summary(self, n: int = 10) -> List[Dict]:
        """获取最近 N 条消息的摘要（用于前端展示，不含冗长的 tool 结果）"""
        summary = []
        for m in self._messages[-n:]:
            entry = {
                "role": m.role.value,
                "content": (m.content or "")[:200] if m.content else "",
            }
            if m.tool_calls:
                entry["tool_calls"] = [
                    {"name": _get_tc_name(tc)}
                    for tc in m.tool_calls[:3]
                ]
            summary.append(entry)
        return summary

    def get_system_messages(self) -> List[Message]:
        """所有 system 消息"""
        return [m for m in self._messages if m.role == Role.SYSTEM]

    def get_user_messages(self) -> List[Message]:
        """所有 user 消息"""
        return [m for m in self._messages if m.role == Role.USER]

    def get_last_user_message(self) -> Optional[Message]:
        """最后一条 user 消息"""
        for m in reversed(self._messages):
            if m.role == Role.USER:
                return m
        return None

    def get_messages_by_round(self, round_id: int) -> List[Message]:
        """获取指定轮次的所有消息"""
        return deepcopy(self._round_map.get(round_id, []))

    # ── 核心输出 ──

    def get_for_llm(self, include_system: bool = True) -> List[Dict]:
        """
        输出消息给 LLM API，按 round_id 整轮保留/丢弃。

        截断策略（按 round_id 整轮操作，绝不拆散同一轮）：
          1. 从最新轮次开始保留
          2. 从最旧轮次开始丢弃，直到预算满足
          3. 所有 system 消息始终保留
          4. 至少保留最后 min_working_reserve token 的最新消息
          5. 如果预算太小连最新一轮都放不下，发出 warning 但仍输出

        tool_calls ↔ tool 配对通过 round_id 保证完整。
        """
        if not self._messages:
            return []

        # 收集所有 system 消息（始终保留，不计入预算）
        system_msgs = []
        for m in self._messages:
            if include_system and m.role == Role.SYSTEM:
                system_msgs.append(m.to_dict())

        # 按 round_id 分组，排除 system-only 轮次
        non_system_rounds = {
            rid: msgs for rid, msgs in self._round_map.items()
            if any(m.role != Role.SYSTEM for m in msgs)
        }

        # 从最新到最旧排序
        sorted_rounds = sorted(non_system_rounds.keys(), reverse=True)

        # 从最新轮次开始收集，逐轮加入直到超预算
        selected_rounds = []
        total_tokens = sum(estimate_message_dict(m) for m in system_msgs)

        for rid in sorted_rounds:
            round_msgs = non_system_rounds[rid]
            round_tokens = sum(estimate_message_tokens(m) for m in round_msgs)

            if total_tokens + round_tokens <= self.max_tokens:
                selected_rounds.append(rid)
                total_tokens += round_tokens
            else:
                # 超过预算，跳过这一整轮
                logger.info(
                    f"get_for_llm: 丢弃第 {rid} 轮（{round_tokens} token），"
                    f"当前已选 {total_tokens}/{self.max_tokens} token"
                )

        # 按原始顺序输出（system + 选中的非 system 轮次）
        sorted_selected = sorted(selected_rounds)
        result = list(system_msgs)
        for rid in sorted_selected:
            for m in non_system_rounds[rid]:
                result.append(m.to_dict())

        # 只有多轮对话且 token 过低才警告（单条消息的短对话是正常的）
        if total_tokens < self.min_working_reserve and len(sorted_rounds) > 1:
            preview = " | ".join(
                f"[{m.get('role','?')}] {str(m.get('content',''))[:80]}"
                for m in result[:5]
            )
            logger.warning(
                f"get_for_llm: 输出仅 {total_tokens} token，"
                f"低于 min_working_reserve={self.min_working_reserve} | "
                f"{len(result)} 条消息 | 前 5 条: {preview}"
            )

        logger.debug(
            f"get_for_llm: 保留 {len(selected_rounds)}/{len(sorted_rounds)} 轮，"
            f"共 {total_tokens} token（预算 {self.max_tokens}）"
        )

        return result

    # ── 修改 ──

    def replace_last(self, message: Message) -> None:
        """替换最后一条消息"""
        if self._messages:
            old = self._messages[-1]
            old_round_id = getattr(old, '_round_id', 0)
            message._round_id = old_round_id
            self._messages[-1] = message
            # 更新 round_map
            round_msgs = self._round_map.get(old_round_id, [])
            for i in range(len(round_msgs) - 1, -1, -1):
                if round_msgs[i] is old:
                    round_msgs[i] = message
                    break

    def remove_last(self) -> Optional[Message]:
        """移除最后一条消息"""
        if self._messages:
            msg = self._messages.pop()
            round_id = getattr(msg, '_round_id', 0)
            round_msgs = self._round_map.get(round_id, [])
            if msg in round_msgs:
                round_msgs.remove(msg)
            return msg
        return None

    def clear(self) -> None:
        """清空所有消息"""
        self._messages.clear()
        self._round_map.clear()
        self._round_counter = 0
        self._task_boundaries.clear()
        self._task_switch_injected = False

    def truncate(self, keep_last: int) -> None:
        """
        保留最后 keep_last 轮对话（仅用于内存管理，非 LLM 输入）。
        以 round_id 为单位，不会拆散 tool_calls ↔ tool 配对。
        """
        if keep_last <= 0:
            self.clear()
            return

        # 按 round_id 分组找到所有轮次
        all_rounds = sorted(self._round_map.keys())

        if len(all_rounds) <= keep_last:
            return

        # 保留最后 keep_last 轮
        keep_rounds = set(all_rounds[-keep_last:])
        kept_messages = [m for m in self._messages if getattr(m, '_round_id', 0) in keep_rounds]
        removed_count = len(self._messages) - len(kept_messages)

        self._messages = kept_messages
        self._round_map = {rid: msgs for rid, msgs in self._round_map.items() if rid in keep_rounds}

        if removed_count > 0:
            logger.info(f"truncate: 移除 {removed_count} 条旧消息，保留最后 {keep_last} 轮")

    # ── 任务管理 ──

    def get_task_switch_prompt(self) -> str:
        """获取任务切换提示词"""
        if self.is_task_switch() and not self._task_switch_injected:
            self._task_switch_injected = True
            if self.task_manager:
                return self.task_manager.get_switch_prompt()
        return ""

    def get_task_context_prompt(self) -> str:
        """获取任务上下文提示"""
        if self.task_manager and self.task_manager.current_task:
            return self.task_manager.get_context_prompt()
        return ""

    def is_task_switch(self) -> bool:
        """当前轮次是否检测到任务切换"""
        if not self._task_boundaries:
            return False
        return self._task_boundaries[-1] == self._round_counter

    # ── 内部工具 ──

    def _ensure_task_manager(self):
        """延迟加载 TaskContextManager"""
        if not self._task_manager_loaded:
            try:
                from ..context.task_manager import TaskContextManager
                if self.task_manager is None:
                    self.task_manager = TaskContextManager()
                self._task_manager_loaded = True
            except ImportError:
                pass

    def __len__(self):
        return len(self._messages)

    def __iter__(self):
        return iter(self._messages)

    def __getitem__(self, idx):
        return self._messages[idx]


def _get_tc_name(tc) -> str:
    """从 ToolCall 对象或 dict 中提取工具名称"""
    if isinstance(tc, dict):
        return tc.get("function", {}).get("name", "?")
    try:
        return tc.function.name if tc.function else "?"
    except AttributeError:
        return str(tc)[:30]
