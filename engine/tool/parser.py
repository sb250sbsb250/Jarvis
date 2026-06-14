"""
engine/tool/parser.py — JSON 解析 + 结果格式化

从 agent_loop.py 提取的 JSON 解析工具和 tool result 构建函数。
"""

import ast
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def make_tool_result(call_id: str, content: str) -> Dict:
    """统一构建 tool result 消息，确保永远不会漏掉 tool_call_id"""
    return {
        "role": "tool",
        "tool_call_id": call_id or "",
        "content": content,
    }


def parse_tool_args(
    tool_name: str,
    tool_args_raw: str,
    tool_registry: Optional[Any] = None,
) -> Tuple[Dict, bool]:
    """多层 JSON 解析。
    返回 (parsed_dict, ok)，不涉及 messages 副作用（由调用方处理）。
    """
    stripped = tool_args_raw.strip()

    # 第 1 层：直接 json.loads
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed, True
    except json.JSONDecodeError:
        pass

    # 第 2 层：修复常见 JSON 错误后再试
    try:
        fixed = fix_common_json_errors(stripped)
        parsed = json.loads(fixed)
        if isinstance(parsed, dict):
            return parsed, True
    except (json.JSONDecodeError, Exception):
        pass

    # 第 3 层：ast.literal_eval（处理单引号等 Python 风格字典）
    if stripped.startswith("{") and stripped.endswith("}") and len(stripped) < 10000:
        try:
            parsed = ast.literal_eval(stripped)
            if isinstance(parsed, dict):
                return parsed, True
        except (ValueError, SyntaxError, MemoryError):
            pass

    # 第 4 层：正则修复 command 字段中未转义的双引号
    try:
        fixed = re.sub(
            r'"command"\s*:\s*"(.+?)"(?=\s*[,}]|\s*\n\s*"(?!command))',
            lambda m: f'"command": {json.dumps(m.group(1))}',
            stripped,
            flags=re.DOTALL,
        )
        parsed = json.loads(fixed)
        if isinstance(parsed, dict):
            return parsed, True
    except (json.JSONDecodeError, Exception):
        pass

    # 全部失败
    return {}, False


def get_tool_param_hint(tool_registry: Any, tool_name: str) -> str:
    """获取工具的参数格式提示。"""
    if tool_registry is None:
        return ""
    tool_def = (
        tool_registry.get_tool_def(tool_name)
        if hasattr(tool_registry, "get_tool_def") else None
    )
    if tool_def and hasattr(tool_def, "parameters"):
        param_lines = []
        for p in tool_def.parameters:
            req = "(必填)" if getattr(p, "required", False) else "(可选)"
            desc = getattr(p, "description", "")[:80]
            param_lines.append(f"  - {p.name} {req}: {desc}")
        if param_lines:
            return "\n期望参数格式:\n" + "\n".join(param_lines)
    return ""


def fix_common_json_errors(raw: str) -> str:
    """修复常见 JSON 格式错误。"""
    # 移除尾部逗号: ,} → } 和 ,] → ]
    raw = re.sub(r",\s*}", "}", raw)
    raw = re.sub(r",\s*]", "]", raw)

    # 单引号 → 双引号（仅在看起来像 Python 字典时）
    if "'" in raw and '"' not in raw:
        raw = raw.replace("'", '"')

    # 修复 True/False/None → true/false/null（Python → JSON）
    raw = re.sub(r"\bTrue\b", "true", raw)
    raw = re.sub(r"\bFalse\b", "false", raw)
    raw = re.sub(r"\bNone\b", "null", raw)

    return raw
