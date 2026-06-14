"""
test_unit/test_complexity.py — 复杂度路由器的单元测试
"""

import pytest
from engine.prompt.complexity import ComplexityRouter, ResponseMode


class TestComplexityRouter:
    """测试复杂度路由器的 4 种模式分类"""

    @pytest.mark.parametrize(
        "text",
        [
            "你好",
            "您好",
            "嗨",
            "hello",
            "hi",
            "早上好",
            "晚上好",
        ],
    )
    def test_greeting_direct(self, text):
        """问候语 → DIRECT"""
        mode, info = ComplexityRouter.classify(text)
        assert mode == ResponseMode.DIRECT, f"'{text}' 应为 DIRECT，实际 {mode.value}"

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("谢谢", ResponseMode.DIRECT),
            ("好的", ResponseMode.DIRECT),
            ("25*4", ResponseMode.DIRECT),
            ("1+1", ResponseMode.DIRECT),
        ],
    )
    def test_factual_direct(self, text, expected):
        """感谢/确认/简单计算 → DIRECT"""
        mode, info = ComplexityRouter.classify(text)
        assert mode == expected, f"'{text}' 应为 {expected.value}，实际 {mode.value}"

    @pytest.mark.parametrize(
        "text",
        [
            "什么是DAG",
            "Python是什么",
            "how to write a function",
            "今天几号",
        ],
    )
    def test_simple_qa_concise(self, text):
        """简单事实问答 → CONCISE"""
        mode, info = ComplexityRouter.classify(text)
        assert mode == ResponseMode.CONCISE, f"'{text}' 应为 CONCISE，实际 {mode.value}"

    @pytest.mark.parametrize(
        "text",
        [
            "解释Python装饰器",
            "定义什么是递归",
        ],
    )
    def test_definition_concise(self, text):
        """定义/解释类问题 → CONCISE"""
        mode, info = ComplexityRouter.classify(text)
        assert mode == ResponseMode.CONCISE, f"'{text}' 应为 CONCISE，实际 {mode.value}"

    # 极短输入（≤10字）--> CONCISE
    @pytest.mark.parametrize(
        "text",
        [
            "好",
            "对",
            "明白",
        ],
    )
    def test_very_short_concise(self, text):
        """极短输入（≤10字，且不匹配直接模式）→ CONCISE"""
        mode, info = ComplexityRouter.classify(text)
        assert mode == ResponseMode.CONCISE, f"'{text}' 应为 CONCISE，实际 {mode.value} (reason={info.get('reason')})"

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("帮我把这个文件格式化一下", ResponseMode.STANDARD),   # 14字，无复杂关键词
            ("帮我写一个Python脚本", ResponseMode.STANDARD),       # 14字
            ("帮我看一下这个代码的写法", ResponseMode.STANDARD),   # 13字
        ],
    )
    def test_normal_request_standard(self, text, expected):
        """普通请求（无复杂关键词，>10字）→ STANDARD"""
        mode, info = ComplexityRouter.classify(text)
        assert mode == expected, f"'{text}' 应为 {expected.value}，实际 {mode.value} (reason={info.get('reason')})"

    @pytest.mark.parametrize(
        "text",
        [
            "审查这段代码的安全问题",
            "分析一下这个系统的性能瓶颈",
            "生成一个 FastAPI 的路由",
            "重构这个模块的结构",
            "优化这个SQL查询",
            "调试这个bug",
            "这段代码有性能问题，帮我审查一下",
        ],
    )
    def test_complex_task_detailed(self, text):
        """复杂任务关键词 → DETAILED"""
        mode, info = ComplexityRouter.classify(text)
        assert mode == ResponseMode.DETAILED, f"'{text}' 应为 DETAILED，实际 {mode.value}"
        assert "complex_keyword" in info.get("reason", ""), f"应标记复杂关键词: {info}"

    @pytest.mark.parametrize(
        "text",
        [
            "翻译成英文",
            "把这个文档翻译一下",
            "把中文翻译成日语",
        ],
    )
    def test_translate_detailed(self, text):
        """翻译任务 → DETAILED"""
        mode, info = ComplexityRouter.classify(text)
        assert mode == ResponseMode.DETAILED, f"'{text}' 应为 DETAILED，实际 {mode.value}"

    def test_long_input_detailed(self):
        """超长输入（>200字）→ DETAILED"""
        long_text = "请帮我分析项目架构 请帮我分析项目架构 " * 10
        mode, info = ComplexityRouter.classify(long_text)
        assert mode == ResponseMode.DETAILED

    def test_classify_preserves_info(self):
        """classify 返回的 info 包含原因"""
        mode, info = ComplexityRouter.classify("你好")
        assert "reason" in info
        assert info.get("matched", False) or info.get("reason") is not None

        mode, info = ComplexityRouter.classify("审查代码")
        assert "complex_keyword" in info.get("reason", "")

    def test_get_system_prompt(self):
        """各模式的 system_prompt 合理"""
        for mode in ResponseMode:
            prompt = ComplexityRouter.get_system_prompt(mode)
            assert len(prompt) > 0

        # 所有模式都有内容
        for mode in ResponseMode:
            prompt = ComplexityRouter.get_system_prompt(mode)
            assert len(prompt) > 0, f"{mode} 的 prompt 不能为空"
            assert "输出规则" in prompt, f"{mode} 应包含输出规则"

    def test_get_max_tokens(self):
        """token 限制合理"""
        assert ComplexityRouter.get_max_tokens(ResponseMode.DIRECT) == 30
        assert ComplexityRouter.get_max_tokens(ResponseMode.CONCISE) == 150
        assert ComplexityRouter.get_max_tokens(ResponseMode.STANDARD) == 500
        assert ComplexityRouter.get_max_tokens(ResponseMode.DETAILED) == 2000

    def test_get_temperature(self):
        """温度设置合理"""
        assert ComplexityRouter.get_temperature(ResponseMode.DIRECT) == 0.1
        assert ComplexityRouter.get_temperature(ResponseMode.CONCISE) == 0.3
        assert ComplexityRouter.get_temperature(ResponseMode.STANDARD) == 0.7
        assert ComplexityRouter.get_temperature(ResponseMode.DETAILED) == 0.8

    def test_priority_correct(self):
        """
        优先级测试：
        - 复杂关键词（"分析"）> 简洁模式（"什么"）
        - 问候 > 极短输入
        """
        # "分析什么" 包含 "什么"（CONCISE）和 "分析"（DETAILED）
        # 复杂关键词应优先
        mode, info = ComplexityRouter.classify("分析什么")
        assert mode == ResponseMode.DETAILED, f"复杂关键词应优先，实际 {mode.value}"

        # 问候后带复杂关键词 → DETAILED
        mode, info = ComplexityRouter.classify("你好，分析一下")
        assert mode == ResponseMode.DETAILED, f"有复杂关键词应 DETAILED，实际 {mode.value} ({info.get('reason')})"
