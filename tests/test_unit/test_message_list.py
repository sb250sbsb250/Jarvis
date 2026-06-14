"""
test_unit/test_message_list.py — MessageList 单元测试

重点覆盖：
  1. 基本增删改查
  2. round_id 分组正确性（tool_calls ↔ tool 配对）
  3. get_for_llm 的 round_id 整轮截断逻辑
  4. truncate 内存管理
  5. 边界条件
"""

import json
import pytest
from engine.message.message_list import MessageList
from engine.core.types import Message, Role


class TestMessageListBasic:
    """基本增删改查"""

    def test_empty(self):
        ml = MessageList()
        assert len(ml) == 0
        assert ml.get_all() == []
        assert ml.get_for_llm() == []

    def test_add_user(self):
        ml = MessageList()
        ml.add_user("你好")
        assert len(ml) == 1
        assert ml[0].role == Role.USER
        assert ml[0].content == "你好"

    def test_add_assistant(self):
        ml = MessageList()
        ml.add_user("你好")
        ml.add_assistant("我很好")
        assert len(ml) == 2
        assert ml[1].role == Role.ASSISTANT

    def test_add_assistant_with_tool_calls(self):
        ml = MessageList()
        ml.add_user("查天气")
        ml.add_assistant(
            "正在查询",
            tool_calls=[{"id": "call_1", "function": {"name": "get_weather", "arguments": "{}"}}],
        )
        assert len(ml) == 2
        assert ml[1].tool_calls is not None
        tc = ml[1].tool_calls[0]
        assert tc.id == "call_1"
        assert tc.name == "get_weather"

    def test_add_tool(self):
        ml = MessageList()
        ml.add_user("查天气")
        ml.add_assistant("", tool_calls=[{"id": "call_1", "function": {"name": "get_weather", "arguments": "{}"}}])
        ml.add_tool("call_1", "晴天")
        assert len(ml) == 3
        assert ml[2].role == Role.TOOL
        assert ml[2].tool_call_id == "call_1"

    def test_add_system(self):
        ml = MessageList()
        ml.add_system("你是助手")
        assert ml[0].role == Role.SYSTEM

    def test_clear(self):
        ml = MessageList()
        ml.add_user("你好")
        ml.add_assistant("嗨")
        ml.clear()
        assert len(ml) == 0
        assert ml.get_for_llm() == []

    def test_add_many(self):
        ml = MessageList()
        msgs = [Message.user("a"), Message.assistant("b")]
        ml.add_many(msgs)
        assert len(ml) == 2


class TestMessageListRoundId:
    """round_id 分组验证"""

    def test_round_id_assignment(self):
        """每个 add_user 开启新 round"""
        ml = MessageList()
        ml.add_user("第一轮")
        ml.add_assistant("回复1")
        ml.add_user("第二轮")
        ml.add_assistant("回复2")

        round1_msgs = ml.get_messages_by_round(1)
        round2_msgs = ml.get_messages_by_round(2)

        assert len(round1_msgs) == 2
        assert round1_msgs[0].content == "第一轮"
        assert round1_msgs[1].content == "回复1"

        assert len(round2_msgs) == 2
        assert round2_msgs[0].content == "第二轮"
        assert round2_msgs[1].content == "回复2"

    def test_tool_call_pair_not_split(self):
        """tool_calls 和 tool 在同一 round，不会被拆散"""
        ml = MessageList()
        ml.add_user("查天气")
        ml.add_assistant(
            "",
            tool_calls=[{"id": "call_1", "function": {"name": "get_weather", "arguments": "{}"}}],
        )
        ml.add_tool("call_1", "晴天 25度")
        ml.add_user("再查北京")
        ml.add_assistant(
            "",
            tool_calls=[{"id": "call_2", "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}],
        )
        ml.add_tool("call_2", "北京 20度")

        round1 = ml.get_messages_by_round(1)
        round2 = ml.get_messages_by_round(2)

        assert len(round1) == 3  # user + assistant + tool
        assert len(round2) == 3  # user + assistant + tool

        # 检查 round1 中 assistant 有 tool_calls
        assert round1[1].tool_calls is not None
        assert round1[1].tool_calls[0].id == "call_1"
        # tool 消息在同一个 round
        assert round1[2].role == Role.TOOL
        assert round1[2].tool_call_id == "call_1"

    def test_round_map_accuracy(self):
        """_round_map 内部结构正确"""
        ml = MessageList()
        ml.add_user("第1轮")
        ml.add_assistant("r1")
        ml.add_tool("ct1", "res1")
        ml.add_user("第2轮")

        assert 1 in ml._round_map
        assert 2 in ml._round_map
        assert len(ml._round_map[1]) == 3
        assert len(ml._round_map[2]) == 1
        assert ml._round_map[2][0].content == "第2轮"


class TestMessageListGetForLlm:
    """get_for_llm round_id 截断逻辑"""

    def test_all_messages_within_budget(self):
        """预算足够时，所有消息都输出"""
        ml = MessageList(max_tokens=10000, min_working_reserve=0)
        ml.add_user("测试")
        ml.add_assistant("回复")

        result = ml.get_for_llm()
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"

    def test_trim_old_rounds(self):
        """超出预算时，丢弃最旧的整轮，保留最新"""
        ml = MessageList(max_tokens=100, min_working_reserve=0)
        # 5 轮对话，每条约 20 token，共约 200 token > 100
        for i in range(5):
            ml.add_user(f"用户输入第{i+1}轮")
            ml.add_assistant(f"助手回复第{i+1}轮")
            ml.add_tool(f"tool_{i}", f"工具结果第{i+1}轮")

        result = ml.get_for_llm(include_system=False)
        # 应该在预算内，且保留最新的几轮
        assert len(result) > 0

        # 验证最新轮次的 user 消息存在
        last_content = result[-1]["content"]
        assert "第5轮" in last_content

    def test_tool_pair_not_broken(self):
        """截断后 tool_calls ↔ tool 配对仍然完整"""
        ml = MessageList(max_tokens=120, min_working_reserve=0)

        # 3 轮对话，每轮约 60 token
        for i in range(3):
            ml.add_user(f"Q{i+1}")
            ml.add_assistant(
                "",
                tool_calls=[{"id": f"call_{i}", "function": {"name": "test", "arguments": "{}"}}],
            )
            ml.add_tool(f"call_{i}", f"结果{i+1}")

        result = ml.get_for_llm(include_system=False)

        # 辅助：检查所有 tool 消息都有对应的 assistant
        tool_ids_in_assistant = set()
        tool_ids_in_tool = set()

        for msg in result:
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if isinstance(tc, dict):
                        tool_ids_in_assistant.add(tc.get("id", ""))
                    else:
                        tool_ids_in_assistant.add(getattr(tc, "id", ""))
            elif msg["role"] == "tool":
                tool_ids_in_tool.add(msg.get("tool_call_id", ""))

        # 每个 tool 消息必须在前面的 assistant 中有对应的 tool_call
        for tid in tool_ids_in_tool:
            assert tid in tool_ids_in_assistant, f"孤立 tool 消息: {tid}"

    def test_system_always_kept(self):
        """system 消息始终保留"""
        ml = MessageList(max_tokens=50, min_working_reserve=0)
        ml.add_system("你是得力助手")
        ml.add_user("你好")

        result = ml.get_for_llm(include_system=True)
        system_msgs = [m for m in result if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0]["content"] == "你是得力助手"

    def test_system_excluded(self):
        """include_system=False 排除 system 消息"""
        ml = MessageList(max_tokens=10000, min_working_reserve=0)
        ml.add_system("你是得力助手")
        ml.add_user("你好")

        result = ml.get_for_llm(include_system=False)
        assert all(m["role"] != "system" for m in result)

    def test_empty_list(self):
        ml = MessageList()
        assert ml.get_for_llm() == []

    def test_single_round_under_budget(self):
        """一轮对话预算足够时全部输出"""
        ml = MessageList(max_tokens=1000, min_working_reserve=0)
        ml.add_user("测试")
        ml.add_assistant("回复" * 50)  # 100 字

        result = ml.get_for_llm()
        assert len(result) == 2

    def test_min_working_reserve_warning(self):
        """低于 min_working_reserve 时触发 warning 但不报错"""
        ml = MessageList(max_tokens=30, min_working_reserve=100)
        ml.add_user("hi")
        ml.add_assistant("hello")

        # 只触发 warning，不抛异常
        result = ml.get_for_llm()
        assert len(result) >= 1  # 至少有一条


class TestMessageListTruncate:
    """truncate 内存管理"""

    def test_truncate_basic(self):
        ml = MessageList()
        for i in range(4):
            ml.add_user(f"Q{i+1}")
            ml.add_assistant(f"A{i+1}")

        ml.truncate(keep_last=2)
        assert len(ml) == 4  # truncate 不影响消息数量? 不对，truncate 应该移除旧消息
        # 等等，truncate 只影响 _messages，不影响原始文件
        # 实际上 truncate 会移除旧消息的！

    def test_truncate_keeps_last_n_rounds(self):
        ml = MessageList()
        for i in range(5):
            ml.add_user(f"Q{i+1}")
            ml.add_assistant(f"A{i+1}")

        ml.truncate(keep_last=2)
        # 应保留最后 2 轮 = Q4 A4 Q5 A5（4条消息）
        assert len(ml) == 4
        assert "Q4" in ml[0].content
        assert "Q5" in ml[2].content

    def test_truncate_zero(self):
        ml = MessageList()
        ml.add_user("你好")
        ml.truncate(keep_last=0)
        assert len(ml) == 0

    def test_truncate_more_than_existing(self):
        ml = MessageList()
        ml.add_user("Q1")
        ml.add_assistant("A1")
        ml.truncate(keep_last=10)  # 10 > 1 round
        assert len(ml) == 2


class TestMessageListEdgeCases:
    """边界条件"""

    def test_replace_last(self):
        ml = MessageList()
        ml.add_user("旧消息")
        ml.replace_last(Message.user("新消息"))
        assert ml[-1].content == "新消息"
        assert len(ml) == 1

    def test_remove_last(self):
        ml = MessageList()
        ml.add_user("Q1")
        ml.add_assistant("A1")
        removed = ml.remove_last()
        assert removed is not None
        assert removed.content == "A1"
        assert len(ml) == 1

    def test_get_last(self):
        ml = MessageList()
        for i in range(3):
            ml.add_user(f"Q{i+1}")
            ml.add_assistant(f"A{i+1}")
        last_2 = ml.get_last(2)
        assert len(last_2) == 2
        assert last_2[0].content == "Q3"

    def test_get_last_user_message(self):
        ml = MessageList()
        ml.add_user("U1")
        ml.add_assistant("A1")
        ml.add_user("U2")
        last_user = ml.get_last_user_message()
        assert last_user is not None
        assert last_user.content == "U2"

    def test_get_recent_summary(self):
        ml = MessageList()
        ml.add_user("测试")
        ml.add_assistant("回复")
        summary = ml.get_recent_summary(n=10)
        assert len(summary) == 2
        assert summary[0]["role"] == "user"

    def test_get_system_messages(self):
        ml = MessageList()
        ml.add_system("sys1")
        ml.add_user("你好")
        ml.add_system("sys2")
        systems = ml.get_system_messages()
        assert len(systems) == 2

    def test_iteration(self):
        ml = MessageList()
        ml.add_user("A")
        ml.add_assistant("B")
        msgs = list(ml)
        assert len(msgs) == 2
        assert msgs[0].content == "A"

    def test_messages_setter(self):
        ml = MessageList()
        ml.add_user("旧")
        new_msgs = [Message.user("新1"), Message.assistant("新2")]
        ml.messages = new_msgs
        assert len(ml) == 2
        assert ml[0].content == "新1"
