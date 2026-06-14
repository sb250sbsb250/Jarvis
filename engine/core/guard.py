"""
engine/core/guard.py — 守卫系统

从 agent_loop.py 提取的守卫逻辑：重复检测、硬中断、空回复干预、错误分级。
使用 GuardState 封装所有守卫状态。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── LLM 行为修复开关 ──
ENABLE_RESULT_CACHE = True        # Fix 1: 只读工具结果缓存
ENABLE_HARD_INTERRUPT = True      # Fix 1: 完全相同调用硬中断
ENABLE_EMPTY_REPLY_FIX = True     # Fix 2: 空回复立即干预
ENABLE_JSON_CORRECTION = True     # Fix 3: JSON 解析失败→修正请求
ENABLE_KEY_FINDINGS = True        # Fix 4: 关键发现压缩保护
ENABLE_ERROR_GRADING = True       # Fix 5: 错误分级引导

# 轻量守卫阈值
STUCK_SAME_TARGET = 3
STUCK_CONSECUTIVE_FAILS = 3


@dataclass
class GuardState:
    """守卫状态容器"""
    last_tool_target: str = ""
    same_target_count: int = 0
    result_cache: Dict[str, str] = field(default_factory=dict)
    exact_call_signatures: List[str] = field(default_factory=list)
    key_findings: Dict[str, str] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
#  辅助
# ═══════════════════════════════════════════════════════════════

def record_get(record, key: str, default=None):
    """兼容 ToolCallRecord 和 dict 的字段访问。"""
    if hasattr(record, key):
        return getattr(record, key, default)
    if isinstance(record, dict):
        return record.get(key, default)
    return default


def safe_inject_system(messages: List[Dict], content: str) -> None:
    """安全地在 messages 末尾追加 system 消息（避开 tool_calls 块）。"""
    if (messages and messages[-1].get("role") == "assistant"
            and messages[-1].get("tool_calls")):
        insert_at = len(messages) - 1
        messages.insert(insert_at, {"role": "system", "content": content})
    else:
        messages.append({"role": "system", "content": content})


# ═══════════════════════════════════════════════════════════════
#  守卫函数
# ═══════════════════════════════════════════════════════════════

def guard_repeated_target(state: GuardState, tool_name: str, tool_args: Dict) -> None:
    """追踪同一目标的连续操作次数。"""
    target = tool_args.get("path") or tool_args.get("query") or tool_args.get("file_path", "")
    if not target:
        state.last_tool_target = ""
        state.same_target_count = 0
        return

    if target == state.last_tool_target:
        state.same_target_count += 1
    else:
        state.same_target_count = 1
        state.last_tool_target = target


def check_stuck(state: GuardState, tool_calls_log: List) -> Optional[str]:
    """纯规则检测是否需要提醒 LLM。"""
    if len(tool_calls_log) < STUCK_CONSECUTIVE_FAILS:
        return None

    if state.same_target_count >= STUCK_SAME_TARGET:
        return (
            f"⚠️ 你已经连续 {state.same_target_count} 次操作 `{state.last_tool_target}`。"
            "如果目标没有变化，请基于已有信息直接输出结论，不要再重复。"
        )

    recent = tool_calls_log[-STUCK_CONSECUTIVE_FAILS:]
    if all(record_get(t, "error") is not None for t in recent):
        errors = [(record_get(t, "error", "") or "")[:80] for t in recent]
        return (
            f"连续 {STUCK_CONSECUTIVE_FAILS} 次操作失败。\n"
            f"最近错误: {', '.join(errors)}\n"
            "请分析失败原因，换一种完全不同的方式。"
        )

    return None


def check_stuck_hard(state: GuardState, tool_name: str, tool_args: Dict) -> Optional[str]:
    """Fix 1: 检测完全相同的调用（工具名+参数完全一致），3 次则硬中断。"""
    try:
        sig = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
    except (TypeError, ValueError):
        sig = f"{tool_name}:{str(tool_args)}"

    state.exact_call_signatures.append(sig)

    if (len(state.exact_call_signatures) >= 3
            and len(set(state.exact_call_signatures[-3:])) == 1):
        return (
            f"[系统拦截] 已连续 3 次调用 {tool_name} 且参数完全相同。\n"
            f"该操作的结果不会改变，请基于已有信息直接输出结论，"
            f"或换一种完全不同的方式完成任务。"
        )
    return None


def suggest_next_action(messages: List[Dict], tool_calls_log: List) -> str:
    """Fix 2: 根据上下文生成空回复干预建议。"""
    user_input = ""
    for m in messages:
        if m.get("role") == "user":
            content = str(m.get("content", ""))
            if content and not content.startswith("[系统提示]"):
                user_input = content[:200]
                break

    recent_tools = []
    for record in tool_calls_log[-5:]:
        name = record_get(record, "tool", "?")
        err = record_get(record, "error")
        status = "失败" if err else "成功"
        recent_tools.append(f"  - {name}: {status}")

    read_files = set()
    for record in tool_calls_log:
        name = record_get(record, "tool", "")
        if name in ("file_read", "code_read", "web_fetch", "read_file"):
            args = record_get(record, "args", {})
            if isinstance(args, dict):
                path = args.get("path", args.get("url", ""))
                if path:
                    read_files.add(path)

    parts = [
        "[系统提醒] 你刚才输出了空回复（无任何内容和工具调用），这会导致对话卡死。",
    ]
    if user_input:
        parts.append(f"\n原始需求: {user_input}")
    if read_files:
        parts.append(f"\n已读取文件: {', '.join(list(read_files)[:10])}")
    if recent_tools:
        parts.append(f"\n最近操作:\n" + "\n".join(recent_tools))
    parts.append(
        "\n请选择下一步:\n"
        "1) 信息已足够 → 直接输出最终结果\n"
        "2) 需要更多信息 → 调用具体工具\n"
        "3) 需要修改代码 → 调用 code_write 等编辑工具"
    )
    return "\n".join(parts)


def handle_tool_error(
    messages: List[Dict], tool_name: str,
    error_str: str, tool_calls_log: List,
    tool_registry: Optional[Any] = None,
) -> None:
    """Fix 5: 根据错误类型和连续失败次数，分级注入引导。"""
    consecutive_fails = 0
    for record in reversed(tool_calls_log):
        if record_get(record, "error"):
            consecutive_fails += 1
        else:
            break

    error_lower = error_str.lower()

    # Tier 1: 参数错误
    is_param_error = any(
        kw in error_lower
        for kw in ("缺少", "参数", "invalid", "required", "missing", "argument")
    )
    if is_param_error:
        if consecutive_fails >= 3:
            from ..tool.parser import get_tool_param_hint
            param_hint = get_tool_param_hint(tool_registry, tool_name)
            if param_hint:
                hint = (
                    f"[系统提示] `{tool_name}` 连续 {consecutive_fails} 次参数错误。\n"
                    f"正确参数:{param_hint}"
                )
                messages.append({"role": "user", "content": hint})
                logger.info(f"📋 Tier 1: 注入 {tool_name} 参数格式")
        return

    # Tier 2: 文件不存在 / 权限问题
    is_file_error = any(
        kw in error_lower
        for kw in ("not found", "permission", "不存在", "没有权限", "no such file", "access denied")
    )
    if is_file_error:
        hint = (
            f"[系统提示] `{tool_name}` 遇到文件/权限错误: {error_str[:150]}\n"
            f"建议: 先用 file_list 或 shell('dir/ls') 确认文件路径是否正确。"
        )
        messages.append({"role": "user", "content": hint})
        logger.info(f"📋 Tier 2: 文件/权限错误引导")
        return

    # Tier 3: 连续 ≥3 次失败（通用）
    if consecutive_fails >= 3:
        alternatives = suggest_alternatives(tool_name, error_str)
        alt_text = "\n".join(f"  - {a}" for a in alternatives) if alternatives else "  - 检查参数后重试"
        hint = (
            f"[系统提示] `{tool_name}` 连续失败 {consecutive_fails} 次。\n"
            f"错误: {error_str[:150]}\n"
            f"建议替代方案:\n{alt_text}\n"
            f"如果此步骤无法完成，可以跳过它继续后续任务。"
        )
        messages.append({"role": "user", "content": hint})
        logger.info(f"📋 Tier 3: 连续失败 {consecutive_fails} 次，注入替代方案")


def suggest_alternatives(tool_name: str, error_str: str) -> List[str]:
    """Fix 5: 根据失败工具返回替代方案。"""
    if tool_name in ("shell_run", "shell", "run_command", "execute"):
        return [
            "先写入脚本文件再用 shell_run 执行脚本文件",
            "检查命令语法是否正确（引号、转义等）",
            "尝试用更简单的命令分步执行",
        ]
    elif tool_name in ("file_read", "code_read", "read_file"):
        return [
            "用 file_list 或 shell('dir') 确认文件路径",
            "检查文件是否存在或路径拼写是否正确",
        ]
    elif tool_name in ("web_fetch", "fetch_url", "http_get"):
        return [
            "改用 web_search 搜索相关内容",
            "检查 URL 是否可访问",
        ]
    elif tool_name in ("file_write", "code_write", "code_create"):
        return [
            "确认目标目录是否存在",
            "检查文件路径是否有写权限",
        ]
    else:
        return [
            "检查参数格式是否正确",
            "跳过此步骤，继续后续任务",
        ]


def detect_loop(tool_calls_log: List) -> bool:
    """检测固定模式循环。"""
    if len(tool_calls_log) < 8:
        return False
    recent = tool_calls_log[-8:]
    names = [record_get(t, "tool", "") for t in recent]
    recent_errors = [record_get(t, "error") for t in recent]
    if len(recent_errors) >= 4 and all(e is not None for e in recent_errors[-4:]):
        return True
    signatures = []
    for t in recent:
        sig = (record_get(t, "tool", ""), json.dumps(record_get(t, "args", {}), sort_keys=True))
        signatures.append(sig)
    if len(set(signatures[-5:])) == 1:
        return True
    if len(names) >= 6:
        for cycle_len in (2, 3):
            needed = cycle_len * 3
            if len(names) >= needed:
                cycle = names[:cycle_len]
                expected = cycle * 3
                if expected == names[:needed] and len(set(cycle)) >= 2:
                    return True
    return False
