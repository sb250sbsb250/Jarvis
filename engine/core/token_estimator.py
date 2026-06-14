"""
Token 估算器 — 支持中文/英文/代码/JSON 的分类型估算

相比旧版 _estimate_message_tokens 的改进:
  - 按内容类型区分系数（代码/JSON 比英文贵）
  - 统计 tool_calls 格式开销
  - 区分 message 角色的 base overhead
"""

from __future__ import annotations
from typing import Dict, Optional, Any
from ..core.types import Message, Role


class TokenEstimator:
    """
    Token 估算器

    基于统计数据的系数:
      - 中文: 0.7 token/字 (UTF-8 3字节 → 0.7 round)
      - 英文/西文: 0.25 token/字符
      - 代码: 0.3 token/字符 (更紧凑但特殊符号多)
      - JSON: 0.35 token/字符 (大量括号/引号)
    """

    # 每字符 token 系数
    RATIOS = {
        "chinese": 0.65,     # 中文（1字≈0.65 token，GPT/DeepSeek实测）
        "english": 0.25,      # 英文/数字/符号（4字符≈1 token）
        "code": 0.3,          # 代码
        "json": 0.35,         # JSON 结构
    }

    # 消息角色基础开销
    OVERHEADS = {
        Role.USER: 8,
        Role.ASSISTANT: 8,
        Role.SYSTEM: 8,
        Role.TOOL: 12,
    }

    # tool_calls 每项额外开销
    TOOL_CALL_OVERHEAD = 15

    # 中文 Unicode 范围
    _CJK_RANGES = [
        (0x4E00, 0x9FFF),   # CJK 统一表意文字
        (0x3000, 0x303F),   # CJK 符号和标点
        (0xFF00, 0xFFEF),   # 全角 ASCII 和标点
        (0x2E80, 0x2EFF),   # CJK 部首补充
    ]

    def estimate(self, message: Message) -> int:
        """
        估算一条消息的 token 数

        Args:
            message: Message 对象

        Returns:
            估算的 token 数（整数）
        """
        tokens = self.OVERHEADS.get(message.role, 8)

        # 内容估算
        if message.content:
            tokens += self._estimate_text(message.content)

        # tool_calls 格式开销
        if message.tool_calls:
            tokens += self.TOOL_CALL_OVERHEAD * len(message.tool_calls)
            for tc in message.tool_calls:
                if hasattr(tc, 'function') and tc.function:
                    fn = tc.function
                    if hasattr(fn, 'arguments') and fn.arguments:
                        args = fn.arguments
                        if isinstance(args, dict):
                            tokens += self._estimate_text(str(args), "json")
                        elif isinstance(args, str):
                            tokens += self._estimate_text(args, "json")
                    if hasattr(fn, 'name') and fn.name:
                        tokens += len(fn.name) // 4

        if message.name:
            tokens += len(message.name) // 4

        return tokens

    def estimate_dict(self, msg_dict: Dict[str, Any]) -> int:
        """从 dict 格式消息估算（兼容旧格式）"""
        role_str = msg_dict.get("role", "user")
        tokens = 8
        content = msg_dict.get("content", "")
        if content:
            tokens += self._estimate_text(content)
        tool_calls = msg_dict.get("tool_calls")
        if tool_calls:
            tokens += self.TOOL_CALL_OVERHEAD * len(tool_calls)
            for tc in tool_calls:
                args = tc.get("function", {}).get("arguments", "{}")
                if isinstance(args, str):
                    tokens += self._estimate_text(args, "json")
        return tokens

    def _estimate_text(self, text: str, content_type: str = "auto") -> int:
        """
        估算一段文本的 token 数

        Args:
            text: 文本
            content_type: auto/chinese/english/code/json，auto 自动检测

        Returns:
            token 数
        """
        if not text:
            return 0

        if content_type == "auto":
            content_type = self._detect_type(text)

        cjk_chars = self._count_cjk(text)
        other_chars = len(text) - cjk_chars

        ratio = self.RATIOS.get(content_type, 0.3)

        # 中文部分和英文部分分开算
        if content_type == "chinese":
            return int(cjk_chars * self.RATIOS["chinese"] + other_chars * self.RATIOS["english"])
        elif content_type in ("code", "json"):
            # 代码/JSON：中文按 1.5，其他按指定系数
            return int(cjk_chars * self.RATIOS["chinese"] + other_chars * ratio)
        else:
            return int(cjk_chars * self.RATIOS["chinese"] + other_chars * self.RATIOS["english"])

    def _detect_type(self, text: str) -> str:
        """自动检测文本类型"""
        if not text:
            return "english"

        # 检测 JSON
        text_stripped = text.strip()
        if text_stripped.startswith(("{", "[")) and (text_stripped.endswith("}") or text_stripped.endswith("]")):
            try:
                # 快速判断：以 [{ 开头且结构紧凑
                if "{" in text_stripped[:10] or "[" in text_stripped[:10]:
                    return "json"
            except Exception:
                pass

        # 检测代码（含换行符 + 缩进 + 关键词）
        code_indicators = ["def ", "class ", "import ", "return ", "if __", "    ", "\t"]
        if any(indicator in text for indicator in code_indicators):
            # 但也要看中文比例——中文多就不算代码
            cjk_ratio = self._count_cjk(text) / max(len(text), 1)
            if cjk_ratio < 0.3:
                return "code"

        # 检测中文为主
        cjk_ratio = self._count_cjk(text) / max(len(text), 1)
        if cjk_ratio > 0.3:
            return "chinese"

        return "english"

    def _count_cjk(self, text: str) -> int:
        """统计 CJK 字符数"""
        count = 0
        for ch in text:
            cp = ord(ch)
            for lo, hi in self._CJK_RANGES:
                if lo <= cp <= hi:
                    count += 1
                    break
        return count


# 全局单例
_estimator = TokenEstimator()


def estimate_message_tokens(message: Message) -> int:
    """快捷接口"""
    return _estimator.estimate(message)


def estimate_message_dict(msg_dict: Dict[str, Any]) -> int:
    """快捷接口（dict 格式）"""
    return _estimator.estimate_dict(msg_dict)
