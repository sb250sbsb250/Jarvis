"""
engine/agent_loop.py — 自主 Agent 循环（编排层）

基于 Claude Code 模式：
    while not done:
        response = llm.chat(messages, tools)
        if tool_calls: 执行 → 追加到 messages
        else: done = True

设计原则：
  - messages 是唯一真相源，不维护额外状态结构
  - 框架只做编排，所有功能逻辑在子模块中
  - 工具加载时输出完整诊断信息，便于调试
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Set, Optional, Callable, Awaitable

from .tool.executor import ToolExecutor
from .tool.policy import ToolPolicy
from .tool.parser import make_tool_result, parse_tool_args, get_tool_param_hint
from .lint.runner import LintRunner
from .checkpoint import Checkpoint
from .llm_client import InsufficientBalanceError, LLMClient
from .prompt.complexity import ComplexityRouter, ResponseMode
from .prompt.context import (
    ContextBuilder, sanitize_tool_messages, _trim_history_messages,
)
from .core.approval import ApprovalGate
from .core.file_guard import FileEditGuard
from .core.script_detector import ScriptSuggestionDetector
from .core.types import ToolCallRecord, ToolResult
from .core.guard import (
    GuardState, ENABLE_RESULT_CACHE, ENABLE_HARD_INTERRUPT,
    ENABLE_EMPTY_REPLY_FIX, ENABLE_JSON_CORRECTION,
    ENABLE_KEY_FINDINGS, ENABLE_ERROR_GRADING,
    check_stuck, check_stuck_hard, guard_repeated_target,
    suggest_next_action, handle_tool_error, suggest_alternatives,
    detect_loop, record_get, safe_inject_system,
)

logger = logging.getLogger(__name__)


# ── 常量（仅 agent_loop 独有的）──

MAX_LLM_ERRORS = 3
MAX_TOOL_ERRORS = 8
TOOL_DEFAULT_TIMEOUT = 60.0
CHECKPOINT_INTERVAL = 10
MAX_TOOL_RESULT_CHARS = 80000

# 诊断开关
DIAG_ENABLED = True
DIAG_PRINT_LLM_INPUT = True
DIAG_MAX_MESSAGE_CHARS = 0
DIAG_PRINT_FULL_SYSTEM = False


def _diag_print(*args, **kwargs):
    if DIAG_ENABLED:
        print(*args, **kwargs)


class AgentLoop:
    """自主 Agent 循环 — 纯 LLM + 工具模式。"""

    def __init__(
        self,
        llm_client: Any,
        tool_registry: Any,
        max_rounds: int = 200,
        system_prompt: str = "",

        auto_lint: bool = True,
        lint_runner: Optional[LintRunner] = None,
        enable_checkpoint: bool = True,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.max_rounds = max_rounds
        self.base_system = system_prompt

        self.auto_lint = auto_lint
        self.lint_runner = lint_runner or LintRunner()
        self.edited_files: Set[str] = set()
        self.enable_checkpoint = enable_checkpoint
        self.skill: Optional[Any] = None

        policy = ToolPolicy()
        self._executor = ToolExecutor(
            registry=tool_registry,
            default_timeout=TOOL_DEFAULT_TIMEOUT,
            policy=policy,
        )

        # Claude Code 风格增强组件
        self._policy = policy
        self._approval_gate = ApprovalGate(policy, auto_approve=True)
        self._file_guard: Optional[FileEditGuard] = None
        self._script_detector = ScriptSuggestionDetector()
        self._current_on_event: Optional[Callable] = None

        self._checkpoint: Optional[Checkpoint] = None
        self._injector: Any = None
        self._topic_store: Any = None
        self._total_tokens_used: int = 0
        self._last_llm_usage: Optional[Dict] = None
        self._has_compressed: bool = False

        # 子模块实例（由子模块管理各自状态）
        self._context_builder = ContextBuilder()
        self._guard_state = GuardState()

        # 模型路由状态
        self._task_mode = None
        self._task_mode_info = None
        self._routed_model = None
        self._model_override = None
        self._effective_model = None
        self._routed_temperature = None
        self._routed_max_tokens = None

    # ═══════════════════════════════════════════════════════════════
    #  主循环
    # ═══════════════════════════════════════════════════════════════

    async def run(
            self,
            task: str,
            working_dir: str = ".",
            history: Optional[List[Dict]] = None,
            on_event: Optional[Callable[[str, Dict], Awaitable[None]]] = None,
            resume_from: Optional[str] = None,
            skip_last_user: bool = True,
            compressed_until: int = 0,
            compressed_summary: str = "",
            model_override: Optional[str] = None,
            mode: str = "coding",
    ) -> Dict[str, Any]:
        """自主执行任务。"""
        self.edited_files.clear()
        self._total_tokens_used = 0
        self._last_llm_usage = None
        self._has_compressed = False

        # 守卫状态重置
        self._guard_state = GuardState()

        self._current_mode = mode

        # 组件初始化
        self._file_guard = FileEditGuard(working_dir)
        self._script_detector.reset()
        self._current_on_event = on_event

        empty_count = 0
        tool_calls_log: List[ToolCallRecord] = []
        findings: List[str] = []
        llm_errors = 0
        tool_errors = 0
        start_round = 0

        # 🎯 自动技能匹配
        from .skill.matcher import match_skill
        self.skill = match_skill(task)
        if self.skill:
            logger.info(
                f"🎯 自动匹配技能: {self.skill.meta.icon} "
                f"{self.skill.meta.display_name} "
                f"({self.skill.meta.name})"
            )
        else:
            logger.info("🎯 无匹配技能，使用通用模式")

        # 诊断：启动时打印完整工具注册信息
        self._diag_print_startup(task, working_dir)

        # ── 检查点 ──
        if self.enable_checkpoint and not resume_from:
            self._checkpoint = Checkpoint(task)
            if self._checkpoint.exists():
                logger.info(f"🔁 发现未清理的检查点: {self._checkpoint.path}")
                if on_event:
                    await on_event("checkpoint", {"type": "found", "path": self._checkpoint.path})
                resume_from = self._checkpoint.path

        if resume_from:
            cp = Checkpoint(task, save_dir=os.path.dirname(os.path.abspath(resume_from)))
            state = cp.load()
            if state:
                messages = state.get("messages", [])
                tool_calls_log = state.get("tool_calls_log", [])
                start_round = state.get("round", 0)
                findings = state.get("findings", [])
                self._context_builder.compressed_summary = state.get("compressed_summary", "")
                self._context_builder.compressed_until = state.get("compressed_until", 0)
                self._total_tokens_used = state.get("total_tokens_used", 0)
                logger.info(
                    f"⏮️ 恢复检查点 (round={start_round}, "
                    f"messages={len(messages)}, "
                    f"tokens={self._total_tokens_used})"
                )
                if on_event:
                    await on_event(
                        "checkpoint",
                        {"type": "restored", "round": start_round, "total_tokens": self._total_tokens_used},
                    )
                messages = sanitize_tool_messages(messages)
            else:
                logger.warning(f"⚠️ 检查点加载失败，重新开始")
                messages = await self._context_builder.build_messages(
                    task=task, working_dir=working_dir,
                    skill=self.skill, base_system=self.base_system,
                    history=history, skip_last_user=skip_last_user,
                    compressed_until=compressed_until,
                    compressed_summary=compressed_summary,
                    mode=mode,
                )
        else:
            messages = await self._context_builder.build_messages(
                task=task, working_dir=working_dir,
                skill=self.skill, base_system=self.base_system,
                history=history, skip_last_user=skip_last_user,
                compressed_until=compressed_until,
                compressed_summary=compressed_summary,
                mode=mode,
            )

        # ── 长期记忆注入 ──
        if self._has_compressed:
            await self._try_save_topics(messages)
        else:
            self._injector = None

        # ── 模型路由 ──
        self._model_override = model_override
        try:
            router = ComplexityRouter(self.llm_client)
            complexity_mode, info = router.route(task)
            self._task_mode = complexity_mode
            self._task_mode_info = info
            if info:
                self._routed_model = info.get("model")
                self._routed_temperature = info.get("temperature")
                self._routed_max_tokens = info.get("max_tokens")
            _diag_print(
                f"  ComplexityRouter: mode={complexity_mode.name}, "
                f"model={self._routed_model or 'default'}, "
                f"tokens={self._routed_max_tokens or 'default'}"
            )
        except Exception as e:
            logger.debug(f"ComplexityRouter 不可用: {e}")
            self._task_mode = ResponseMode.STANDARD
            self._task_mode_info = None

        self._effective_model = self._model_override or self._routed_model

        # ── 模式配置覆盖（在 ComplexityRouter 之后应用） ──
        try:
            from .prompt.modes import get_mode_config
            mode_cfg = get_mode_config(mode)
            if mode_cfg.temperature is not None:
                self._routed_temperature = mode_cfg.temperature
            if mode_cfg.max_tokens is not None:
                self._routed_max_tokens = mode_cfg.max_tokens
            if mode_cfg.default_model and not self._model_override:
                self._effective_model = mode_cfg.default_model
        except Exception as e:
            logger.debug(f"模式配置未生效: {e}")

        # ═══════════════════════════════════════════════════════════
        #  主循环
        # ═══════════════════════════════════════════════════════════

        final_content = ""

        for round_display in range(start_round, self.max_rounds):
            try:
                round_idx = round_display - start_round

                # ── 守卫检测（在 LLM 调用前检查）──
                stuck_msg = check_stuck(self._guard_state, tool_calls_log)
                if stuck_msg:
                    safe_inject_system(messages, stuck_msg)
                    tool_calls_log.append(ToolCallRecord(
                        tool="__system__", args={}, round=round_display,
                        call_id="", error=stuck_msg,
                    ))
                    logger.info(f"🛡️ 守卫触发: {stuck_msg[:60]}")

                # ── 准备工具列表 ──
                from .skill.matcher import get_filtered_tools
                tools = get_filtered_tools(self.tool_registry, self.skill)

                # ── 模式级工具过滤 ──
                try:
                    from .prompt.modes import get_mode_config
                    mode_cfg = get_mode_config(mode)
                    if mode_cfg.allowed_tools is not None:
                        _allowed_set = set(mode_cfg.allowed_tools)
                        tools = [
                            t for t in tools
                            if t.get("function", {}).get("name") in _allowed_set
                        ]
                except Exception:
                    pass

                # ── LLM 调用 ──
                kwargs = {}
                if self._routed_temperature is not None:
                    kwargs["temperature"] = self._routed_temperature
                if self._routed_max_tokens is not None:
                    kwargs["max_tokens"] = self._routed_max_tokens
                if self._effective_model:
                    kwargs["model"] = self._effective_model

                # 诊断输出：LLM 输入（消息概览 + 工具）
                if DIAG_PRINT_LLM_INPUT:
                    self._diag_print_llm_input(messages, tools, round_display)

                response = await self.llm_client.chat_completion(
                    messages=messages,
                    tools=tools if tools else None,
                    **kwargs,
                )

                # ── 用量统计 ──
                usage = response.get("usage", {})
                if usage:
                    total_tokens = usage.get("total_tokens", 0) or usage.get(
                        "total_tokens_used", 0
                    )
                    if total_tokens:
                        self._total_tokens_used = total_tokens
                    self._last_llm_usage = usage

                choice = response.get("choices", [{}])[0]
                message = choice.get("message", {})
                content = message.get("content", "") or ""
                tool_calls = message.get("tool_calls", None)

                # 诊断输出
                self._diag_print_llm_response(response, round_display)

                # ── Token 预算检查 + 压缩 ──
                if self._context_builder.should_compress(messages):
                    if ENABLE_KEY_FINDINGS and self._guard_state.key_findings:
                        kf_text = "## 已获取的关键信息（无需重复读取）\n" + "\n".join(
                            f"- {path}: {info[:200]}"
                            for path, info in list(self._guard_state.key_findings.items())[-20:]
                        )
                    else:
                        kf_text = ""
                    await self._context_builder.compress(
                        messages, self.llm_client, round_display,
                        key_findings_text=kf_text,
                    )
                    self._has_compressed = True

                # ── 空回复处理 (Fix 2) ──
                if not content and not tool_calls:
                    if ENABLE_EMPTY_REPLY_FIX:
                        empty_count += 1
                        if empty_count == 1:
                            suggestion = suggest_next_action(messages, tool_calls_log)
                            logger.info(f"💬 空回复干预: 注入 user 角色建议")
                            messages.append({"role": "user", "content": suggestion})
                            continue
                        elif empty_count >= 2:
                            logger.warning(f"🚫 连续 {empty_count} 次空回复，强制终止")
                            done = True
                            final_content = (
                                f"[系统终止] 连续 {empty_count} 次输出空内容，"
                                f"已终止任务。\n\n"
                                f"## 已完成的操作\n"
                                + _format_tool_log(tool_calls_log[-10:])
                                + "\n\n## 总结\n任务未能完成，原因是 Agent 持续输出空内容。"
                            )
                            break
                    else:
                        # 原有逻辑：最多容忍 3 次空回复
                        empty_count += 1
                        if empty_count >= 3:
                            logger.warning(f"🚫 连续 {empty_count} 次空回复，强制终止")
                            done = True
                            final_content = f"[系统终止] 连续 {empty_count} 次空回复"
                            break
                        else:
                            continue

                # ── 追加 assistant 回复到对话历史（根治孤立 tool 消息）──
                messages.append(message)

                if tool_calls:
                    empty_count = 0  # 有正常输出，重置计数器

                    # ── 处理工具调用 ──
                    for tc in tool_calls:
                        tool_name = tc.get("function", {}).get("name", "")
                        tool_args_raw = tc.get("function", {}).get("arguments", "{}")
                        call_id = tc.get("id", "")

                        if isinstance(tool_args_raw, dict):
                            tool_args = tool_args_raw
                        elif isinstance(tool_args_raw, str) and tool_args_raw.strip():
                            if ENABLE_JSON_CORRECTION:
                                tool_args, parse_ok = parse_tool_args(
                                    tool_name, tool_args_raw, self.tool_registry,
                                )
                                if not parse_ok:
                                    param_hint = get_tool_param_hint(self.tool_registry, tool_name)
                                    correction = (
                                        f"[系统提示] 你上次调用 `{tool_name}` 的参数 JSON 解析失败。\n"
                                        f"原始内容: {tool_args_raw[:300]}\n"
                                        f"{param_hint}\n"
                                        f"请用正确的 JSON 格式重新调用此工具。"
                                    )
                                    logger.warning(f"⚠️ JSON 解析全失败，注入修正请求")
                                    messages.append({"role": "user", "content": correction})
                                    tool_calls_log.append(ToolCallRecord(
                                        tool=tool_name, args={}, round=round_display,
                                        call_id=call_id, error="JSON 解析失败",
                                    ))
                                    continue
                            else:
                                # 原有三层解析逻辑
                                try:
                                    tool_args = json.loads(tool_args_raw)
                                except json.JSONDecodeError:
                                    stripped = tool_args_raw.strip()
                                    if stripped.startswith("{") and stripped.endswith("}"):
                                        try:
                                            import ast
                                            if len(stripped) < 10000:
                                                parsed = ast.literal_eval(stripped)
                                                tool_args = parsed if isinstance(parsed, dict) else {"raw": tool_args_raw}
                                            else:
                                                tool_args = {"raw": tool_args_raw}
                                        except (ValueError, SyntaxError, MemoryError):
                                            tool_args = None
                                    else:
                                        tool_args = None
                                    if tool_args is None:
                                        try:
                                            fixed = re.sub(
                                                r'"command"\s*:\s*"(.+?)"(?=\s*[,}]|\s*\n\s*"(?!command))',
                                                lambda m: f'"command": {json.dumps(m.group(1))}',
                                                tool_args_raw, flags=re.DOTALL,
                                            )
                                            tool_args = json.loads(fixed)
                                        except (json.JSONDecodeError, Exception):
                                            logger.warning(f"[tc] arguments 解析失败: {tool_args_raw[:200]}")
                                            tool_args = {"raw": tool_args_raw}
                        else:
                            tool_args = {}

                        # ── 参数校验 ──
                        tool_def = self.tool_registry.get_tool_def(tool_name) if hasattr(self.tool_registry, 'get_tool_def') else None
                        has_required = False
                        if tool_def:
                            has_required = any(p.required for p in tool_def.parameters)
                        meaningful = any(
                            v and str(v).strip() for v in tool_args.values()
                        ) if tool_args else False
                        if has_required and not meaningful:
                            required_names = [p.name for p in tool_def.parameters if p.required]
                            logger.warning(f"⚠️ [{tool_name}] 缺少必填参数: {required_names}")
                            messages.append(make_tool_result(
                                call_id,
                                f"[系统提示] `{tool_name}` 缺少必填参数: {', '.join(required_names)}。请提供完整参数后重试。",
                            ))
                            tool_calls_log.append(ToolCallRecord(
                                tool=tool_name, args=tool_args, round=round_display,
                                call_id=call_id, error="缺少必填参数",
                            ))
                            continue

                        # ── 同意门 ──
                        if self._approval_gate:
                            approved = await self._approval_gate.check_and_wait(
                                tool_name, tool_args,
                                on_event=self._current_on_event,
                                call_id=call_id,
                            )
                            if approved is False:
                                messages.append(make_tool_result(
                                    call_id,
                                    f"[用户拒绝] `{tool_name}` 已被用户拒绝。",
                                ))
                                tool_calls_log.append(ToolCallRecord(
                                    tool=tool_name, args=tool_args, round=round_display,
                                    call_id=call_id, error="用户拒绝",
                                ))
                                continue

                        # ── 目标追踪 + 硬中断 ──
                        guard_repeated_target(self._guard_state, tool_name, tool_args)
                        if ENABLE_HARD_INTERRUPT:
                            hard_msg = check_stuck_hard(self._guard_state, tool_name, tool_args)
                            if hard_msg:
                                logger.warning(f"🚫 硬中断: {tool_name} 已连续调用 3 次相同参数")
                                messages.append(make_tool_result(call_id, hard_msg))
                                tool_calls_log.append(ToolCallRecord(
                                    tool=tool_name, args=tool_args,
                                    round=round_display, call_id=call_id,
                                    error="硬中断-重复调用",
                                ))
                                continue

                        # ── 执行工具 ──
                        if self._current_on_event:
                            try:
                                await self._current_on_event("tool_call", {
                                    "tool": tool_name,
                                    "args": tool_args,
                                    "call_id": call_id,
                                })
                            except Exception:
                                pass
                        try:
                            result = await self._execute_tool(
                                tool_name, tool_args, call_id,
                            )
                        except Exception as e:
                            result = ToolResult.fail(
                                call_id=call_id, tool_name=tool_name,
                                error=str(e),
                            )

                        # ── 处理结果 ──
                        is_success = getattr(result, 'success', False)
                        content_val = getattr(result, 'content', None)
                        result_str = str(content_val) if content_val else ""
                        error_val = getattr(result, 'error', None)
                        error_str = str(error_val) if isinstance(error_val, str) else ""

                        if not is_success and not error_str and not result_str:
                            error_str = "工具执行返回空"

                        # ── 发射 tool_result / tool_error 事件 ──
                        if self._current_on_event:
                            try:
                                if not is_success:
                                    await self._current_on_event("tool_error", {
                                        "tool": tool_name,
                                        "args": tool_args,
                                        "call_id": call_id,
                                        "error": error_str or "工具执行失败",
                                    })
                                else:
                                    await self._current_on_event("tool_result", {
                                        "tool": tool_name,
                                        "args": tool_args,
                                        "call_id": call_id,
                                        "result": result_str,
                                    })
                            except Exception:
                                pass

                        # 诊断输出：工具执行结果
                        _diag_print(
                            f"  {'✅' if is_success else '❌'} [{tool_name}] "
                            f"{'成功' if is_success else '失败'} | "
                            f"结果: {(result_str or error_str or '').replace(chr(10), ' ')[:300]}"
                        )

                        if not is_success:
                            logger.warning(f"❌ [{tool_name}] 错误: {(error_str or '工具执行失败')[:200]}")
                            tool_errors += 1

                        # ── 格式化结果 ──
                        if result_str and len(result_str) > MAX_TOOL_RESULT_CHARS:
                            result_str = result_str[:MAX_TOOL_RESULT_CHARS] + "\n... [结果已截断]"

                        # ── 统一添加 tool result ──
                        messages.append(make_tool_result(call_id, result_str))

                        # ── 记录 ──
                        tool_calls_log.append(ToolCallRecord(
                            tool=tool_name, args=tool_args,
                            round=round_display, call_id=call_id,
                            error=error_str if not is_success else None,
                        ))

                        # ── Fix 4: 捕获关键发现 ──
                        if ENABLE_KEY_FINDINGS and is_success:
                            if (hasattr(self.tool_registry, 'is_read_tool')
                                    and self.tool_registry.is_read_tool(tool_name)):
                                file_path = tool_args.get("path", tool_args.get("url", ""))
                                if file_path and result_str:
                                    lines_count = result_str.count("\n") + 1
                                    self._guard_state.key_findings[file_path] = (
                                        f"Round {round_display}, {len(result_str)} chars, "
                                        f"{lines_count} lines, preview: {result_str[:300]}"
                                    )

                        # ── Fix 5: 错误分级引导 ──
                        if not is_success and ENABLE_ERROR_GRADING:
                            handle_tool_error(
                                messages, tool_name, error_str,
                                tool_calls_log, self.tool_registry,
                            )

                        # ── ScriptSuggestionDetector ──
                        self._script_detector.record(tool_name, is_success)

                        # ── Todo 更新事件 ──
                        if (on_event and error_str and "todo_write" in error_str
                                and "已完成" in error_str):
                            try:
                                await on_event("todo_update", {"message": error_str})
                            except Exception:
                                pass

                    # ── 轮次内 lint 检查 ──
                    if self.auto_lint and round_idx > 0 and round_idx % 5 == 0:
                        await self._auto_lint_check(findings, round_idx, on_event, messages=messages)

                    # ── 循环检测 ──
                    if detect_loop(tool_calls_log):
                        logger.warning("⚠️ 检测到循环模式")
                        safe_inject_system(
                            messages,
                            "⚠️ 检测到重复操作循环。请跳出当前思路，"
                            "考虑完全不同的方法或基于已有信息输出结论。"
                        )

                    # ── 检查点 ──
                    if self.enable_checkpoint and self._checkpoint:
                        if round_idx > 0 and round_idx % CHECKPOINT_INTERVAL == 0:
                            self._checkpoint.save(
                                messages=messages,
                                tool_calls_log=tool_calls_log,
                                round_idx=round_display,
                                findings=findings,
                                compressed_summary=self._context_builder.compressed_summary,
                                total_tokens_used=self._total_tokens_used,
                                compressed_until=self._context_builder.compressed_until,
                            )
                            logger.info(f"💾 检查点保存 (round={round_display})")

                    # ── 工具错误率检查 ──
                    if tool_errors >= MAX_TOOL_ERRORS:
                        logger.warning(
                            f"🚫 工具错误数已达 {tool_errors}/{MAX_TOOL_ERRORS}，终止"
                        )
                        done = True
                        final_content = (
                            f"[系统终止] 工具错误数已达上限 ({tool_errors})，终止任务。\n\n"
                            f"## 已完成的操作\n"
                            + _format_tool_log(tool_calls_log[-10:])
                        )
                        break

                    # ── LLM 错误检查 ──
                    if llm_errors >= MAX_LLM_ERRORS:
                        logger.warning(f"🚫 LLM 错误已达 {llm_errors}，终止")
                        done = True
                        final_content = f"[系统终止] LLM 错误达上限"
                        break

                else:
                    # ── LLM 直接回答（无工具调用）→ 结束 ──
                    done = True
                    final_content = content or ""
                    break

            except InsufficientBalanceError as e:
                logger.warning(f"🚫 余额不足: {e}")
                if on_event:
                    await on_event("insufficient_balance", {"error": str(e)})
                return {
                    "success": False,
                    "error": f"余额不足: {e}",
                    "messages": messages,
                    "total_tokens": self._total_tokens_used,
                    "llm_usage": self._last_llm_usage,
                }
            except Exception as e:
                llm_errors += 1
                logger.error(f"💥 round {round_display} 异常: {e}", exc_info=True)
                if llm_errors >= MAX_LLM_ERRORS:
                    done = True
                    final_content = f"[系统终止] LLM 调用异常: {e}"
                    break
                else:
                    messages.append({
                        "role": "system",
                        "content": f"⚠️ 上轮异常: {e}，请继续。",
                    })
                    continue
        else:
            # 自然达到 max_rounds
            final_content = f"[达到最大轮数 {self.max_rounds}]"

        # ═══════════════════════════════════════════════════════════
        #  返回结果
        # ═══════════════════════════════════════════════════════════

        result = {
            "success": True,
            "content": final_content,
            "messages": messages,
            "tool_calls_log": tool_calls_log,
            "total_tokens": self._total_tokens_used,
            "llm_usage": self._last_llm_usage,
            "compressed_until": self._context_builder.compressed_until,
            "compressed_summary": self._context_builder.compressed_summary,
            "rounds": round_display,
        }

        if self.enable_checkpoint and self._checkpoint:
            self._checkpoint.cleanup()

        if self.auto_lint and findings:
            result["lint_findings"] = findings

        if on_event:
            try:
                await on_event("done", result)
            except Exception:
                pass

        return result

    # ═══════════════════════════════════════════════════════════════
    #  工具执行
    # ═══════════════════════════════════════════════════════════════

    async def _execute_tool(self, tool_name: str, tool_args: Dict, call_id: str = "") -> Any:
        """工具执行（含结果缓存逻辑）。"""
        # ── Fix 1: 只读工具结果缓存 ──
        _is_read = (
            ENABLE_RESULT_CACHE
            and hasattr(self.tool_registry, 'is_read_tool')
            and self.tool_registry.is_read_tool(tool_name)
        )
        if _is_read:
            cache_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
            if cache_key in self._guard_state.result_cache:
                cached = self._guard_state.result_cache[cache_key]
                logger.info(f"🔄 [{tool_name}] 缓存命中，跳过实际执行")
                return ToolResult.ok(
                    call_id, tool_name,
                    f"[缓存结果 — 已在前面步骤中获取，无需重复读取]\n{cached}",
                )

        # ── 写操作：清除相关缓存 ──
        if tool_name in ("file_write", "code_write", "code_create", "code_append",
                         "file_append", "file_rename", "shell_run"):
            if self._guard_state.result_cache:
                self._guard_state.result_cache.clear()
                logger.debug("🗑️ 写操作/Shell 执行，已清除结果缓存")

        # v3.0: 使用 registry.execute() 扁平化调度
        if hasattr(self.tool_registry, 'execute'):
            logger.info(f"⚡ [{tool_name}] | 参数: {tool_args}")
            if tool_name in ("edit", "write_file", "code_editor", "diff_file", "file"):
                file_path = tool_args.get("path", tool_args.get("file_path", ""))
                if file_path:
                    self.edited_files.add(file_path)
            start = time.time()
            result = await self.tool_registry.execute(tool_name, call_id, **tool_args)
            elapsed = time.time() - start
            logger.info(f"✅ [{tool_name}] 完成 ({elapsed:.1f}s)")

            # 缓存只读工具的成功结果
            if _is_read and hasattr(result, 'success') and result.success:
                content_str = str(result.content)[:5000] if result.content else ""
                if content_str:
                    self._guard_state.result_cache[cache_key] = content_str

            # 归一化：确保返回 ToolResult
            if not isinstance(result, ToolResult):
                logger.warning(f"⚠️ [{tool_name}] 返回了非 ToolResult 类型: {type(result).__name__}，已自动包装")
                if isinstance(result, str):
                    return ToolResult.ok(call_id, tool_name, result)
                elif isinstance(result, dict):
                    return ToolResult.ok(call_id, tool_name, result)
                else:
                    return ToolResult.ok(call_id, tool_name, str(result) if result is not None else "")

            return result

        # 向后兼容
        tool = self.tool_registry.get(tool_name) if hasattr(self.tool_registry, 'get') else None
        if not tool:
            raise ValueError(f"未知工具: {tool_name}")
        logger.info(f"⚡ [{tool_name}] | 参数: {tool_args}")
        if tool_name in ("edit", "write_file", "code_editor", "diff_file", "file"):
            file_path = tool_args.get("path", tool_args.get("file_path", ""))
            if file_path:
                self.edited_files.add(file_path)
        start = time.time()
        result = await tool.execute(call_id, **tool_args)
        elapsed = time.time() - start
        logger.info(f"✅ [{tool_name}] 完成 ({elapsed:.1f}s)")

        if _is_read and hasattr(result, 'success') and result.success:
            content_str = str(result.content)[:5000] if result.content else ""
            if content_str:
                self._guard_state.result_cache[cache_key] = content_str

        # 归一化：确保返回 ToolResult
        if not isinstance(result, ToolResult):
            logger.warning(f"⚠️ [{tool_name}] 返回了非 ToolResult 类型: {type(result).__name__}，已自动包装")
            if isinstance(result, str):
                return ToolResult.ok(call_id, tool_name, result)
            elif isinstance(result, dict):
                return ToolResult.ok(call_id, tool_name, result)
            else:
                return ToolResult.ok(call_id, tool_name, str(result) if result is not None else "")

        return result

    # ═══════════════════════════════════════════════════════════════
    #  长期记忆
    # ═══════════════════════════════════════════════════════════════

    def _ensure_injector(self) -> None:
        if self._injector is not None:
            return
        try:
            from engine.longterm.topic_inject import get_injector
            self._injector = get_injector()
            self._topic_store = self._injector.store
        except Exception as e:
            logger.debug(f"长期记忆未启用: {e}")
            self._injector = None

    async def _try_save_topics(self, messages: List[Dict]) -> None:
        self._ensure_injector()
        if not self._injector or not self._topic_store:
            return
        try:
            from engine.longterm.topic_compress import compress_dialogue
            topics = await compress_dialogue(
                messages=messages[-40:],
                llm_client=self.llm_client,
                store=self._topic_store,
                min_importance=0.3,
            )
            if topics:
                logger.info(f"💾 提取 {len(topics)} 个长期记忆")
        except Exception as e:
            logger.debug(f"长期记忆保存跳过: {e}")

    # ═══════════════════════════════════════════════════════════════
    #  Lint 检查
    # ═══════════════════════════════════════════════════════════════

    async def _auto_lint_check(
        self, findings: List[str], round_idx: int,
        on_event: Optional[Callable] = None,
        messages: Optional[List[Dict]] = None,
    ) -> None:
        for file_path in list(self.edited_files):
            if not os.path.isfile(file_path):
                continue
            try:
                result = await self.lint_runner.run(file_path)
                if result.get("passed"):
                    if on_event:
                        await on_event("lint_pass", {"file": file_path})
                else:
                    # 将 lint 结果注入到 LLM 上下文，让 LLM 知道代码有问题
                    feedback = self.lint_runner.format_feedback(result, file_path)
                    if feedback and messages is not None:
                        safe_inject_system(messages, feedback)
                    msg = f"⚠️ Lint 提醒: {file_path} 有代码风格问题"
                    findings.append(msg)
                    if on_event:
                        await on_event("lint_error", {"file": file_path})
            except Exception as e:
                logger.debug(f"Lint 跳过 {file_path}: {e}")

    # ═══════════════════════════════════════════════════════════════
    #  诊断输出
    # ═══════════════════════════════════════════════════════════════

    def _diag_print_startup(self, task: str, working_dir: str) -> None:
        _diag_print(f"{'=' * 80}")
        _diag_print(f"🤖 AgentLoop 启动")
        _diag_print(f"  Task: {task[:200]}")
        _diag_print(f"  CWD: {working_dir}")
        _diag_print(f"  Model: {self.llm_client.model if hasattr(self.llm_client, 'model') else 'default'}")
        _diag_print(f"  Skill: {self.skill.meta.display_name if self.skill else 'None'}")

        try:
            tool_count = len(self.tool_registry.list_tools()) if hasattr(self.tool_registry, 'list_tools') else 0
            _diag_print(f"  Tools: {tool_count} registered")
        except Exception:
            pass

        _diag_print(f"{'=' * 80}")

    def _diag_print_llm_input(self, messages: List[Dict], tools: List[Dict], round_num: int) -> None:
        """诊断输出：打印发送给 LLM 的消息概览和工具列表"""
        if not DIAG_ENABLED:
            return
        _diag_print(f"  {'─' * 50}")
        _diag_print(f"  📤 R{round_num} LLM 输入 ({len(messages)} 条消息)")
        # 打印各条消息的概览
        for i, m in enumerate(messages):
            role = m.get("role", "?")
            content = m.get("content", "") or ""
            tcs = m.get("tool_calls")
            tc_id = m.get("tool_call_id", "")
            label = f"[{i}] {role}"
            if role == "tool":
                # tool 结果：只显示前 200 字符
                preview = content[:200].replace(chr(10), " ")
                _diag_print(f"    {label} ({tc_id[:20]}): {preview}")
            elif role == "assistant" and tcs:
                names = [tc.get("function", {}).get("name", "?") for tc in tcs]
                _diag_print(f"    {label}: tool_calls → {', '.join(names)}")
            elif role == "system":
                if DIAG_PRINT_FULL_SYSTEM:
                    _diag_print(f"    {label}: {content[:500].replace(chr(10), ' ')}")
                else:
                    _diag_print(f"    {label}: (~{len(content)} chars)")
            else:
                preview = content[:300].replace(chr(10), " ")
                _diag_print(f"    {label}: {preview}")
        # 打印工具列表概览
        if tools:
            tool_names = [t.get("function", {}).get("name", "?") for t in tools]
            _diag_print(f"    🔧 工具: {', '.join(tool_names)}")
        _diag_print(f"  {'─' * 50}")

    def _diag_print_llm_response(self, response: Dict, round_num: int) -> None:
        if not DIAG_ENABLED:
            return
        choice = response.get("choices", [{}])[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "?")
        content = message.get("content", "") or ""
        tcs = message.get("tool_calls", None)

        _diag_print(f"  {'─' * 50}")
        if tcs:
            _diag_print(f"  🤖 R{round_num} LLM 工具调用 ({finish_reason}):")
            for tc in tcs:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                args = fn.get("arguments", "")
                try:
                    args_pretty = json.dumps(json.loads(args), ensure_ascii=False, indent=2)
                except Exception:
                    args_pretty = str(args)[:500]
                _diag_print(f"    🛠️  {name}")
                for line in args_pretty.split(chr(10)):
                    _diag_print(f"      {line}")
        else:
            _diag_print(f"  🤖 R{round_num} LLM 回答 ({finish_reason}):")
            # 显示完整内容（或截断到 DIAG_MAX_MESSAGE_CHARS）
            max_chars = DIAG_MAX_MESSAGE_CHARS if DIAG_MAX_MESSAGE_CHARS > 0 else len(content)
            if content:
                _diag_print(f"{content[:max_chars]}")
            else:
                _diag_print(f"    (空回复)")
        _diag_print(f"  {'─' * 50}")


# ═══════════════════════════════════════════════════════════════
#  模块级辅助函数
# ═══════════════════════════════════════════════════════════════

def _format_tool_log(tool_calls_log: List) -> str:
    """格式化工具调用日志（用于终止消息）。"""
    lines = []
    _get = record_get
    for t in tool_calls_log:
        name = _get(t, "tool", "?")
        err = _get(t, "error")
        status = "✅" if not err else "❌"
        error_info = f": {err[:80]}" if err else ""
        lines.append(f"  {status} {name}{error_info}")
    return "\n".join(lines)
