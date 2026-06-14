"""
prompt/complexity.py — 复杂度自适应 Prompt 路由器

同一个模型，通过输出格式约束控制响应速度：
  - DIRECT:   直接回答，一句话（max_tokens=30, temp=0.1）
  - CONCISE:  简洁回答，1-3句话（max_tokens=150, temp=0.3）
  - STANDARD: 标准模式，正常推理（max_tokens=500, temp=0.7）
  - DETAILED: 详细模式，完整思考（max_tokens=2000, temp=0.8）
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Dict, Optional, Tuple


class ResponseMode(Enum):
    """响应模式"""
    DIRECT = "direct"
    CONCISE = "concise"
    STANDARD = "standard"
    DETAILED = "detailed"


class ComplexityRouter:
    """
    复杂度路由器 — 根据用户输入快速判断响应模式。
    所有模式使用同一个模型，只改变 Prompt 结构和参数。
    """

    # ── 直接回答模式触发条件 ──
    DIRECT_PATTERNS = {
        "greeting": [r"^(你好|您好|嗨|hello|hi|hey|早上好|下午好|晚上好)$", re.IGNORECASE],
        "thanks": [r"^(谢谢|感谢|thanks|thank you|thx)$", re.IGNORECASE],
        "affirm": [r"^(好的|ok|嗯|哦|知道了|明白了|好的吧|可以)$", re.IGNORECASE],
        "simple_calc": [r"^\d+\s*[+\-*/]\s*\d+\s*$", re.IGNORECASE],
    }

    # ── 简洁模式触发条件 ──
    CONCISE_PATTERNS = {
        "factual_qa": [r"^(什么是|谁|什么时候|在哪|哪里|how|what|when|where)", re.IGNORECASE],
        "definition": [r"^(定义|解释|说明|describe|explain|define)", re.IGNORECASE],
        "simple_why": [r"^(为什么|为何|why)", re.IGNORECASE],
        "yes_no": [r"^(能|可以|是否|有没有|对吗|对不对)", re.IGNORECASE],
    }

    # ── 复杂关键词 ──
    COMPLEX_KEYWORDS = {
        "分析", "审查", "生成", "创建", "修改", "删除",
        "比较", "总结", "规划", "设计", "实现", "重构",
        "调试", "测试", "优化", "对比", "评估",
        "翻译",
    }

    @classmethod
    def classify(cls, user_input: str, history_len: int = 0) -> Tuple[ResponseMode, Dict]:
        """
        分类问题复杂度。
        Returns: (mode, info_dict)
        """
        text = user_input.strip()

        # 1. 纯问候（仅当输入只有问候语时）→ DIRECT
        greeting_pattern = cls.DIRECT_PATTERNS.get("greeting")
        if greeting_pattern and re.match(greeting_pattern[0], text, greeting_pattern[1]):
            # 问候语后还有内容 → 继续往下判断
            for kw in cls.COMPLEX_KEYWORDS:
                if kw in text:
                    return ResponseMode.DETAILED, {"reason": f"complex_keyword:{kw}", "matched": True}
            return ResponseMode.DIRECT, {"reason": "greeting", "matched": True}

        # 2. 其他直接模式（感谢/确认/简单计算）
        for category, (pattern, flags) in cls.DIRECT_PATTERNS.items():
            if category == "greeting":
                continue
            if re.match(pattern, text, flags):
                return ResponseMode.DIRECT, {"reason": category, "matched": True}

        # 3. 复杂关键词 → 详细（优先于简洁）
        for kw in cls.COMPLEX_KEYWORDS:
            if kw in text:
                return ResponseMode.DETAILED, {"reason": f"complex_keyword:{kw}", "matched": True}

        # 4. 简洁模式（事实问答/定义/简单为什么）
        for category, (pattern, flags) in cls.CONCISE_PATTERNS.items():
            if re.match(pattern, text, flags):
                return ResponseMode.CONCISE, {"reason": category, "matched": True}

        # 5. 极短输入（≤10字）→ 简洁
        if len(text) <= 10:
            return ResponseMode.CONCISE, {"reason": "very_short", "length": len(text)}

        # 6. 长输入 → 详细
        if len(text) > 200:
            return ResponseMode.DETAILED, {"reason": "long_input", "length": len(text)}

        # 7. 多轮对话 → 标准
        if history_len > 10:
            return ResponseMode.STANDARD, {"reason": "long_history", "history_len": history_len}

        # 8. 默认：标准模式
        return ResponseMode.STANDARD, {"reason": "default"}

    @classmethod
    def get_system_prompt(cls, mode: ResponseMode, skill_name: str = "") -> str:
        """根据模式获取对应的系统提示词"""
        base = f"你是 {skill_name or 'Jarvis'}，一个智能助手。"

        if mode == ResponseMode.DIRECT:
            return f"""{base}

## 输出规则（极其重要）
- 直接回答，一句话
- 不要解释
- 不要分步骤
- 不要加前缀（如"好的"、"明白了"）
- 不要说"作为AI助手"

示例：
用户: 你好 → 输出: 你好！有什么可以帮你的？
用户: 1+1等于几 → 输出: 2
用户: 谢谢 → 输出: 不客气

**立即输出答案，不要思考过程**"""

        if mode == ResponseMode.CONCISE:
            return f"""{base}

## 输出规则
- 1-3句话回答
- 只输出核心答案
- 不要分点列举
- 不要有多余解释

示例：
用户: 什么是DAG
输出: DAG是有向无环图，常用于表示依赖关系和任务调度。"""

        if mode == ResponseMode.STANDARD:
            return f"""{base}

## 输出规则
- 可以分2-3个要点
- 每个要点一行
- 不要过度解释
- 第1句直接回答问题"""

        # DETAILED
        return f"""{base}

## 输出规则
- 允许详细分析
- 可以分步骤
- 可以列出多个要点
- 确保完整性
- 先理解问题，再给出方案"""

    @classmethod
    def get_max_tokens(mode: str, input_length: int = 0) -> int:
        base = {
            "simple": 100000,
            "standard": 100000,
            "complex": 409600,
        }.get(mode, 100000)

        # 如果输入上下文较大，自动增加输出空间


        return base

    @classmethod
    def get_temperature(cls, mode: ResponseMode) -> float:
        mapping = {
            ResponseMode.DIRECT: 0.1,
            ResponseMode.CONCISE: 0.3,
            ResponseMode.STANDARD: 0.7,
            ResponseMode.DETAILED: 0.8,
        }
        return mapping[mode]
