"""
engine/agent_loop.py — 自主 Agent 循环（增强版：工作记忆 + 自我反思 + Token 预算 + 检查点）

基于 Claude Code 模式改进：
    while not done:
        response = llm.chat(messages, tools)
        if tool_calls: 执行 → 更新工作记忆 → 追加到 messages
        else: done = True

改进特性：
  - 工作记忆（WorkingMemory）：追踪已读取/已写入/已失败的操作
  - 自我反思（SelfReflection）：连续失败后强制反思
  - Token 预算管理（TokenBudget）：超 80% 上下文字动压缩旧轮次
  - 检查点（Checkpoint）：每 N 轮保存状态，支持中断恢复
  - 工具失败自动修正提示
  - 死循环检测
  - 自动 lint
  - 环境信息收集
  - 工具结果截断保护
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Set, Optional, Callable, Awaitable

from .tool.registry import ToolRegistry
from .tool.executor import ToolExecutor
from .tool.policy import ToolPolicy
from .core.types import ToolCall, ToolResult
from .lint.runner import LintRunner
from .memory.working_memory import WorkingMemory
from .checkpoint import Checkpoint
from .llm_client import InsufficientBalanceError

logger = logging.getLogger(__name__)


# ── 常量 ──

MAX_TOOL_RESULT_CHARS = 8000
MAX_CONTEXT_TOKENS = 128_000          # 参考 DeepSeek 上下文窗口
TOKEN_SAFETY_MARGIN = 0.80            # 超过 80% 开始压缩
CHECKPOINT_INTERVAL = 10              # 每 10 轮保存一次检查点
REFLECTION_THRESHOLD = 3              # 连续 3 次失败触发反思
COMPRESS_ROUNDS = 2                   # 每次压缩删除 2 轮
KEEP_RECENT_TURNS = 3                 # 注入历史时，保留最近 N 轮完整对话；更老的做摘要

# ── 错误阈值（分离 LLM 和工具错误）──
MAX_LLM_ERRORS = 3                    # LLM 连续错误上限
MAX_TOOL_ERRORS = 8                   # 工具连续错误上限（工具错误更常见，容忍度更高）
TOOL_DEFAULT_TIMEOUT = 60.0           # 工具执行默认超时（秒）


class AgentLoop:
    """
    自主 Agent 循环 — 纯 LLM + 工具模式

    用法:
        loop = AgentLoop(llm_client=..., tool_registry=..., system_prompt=...)
        result = await loop.run(task="...", working_dir=".")
    """

    # 通用执行框架模板（不变的部分），{{ }} 由运行时填充
    _BASE_TEMPLATE = """你是 Jarvis，一个强大的自主智能助手。

{{ skill_prompt }}

## 工作环境
{{ env_info }}

## 可用工具
{{ available_tools }}

## 当前任务
{{ task }}

{{ user_profile }}

## 约束条件
{{ constraints }}

## 核心原则
1. 理解优先，动手在后 — 先分析意图，再行动
2. 一次一步，步步验证 — 完成后检查结果
3. 记忆复用，避免重复 — 牢记已获取的信息
4. 失败不是终点 — 分析原因，换方式重试（同方式最多2次）
5. 不需要征询用户同意即可执行搜索、读取、分析
6. 任务完成或无法继续时，给出清晰总结

## 信息整合规则
- 读取到用户背景信息（简历、档案、偏好）时，检查是否有空白身份文件（USER.md、IDENTITY.md、MEMORY.md），有则自动填充
- 能用已有信息填的先填好，再让用户确认。禁止明知信息够用却从零开始提问
- 一次对话中获取的新信息，结束前总结并回写到对应文件

## 错误处理
- 工具返回空：检查参数，换工具，或写入文件再读
- 工具返回错误：参数错→修正；权限错→说明；超时→减量
- 最多重试2次同方式，第3次必须换方案
- 注意不要重复调用相同工具读取相同内容

## 输出要求
- 用中文回答，代码注释用英文
- 完成时给出核心结论 + 关键步骤
- 无法完成时说明原因 + 需要什么
- 主动汇报进度"""

    # 模板变量模式
    _TEMPLATE_PATTERN = re.compile(r'\{\{\s*(\w+)\s*\}\}')

    def __init__(
        self,
        llm_client: Any,
        tool_registry: Any,
        max_rounds: int = 200,
        system_prompt: str = "",
        skill: Optional[Any] = None,
        auto_lint: bool = True,
        lint_runner: Optional[LintRunner] = None,
        enable_checkpoint: bool = True,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.max_rounds = max_rounds
        self.base_system = system_prompt
        self.skill = skill
        self.auto_lint = auto_lint
        self.lint_runner = lint_runner or LintRunner()
        self.edited_files: Set[str] = set()
        self.enable_checkpoint = enable_checkpoint

        # 工具执行器（含超时 + 权限 + 重试）
        policy = ToolPolicy()
        self._executor = ToolExecutor(
            registry=tool_registry,
            default_timeout=TOOL_DEFAULT_TIMEOUT,
            policy=policy,
        )

        # 工作记忆（每轮更新）
        self.working_memory = WorkingMemory()

        # 检查点（run 时按 task_id 创建）
        self._checkpoint: Optional[Checkpoint] = None

        # Token 追踪
        self._total_tokens_used: int = 0
        self._last_llm_usage: Optional[Dict] = None
        self._has_compressed: bool = False

    async def run(
        self,
        task: str,
        working_dir: str = ".",
        history: Optional[List[Dict]] = None,
        on_event: Optional[Callable[[str, Dict], Awaitable[None]]] = None,
        resume_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        自主执行任务。

        Args:
            task: 用户任务描述
            working_dir: 工作目录
            history: 之前的对话历史
            on_event: 事件回调 async fn(event_type, data)
                      类型: "planning" | "round_start" | "tool_call"
                           | "tool_result" | "tool_error"
                           | "reflection" | "token_warning"
                           | "checkpoint" | "compress"
                           | "lint_pass" | "lint_error" | "done"
            resume_from: 检查点路径（从断点恢复）

        Returns:
            {"success": bool, "content": str, "rounds": int, "tool_calls": [...]}
        """
        self.edited_files.clear()
        self.working_memory.clear()
        self._total_tokens_used = 0
        self._last_llm_usage = None
        self._has_compressed = False

        tool_calls_log: List[Dict] = []
        findings: List[str] = []
        llm_errors = 0
        tool_errors = 0
        start_round = 0

        # ── 检查点初始化 ──
        if self.enable_checkpoint and not resume_from:
            self._checkpoint = Checkpoint(task)
            if self._checkpoint.exists():
                # 发现未清理的检查点（上次异常退出）
                logger.info(f"🔁 发现未清理的检查点: {self._checkpoint.path}")
                if on_event:
                    await on_event("checkpoint", {
                        "type": "found",
                        "path": self._checkpoint.path,
                    })
                # 自动恢复（不询问）
                resume_from = self._checkpoint.path

        # ── 从断点恢复 ──
        if resume_from:
            cp = Checkpoint(task, save_dir=os.path.dirname(os.path.abspath(resume_from)))
            state = cp.load()
            if state:
                messages = state.get("messages", [])
                tool_calls_log = state.get("tool_calls_log", [])
                findings = state.get("findings", [])
                start_round = state.get("round", 0)
                wm_data = state.get("working_memory")
                if wm_data:
                    self.working_memory = WorkingMemory.from_dict(wm_data)
                # 恢复已编辑文件列表（用于自动 lint）
                ef_data = state.get("edited_files", [])
                self.edited_files = set(ef_data) if ef_data else set()
                logger.info(
                    f"🔁 从检查点恢复: 第{start_round}轮, "
                    f"{len(messages)}条消息, {len(tool_calls_log)}个工具调用, "
                    f"{len(self.edited_files)}个已编辑文件"
                )
                if on_event:
                    await on_event("checkpoint", {
                        "type": "resumed",
                        "round": start_round,
                    })
                # 恢复后使用新的检查点
                if self.enable_checkpoint:
                    self._checkpoint = Checkpoint(task)
            else:
                # 检查点无效，从头开始
                logger.warning("检查点无效，从头开始")
                messages = await self._build_messages(task, working_dir, history)
                start_round = 0
                if self.enable_checkpoint:
                    self._checkpoint = Checkpoint(task)
        else:
            messages = await self._build_messages(task, working_dir, history)
            start_round = 0

        # ⭐ 预判
        if on_event:
            await self._emit_planning(task, on_event)

        start_time = time.time()

        # ── 主循环 ──
        for round_idx in range(start_round, self.max_rounds):
            round_display = round_idx + 1
            logger.info(
                f"🔄 Agent 第 {round_display}/{self.max_rounds} 轮 | "
                f"已执行 {len(tool_calls_log)} 个工具调用 | "
                f"已编辑 {len(self.edited_files)} 个文件 | "
                f"WM 连续错误: {self.working_memory.consecutive_errors}"
            )

            if on_event:
                await on_event("round_start", {"round": round_display})

            # ── 注入工作记忆提醒（每轮开始前） ──
            reminder = self.working_memory.get_reminder()
            if reminder:
                # 检查上一轮是否已经有工作记忆提醒，避免重复
                has_recent_reminder = any(
                    isinstance(m, dict) and m.get("role") == "system"
                    and m.get("content", "").startswith("## 📋 工作记忆")
                    for m in messages[-2:]
                )
                if not has_recent_reminder:
                    self._safe_inject_system(messages, reminder)
                    logger.debug(f"📋 注入工作记忆({len(reminder)}字符)")

            # ── 自我反思（连续失败检测） ──
            if self.working_memory.need_reflection(REFLECTION_THRESHOLD):
                reflection = self.working_memory.get_reflection_prompt()
                if reflection:
                    logger.warning(f"🧠 触发自我反思（连续{self.working_memory.consecutive_errors}次错误）")
                    self._safe_inject_system(messages, reflection)
                    if on_event:
                        await on_event("reflection", {
                            "consecutive_errors": self.working_memory.consecutive_errors,
                        })
                    # 反思也算一次"错误"重置节奏——注入后重置计数避免循环反思
                    # 实际上反思后 LLM 可能成功也可能失败，重置计数让反思只触发一次
                    # 如果反思后仍然失败，计数会重新累积到阈值再次触发
                    self.working_memory.consecutive_errors = 0

            # ── 获取工具列表 ──
            tools = []
            if self.tool_registry and hasattr(self.tool_registry, 'get_openai_tools'):
                tools = self.tool_registry.get_openai_tools()

            # ── LLM 调用 ──
            try:
                response = await self.llm_client.chat_completion(
                    messages=messages,
                    temperature=0.3,
                    max_tokens=4096,
                    tools=tools if tools else None,
                )
            except InsufficientBalanceError as e:
                # 余额不足 → 不重试，直接友好提示
                msg = str(e)
                logger.error(f"💰 {msg}")
                findings.append(f"💰 {msg}")
                messages.append({"role": "assistant", "content": msg})
                if on_event:
                    await on_event("done", {
                        "rounds": round_display,
                        "tool_calls": len(tool_calls_log),
                        "content": msg,
                    })
                if self._checkpoint:
                    self._checkpoint.cleanup()
                return {
                    "success": False,
                    "content": msg,
                    "rounds": round_display,
                    "tool_calls": tool_calls_log,
                }
            except Exception as e:
                logger.error(f"第 {round_display} 轮 LLM 调用失败: {e}")
                findings.append(f"❌ LLM 调用出错: {e}")
                llm_errors += 1
                self.working_memory.record_error(
                    "llm", {}, str(e), round_display
                )
                if llm_errors >= MAX_LLM_ERRORS:
                    break
                continue

            # ── Token 预算检查 ──
            usage = response.get("usage", {})
            total_tokens = usage.get("total_tokens", 0)
            if total_tokens:
                self._total_tokens_used = total_tokens
                self._last_llm_usage = usage

                if total_tokens > MAX_CONTEXT_TOKENS * TOKEN_SAFETY_MARGIN:
                    logger.warning(
                        f"⏱️ Token 水位 {total_tokens}/{MAX_CONTEXT_TOKENS} "
                        f"({total_tokens/MAX_CONTEXT_TOKENS:.0%})，开始压缩"
                    )
                    compressed = await self._compress_messages(messages, round_display)
                    if compressed:
                        self._has_compressed = True
                        if on_event:
                            await on_event("compress", {
                                "before": total_tokens,
                                "after": self._estimate_current_tokens(messages),
                                "round": round_display,
                            })

            # ── 解析 LLM 响应 ──
            choice = response.get("choices", [{}])[0]
            msg = choice.get("message", {})
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls", [])

            # 追加助手回复
            assistant_msg: Dict = {"role": "assistant", "content": content or ""}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            # ── 无工具调用 → 让 LLM 生成最终总结后结束 ──
            if content and not tool_calls:
                logger.info(f"✅ Agent 第 {round_display} 轮完成，请求最终总结")

                messages.append({
                    "role": "system",
                    "content": "任务完成。请给出清晰的最终总结。",
                })

                # ⭐ 再跑一轮 LLM 让总结系统消息被消费
                try:
                    final_resp = await self.llm_client.chat_completion(
                        messages=messages,
                        temperature=0.3,
                        max_tokens=2048,
                    )
                    final_msg = final_resp.get("choices", [{}])[0].get("message", {})
                    final_content = final_msg.get("content", content)
                    # 检查总结轮是否产生了工具调用
                    if final_msg.get("tool_calls"):
                        logger.info("总结轮仍有工具调用，使用原始内容")
                        final_content = content
                    else:
                        content = final_content
                except Exception as e:
                    logger.warning(f"最终总结 LLM 调用失败: {e}，使用原始内容")

                if on_event:
                    await on_event("done", {
                        "rounds": round_display,
                        "tool_calls": len(tool_calls_log),
                        "content": content,
                    })

                # 清理检查点
                if self._checkpoint:
                    self._checkpoint.cleanup()

                return {
                    "success": True,
                    "content": content,
                    "rounds": round_display,
                    "tool_calls": tool_calls_log,
                }

            # ── 处理工具调用 ──
            if tool_calls:
                for tc in tool_calls:
                    tool_name = tc.get("function", {}).get("name", "")
                    tool_args_str = tc.get("function", {}).get("arguments", "{}")
                    try:
                        tool_args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                    except json.JSONDecodeError:
                        tool_args = {"raw": tool_args_str}

                    # 空参数拦截：LLM 调了工具但没传参数 → 跳过
                    meaningful = any(
                        v is not None and v != ""
                        for k, v in tool_args.items()
                        if k not in ("raw",)  # raw 不算有效参数
                    )
                    if not meaningful:
                        logger.warning(f"⚠️ [{tool_name}] 空参数调用，跳过")
                        tool_result_msg = {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": f"错误: 调用 {tool_name} 但未提供任何参数。请指定操作和所需参数。",
                        }
                        messages.append(tool_result_msg)
                        tool_calls_log.append({
                            "tool": tool_name, "args": {},
                            "round": round_display, "error": "空参数"
                        })
                        continue

                    log_entry = {
                        "tool": tool_name,
                        "args": tool_args,
                        "round": round_display,
                        "error": None,
                    }

                    if on_event:
                        await on_event("tool_call", log_entry)

                    # 执行工具
                    try:
                        result = await self._execute_tool(tool_name, tool_args, tc.get("id", ""))
                        is_success = (
                            hasattr(result, 'status')
                            and getattr(result, 'status', None) is not None
                            and getattr(result.status, 'value', "OK") != "ERROR"
                        )

                        # 格式化结果文本
                        if hasattr(result, 'content'):
                            raw = result.content
                            if isinstance(raw, dict):
                                parts = []
                                for k, v in raw.items():
                                    if isinstance(v, str) and len(v) > 100:
                                        parts.append(f"--- {k} ---\n{v}")
                                    else:
                                        parts.append(f"{k}: {v}")
                                result_str = "\n\n".join(parts)
                            else:
                                result_str = str(raw)
                        else:
                            result_str = str(result)

                        if not is_success:
                            error_msg = result.error_message if hasattr(result, 'error_message') else "执行失败"
                            result_str = f"错误: {error_msg}"

                        if len(result_str) > MAX_TOOL_RESULT_CHARS:
                            result_str = result_str[:MAX_TOOL_RESULT_CHARS] + "\n... [截断]"

                        # ── 更新工作记忆 ──
                        if is_success:
                            # 成功：清除连续错误计数
                            self.working_memory.clear_errors()

                            # 记录读取操作
                            if is_success and self.tool_registry and self.tool_registry.is_read_tool(tool_name):
                                action = tool_args.get("action", "")
                                if action in ("read", "search", "fetch", "list", "time", "info", "read_pdf", "read_docx", "ocr"):
                                    path = tool_args.get("path", tool_args.get("query", action))
                                    summary = result_str[:100]
                                    self.working_memory.record_read(
                                        f"{tool_name}/{path}", summary
                                    )

                            # 记录写入操作
                            if is_success and self.tool_registry and self.tool_registry.is_write_tool(tool_name):
                                action = tool_args.get("action", "")
                                if action in ("write", "edit", "append"):
                                    path = tool_args.get("path", "")
                                    self.working_memory.record_write(
                                        path or f"{tool_name}/{action}"
                                    )

                            # 记录方案尝试成功
                            self.working_memory.record_approach(
                                f"{tool_name}({json.dumps(tool_args, ensure_ascii=False)[:60]})",
                                "✅ 成功",
                                round_display,
                            )

                            status_text = "执行成功"
                        else:
                            # 失败：记录错误
                            error_msg = result.error_message if hasattr(result, 'error_message') else str(result)
                            self.working_memory.record_error(
                                tool_name, tool_args, error_msg, round_display
                            )
                            self.working_memory.record_approach(
                                f"{tool_name}({json.dumps(tool_args, ensure_ascii=False)[:60]})",
                                f"❌ {error_msg[:80]}",
                                round_display,
                            )
                            status_text = f"失败: {error_msg[:60]}"

                        # 读取操作追加"不要重复读取"提醒
                        if is_success and self.tool_registry and self.tool_registry.is_read_tool(tool_name):
                            result_str += (
                                "\n\n[已获取以上数据。不要再次调用相同工具读取相同内容。"
                                "如果数据足够，直接进行下一步处理。]"
                            )

                        tool_result_msg = {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": result_str,
                        }
                        messages.append(tool_result_msg)
                        tool_calls_log.append({**log_entry, "result": result_str[:200]})
                        findings.append(f"{'✅' if is_success else '❌'} {tool_name}: {status_text}")

                        if on_event:
                            await on_event(
                                "tool_result" if is_success else "tool_error",
                                {**log_entry, "result": result_str[:300]}
                            )

                    except Exception as e:
                        error_str = str(e)
                        logger.error(f"❌ 工具 {tool_name} 执行异常: {error_str}")
                        self.working_memory.record_error(
                            tool_name, tool_args, error_str, round_display
                        )
                        tool_result_msg = {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": f"错误: {error_str}",
                        }
                        messages.append(tool_result_msg)
                        tool_calls_log.append({**log_entry, "error": error_str})
                        findings.append(f"❌ {tool_name}: {error_str}")
                        tool_errors += 1

                        if on_event:
                            await on_event("tool_error", {**log_entry, "error": error_str})

                        if tool_errors >= MAX_TOOL_ERRORS:
                            break

                # ── 自动 lint ──
                if self.auto_lint and self.edited_files:
                    await self._auto_lint_check(findings, round_display, on_event)

                # ── 死循环检测 ──
                if self._detect_loop(tool_calls_log):
                    recent = tool_calls_log[-5:]
                    logger.warning(
                        f"🚨 死循环检测 | "
                        f"最近5个: {[t.get('tool', '?') for t in recent]}"
                    )
                    already_warned = any(
                        "检测到重复执行" in m.get("content", "")
                        for m in messages[-3:]
                    )
                    if not already_warned:
                        self._safe_inject_system(messages,
                            "你已经多次调用相同工具读取相同数据。"
                            "请基于已获取的信息直接进行处理，不要再重复读取。"
                            "如果工具返回空，换一种方式（如写入文件再读取），"
                            "或者直接跳过该步骤继续后续工作。"
                        )
                    else:
                        self._safe_inject_system(messages,
                            "停止重复操作。立即基于已有信息输出最终结果。"
                        )

                # ── 检查点保存 ──
                if (self._checkpoint
                    and round_display > 0
                    and round_display % CHECKPOINT_INTERVAL == 0):
                    self._checkpoint.save(
                        round_idx=round_display,
                        messages=messages,
                        tool_calls_log=tool_calls_log,
                        findings=findings,
                        working_memory=self.working_memory,
                        edited_files=list(self.edited_files),
                    )
                    if on_event:
                        await on_event("checkpoint", {
                            "type": "saved",
                            "round": round_display,
                        })

            # 超出错误上限（分离 LLM 和工具错误）
            if llm_errors >= MAX_LLM_ERRORS:
                logger.error("❌ LLM 错误次数过多，终止执行")
                break
            if tool_errors >= MAX_TOOL_ERRORS:
                logger.error("❌ 工具错误次数过多，终止执行")
                break

        # ── 达到最大轮次，强制结束 ──
        elapsed = time.time() - start_time
        logger.info(f"⏰ 达到最大轮次 {self.max_rounds}，强制结束 ({elapsed:.1f}s)")
        messages.append({
            "role": "system",
            "content": "已达到最大执行轮次。请给出当前完成情况的总结。",
        })

        try:
            final_response = await self.llm_client.chat_completion(
                messages=messages,
                temperature=0.3,
                max_tokens=2048,
            )
            final_content = final_response.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception:
            final_content = "执行达到最大轮次，无法获取总结。"

        if on_event:
            await on_event("done", {
                "rounds": self.max_rounds,
                "tool_calls": len(tool_calls_log),
                "content": final_content,
            })

        # 清理检查点
        if self._checkpoint:
            self._checkpoint.cleanup()

        return {
            "success": True,
            "content": final_content,
            "rounds": self.max_rounds,
            "tool_calls": tool_calls_log,
        }

    # ── 消息构建 ──

    async def _build_messages(
        self,
        task: str,
        working_dir: str,
        history: Optional[List[Dict]],
    ) -> List[Dict]:
        """构建初始消息列表，基础模板 + skill/user/memory + 历史对话（LLM压缩）"""
        messages: List[Dict] = []

        # 构建变量（从 skill / user / memory 抽取）
        variables = self._build_template_variables(task, working_dir, history)

        # 基础模板 + 外部传入的额外提示（如 router 的 skill 提示词）
        base = self._BASE_TEMPLATE
        if self.base_system:
            base = base + "\n\n" + self.base_system

        system_prompt = self._render_template(base, variables)
        messages.append({"role": "system", "content": system_prompt})

        # ⭐ 注入历史对话 — 老对话 LLM 摘要 + 最近 N 轮完整保留
        if history:
            # 1. 找到所有 user 消息的索引（标记对话轮次边界）
            user_indices = [
                i for i, m in enumerate(history)
                if m.get("role") == "user"
            ]

            # 2. 跳过最后一条 user（即当前输入，已被下方 "任务: {task}" 替代）
            user_indices = user_indices[:-1] if user_indices else []

            if user_indices:
                # 3. 拆分：最近 KEEP_RECENT_TURNS 轮 vs 更老的
                recent_start = user_indices[-KEEP_RECENT_TURNS] if len(user_indices) >= KEEP_RECENT_TURNS else user_indices[0]

                old_history = history[:recent_start]   # 老对话
                recent_history = history[recent_start:]  # 最近 N 轮

                # 4. 老对话 → LLM 压缩摘要（规则摘要作为 fallback）
                if old_history:
                    summary = await self._llm_summarize(old_history, context="早期对话")
                    if summary:
                        old_turn_count = len(user_indices) - min(len(user_indices), KEEP_RECENT_TURNS)
                        messages.append({
                            "role": "system",
                            "content": f"## 📜 历史对话摘要（第1~{old_turn_count}轮）\n{summary}",
                        })

                # 5. 最近 N 轮 → 完整消息（工具结果截短）
                messages.extend(_trim_history_messages(recent_history))

            else:
                # 只有当前输入，没有历史对话
                pass

        messages.append({
            "role": "user",
            "content": (
                f"任务: {task}\n\n"
                f"分析当前情况，决定下一步行动。\n"
                f"如果需要更多信息，调用工具获取。\n"
                f"如果任务完成或无法继续，直接输出最终答案。"
            ),
        })

        return messages

    def _build_template_variables(
        self,
        task: str,
        working_dir: str,
        history: Optional[List[Dict]],
    ) -> Dict[str, str]:
        """从 skill / user / memory 抽取数据填充模板"""
        variables = {
            "task": task,
            "working_dir": working_dir,
            "env_info": self._gather_environment(working_dir),
            "user_profile": self._read_user_profile(working_dir),
            "available_tools": self._summarize_tools(),
            "skill_prompt": "",
            "constraints": "",
        }

        if self.skill:
            if hasattr(self.skill, 'get_config_value'):
                variables["constraints"] = self.skill.get_config_value("constraints", "")
            sp = self.skill.get_system_prompt()
            if sp:
                variables["skill_prompt"] = f"## 当前任务领域：{self.skill.meta.display_name}\n{sp}"

        return variables

    def _render_template(self, template: str, variables: Dict[str, str]) -> str:
        """替换 {{ variable }} 占位符，递归处理变量值中的模板"""
        # 先预渲染变量值（变量值本身可能含模板）
        rendered_vars = {}
        for k, v in variables.items():
            if isinstance(v, str) and "{{" in v:
                # 递归渲染变量值中的模板
                rendered_vars[k] = self._TEMPLATE_PATTERN.sub(
                    lambda m: variables.get(m.group(1), f"{{{{ {m.group(1)} }}}}"),
                    v
                )
            else:
                rendered_vars[k] = v

        def replacer(match):
            var_name = match.group(1)
            return rendered_vars.get(var_name, f"{{{{ {var_name} }}}}")
        return self._TEMPLATE_PATTERN.sub(replacer, template)

    def _summarize_history(self, history: List[Dict]) -> str:
        """生成历史对话摘要 — 按轮次提取 Q&A + 工具结果关键数据"""
        if not history:
            return ""

        # 按 user 消息分割为轮次
        turns: List[List[Dict]] = []
        current_turn: List[Dict] = []
        for m in history:
            if m.get("role") == "user" and current_turn:
                turns.append(current_turn)
                current_turn = []
            current_turn.append(m)
        if current_turn:
            turns.append(current_turn)

        if not turns:
            return ""

        lines = []
        for ti, turn in enumerate(turns, 1):
            user_text = ""
            assistant_text = ""
            tool_names = []
            tool_highlights: List[str] = []

            for m in turn:
                role = m.get("role", "")
                if role == "user":
                    user_text = str(m.get("content", ""))[:150].replace("\n", " ")
                elif role == "assistant":
                    content = str(m.get("content", ""))
                    if content:
                        assistant_text = content[:200].replace("\n", " ")
                    tcs = m.get("tool_calls", [])
                    for tc in tcs:
                        fn = tc.get("function", {})
                        name = fn.get("name", "?")
                        tool_names.append(name)
                elif role == "tool":
                    content = str(m.get("content", ""))
                    tool_highlights.extend(self._extract_highlights(content))

            # 构建轮次摘要行
            if tool_names:
                names_str = " | ".join(tool_names[:3])
                if len(tool_names) > 3:
                    names_str += f" +{len(tool_names)-3}"
                indicator = f"[{names_str}]"
            else:
                indicator = "[直接回答]"

            line = f"第{ti}轮 {indicator}\n  Q: {user_text or '(系统消息)'}"
            if assistant_text:
                line += f"\n  A: {assistant_text}"
            if tool_highlights:
                line += f"\n  数据: {'; '.join(tool_highlights[:5])}"
            lines.append(line)

        return "\n".join(lines)

    @staticmethod
    def _extract_highlights(text: str) -> List[str]:
        """从工具返回文本中提取关键数据"""
        highlights = []
        if not text:
            return highlights

        text = text[:2000]
        patterns = [
            (r'"count"\s*:\s*(\d+)', "count"),
            (r'"total_lines"\s*:\s*(\d+)', "lines"),
            (r'"matches"\s*:\s*(\d+)', "matches"),
            (r'"score"\s*:\s*(\d+)', "score"),
            (r'"files"\s*:\s*(\d+)', "files"),
            (r'"status"\s*:\s*"([^"]+)"', "status"),
            (r'"passed"\s*:\s*(true|false)', "passed"),
            (r'"keyword"\s*:\s*"([^"]+)"', "keyword"),
        ]

        seen = set()
        for pattern, label in patterns:
            for m in re.findall(pattern, text, re.IGNORECASE)[:2]:
                h = f"{label}={m}"
                if h not in seen:
                    seen.add(h)
                    highlights.append(h)

        if not highlights and len(text) > 20:
            clean = text.strip("{} \n\r\t")
            first = clean.split("\n")[0].strip()[:100]
            if first:
                highlights.append(first)

        return highlights[:5]

    async def _llm_summarize(self, messages: List[Dict], context: str = "") -> str:
        """
        用 LLM 压缩消息为结构化摘要，保留关键数据。

        小批量消息（<5条或<500字符）直接用规则摘要，节省 token。
        大宗消息调 LLM，失败时 fallback 到 _summarize_history。
        """
        if not messages or len(messages) < 3:
            return ""

        # 小批量：规则摘要更快
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        if len(messages) <= 5 and total_chars < 500:
            return self._summarize_history(messages)

        # 构建 LLM prompt
        dialogue_parts = []
        for m in messages:
            role = m.get("role", "?")
            content = str(m.get("content", ""))[:400]
            if m.get("tool_calls"):
                tools = [tc.get("function", {}).get("name", "?") for tc in m.get("tool_calls", [])]
                content += f"【调用了: {', '.join(tools)}】"
            dialogue_parts.append(f"[{role}] {content}")

        prompt = (
            "将以下Agent对话记录压缩为结构化摘要，保留所有关键信息：\n\n"
            "## 压缩要求\n"
            "1. 保留所有具体数据：文件路径、数值、配置、命令结果、错误信息\n"
            "2. 保留用户意图和Agent做出的决策\n"
            "3. 保留工具调用的关键返回（如查询结果、文件内容要点、搜索命中数）\n"
            "4. 丢弃过程细节：重试、调试信息、重复操作、冗长的工具输出\n"
            "5. 如果在做代码分析/审计，保留分析结论和关键发现\n"
            "6. 用中文输出，简洁但完整，100-300字\n\n"
            f"## 对话记录（{context}）\n"
            + "\n".join(dialogue_parts)
            + "\n\n## 摘要:"
        )

        try:
            resp = await self.llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
                temperature=0.2,
            )
            result = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if result and len(result) > 10:
                logger.info(f"🤖 LLM 压缩完成: {len(messages)}条 → {len(result)}字摘要")
                return result
        except Exception as e:
            logger.warning(f"LLM 压缩失败，fallback 规则摘要: {e}")

        return self._summarize_history(messages)

    @staticmethod
    def _safe_inject_system(messages: List[Dict], content: str) -> None:
        """
        安全注入 system 消息，不破坏 OpenAI API 消息格式。

        规则: assistant(tool_calls) 后必须紧跟 tool 消息。
        如果 messages[-1] 是 assistant(tool_calls)，插在它之前；
        否则直接追加到末尾。
        """
        if (messages and messages[-1].get("role") == "assistant"
                and messages[-1].get("tool_calls")):
            # 不能插在 assistant(tool_calls) 和 tool 之间
            # 插在最后一个 assistant(tool_calls) 之前
            insert_at = len(messages) - 1
            messages.insert(insert_at, {"role": "system", "content": content})
        else:
            messages.append({"role": "system", "content": content})

    def _summarize_tools(self) -> str:
        """生成可用工具摘要"""
        if not self.tool_registry or not hasattr(self.tool_registry, 'list_tools'):
            return ""
        names = self.tool_registry.list_tools()
        return ", ".join(names[:20]) if names else ""

    @staticmethod
    def _read_user_profile(working_dir: str) -> str:
        """读取身份文件（USER.md / SOUL.md），注入系统提示"""
        profile_specs = [
            ("USER.md", "## 用户档案"),
            ("SOUL.md", "## 行为风格"),
        ]
        parts = []
        for fname, label in profile_specs:
            path = os.path.join(working_dir, fname)
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    if content:
                        parts.append(f"{label}（{fname}）\n{content[:1500]}")
                except Exception:
                    pass
        return "\n\n".join(parts) if parts else ""

    # ── Token 预算管理 ──

    async def _compress_messages(self, messages: List[Dict], current_round: int) -> bool:
        """
        压缩消息列表：删除最旧的完整轮次。

        删除策略：
        - 保留 system prompt + 第一条 user 消息
        - 从第 2 轮开始删除最旧的 COMPRESS_ROUNDS 轮
        - 每轮保持完整（tool_call ↔ tool_result 不拆散）
        - 删除后清理残留的孤儿 tool_call_id

        Returns:
            True 如果压缩成功
        """
        if len(messages) < 6:
            return False

        # 找到 system prompt 结束位置
        system_end = 1  # 第0条是system，第1条是user
        for i, m in enumerate(messages):
            if m.get("role") == "user" and i > 0:
                system_end = i + 1
                break

        # 从 system 之后的消息中，找到最旧的完整轮次
        to_drop = []
        assistant_indices = [
            i for i in range(system_end, len(messages))
            if messages[i].get("role") == "assistant"
        ]

        # 删除最旧的 COMPRESS_ROUNDS 个 assistant 以及它们对应的 tool 消息
        dropped = 0
        for idx in assistant_indices[:COMPRESS_ROUNDS * 3]:  # 每个assistant配多个tool
            if dropped >= COMPRESS_ROUNDS * 4:  # 最多删 4 组
                break
            to_drop.append(idx)
            dropped += 1

            # 也要删掉这个 assistant 前面的/后面的 tool 消息
            # assistant 消息里的 tool_calls 会跟着被删
            # 它后面的 tool 消息（role=tool）也必须删
            for j in range(idx + 1, len(messages)):
                if messages[j].get("role") == "tool":
                    to_drop.append(j)
                else:
                    break

        if not to_drop:
            return False

        # ⭐ 生成被删消息的 LLM 压缩摘要
        dropped_msgs = [messages[idx] for idx in sorted(set(to_drop))]
        summary = await self._llm_summarize(dropped_msgs, context=f"Token超限压缩(第{current_round}轮)")
        if summary:
            summary = f"## 📜 早期对话摘要（已压缩 {len(dropped_msgs)} 条消息）\n{summary}"

        # 去重 + 降序删除
        to_drop = sorted(set(to_drop), reverse=True)
        for idx in to_drop:
            if idx < len(messages):
                messages.pop(idx)

        # ⭐ 清理孤儿 tool_calls：被删 assistant 的 tool_call_id 可能残留在后续消息中
        valid_call_ids = set()
        for m in messages:
            tcs = m.get("tool_calls", [])
            for tc in tcs:
                valid_call_ids.add(tc.get("id", ""))
        # 删除没有对应 assistant tool_calls 的孤儿 tool 消息
        orphan_indices = []
        for i, m in enumerate(messages):
            if m.get("role") == "tool":
                if m.get("tool_call_id", "") not in valid_call_ids:
                    orphan_indices.append(i)
        for idx in reversed(orphan_indices):
            messages.pop(idx)
            to_drop.append(idx)

        # 注入摘要（system prompt 之后、首条 user 之后）
        if summary and system_end < len(messages):
            messages.insert(system_end, {"role": "system", "content": summary})

        logger.info(
            f"🗜️ Token 压缩: 删除了 {len(to_drop)} 条消息 (约{COMPRESS_ROUNDS}轮), "
            f"剩余 {len(messages)} 条"
        )
        return True

    def _estimate_current_tokens(self, messages: List[Dict]) -> int:
        """粗略估算当前 token 数"""
        try:
            from .core.token_estimator import estimate_message_dict
            total = 0
            for m in messages:
                if isinstance(m, dict):
                    total += estimate_message_dict(m)
            return total
        except Exception:
            return self._total_tokens_used

    # ── 工具执行 ──

    async def _execute_tool(self, tool_name: str, tool_args: Dict, call_id: str = "") -> Any:
        """执行单个工具"""
        tool = self.tool_registry.get(tool_name) if hasattr(self.tool_registry, 'get') else None
        if not tool:
            raise ValueError(f"未知工具: {tool_name}")

        logger.info(f"⚡ [{tool_name}] | 参数: {tool_args}")

        # 记录被编辑的文件
        if tool_name in ("edit", "write_file", "code_editor", "diff_file"):
            file_path = tool_args.get("path", tool_args.get("file_path", ""))
            if file_path:
                self.edited_files.add(file_path)

        start = time.time()
        result = await tool.execute(call_id, **tool_args)
        elapsed = time.time() - start

        logger.info(f"✅ [{tool_name}] 完成 ({elapsed:.1f}s)")
        return result

    # ── 事件 ──

    async def _emit_planning(
        self,
        task: str,
        on_event: Callable[[str, Dict], Awaitable[None]],
    ) -> None:
        """发送预判消息"""
        await on_event("planning", {
            "type": "planning",
            "content": f"分析任务: {task[:100]}...",
        })

    # ── 自动 lint ──

    async def _auto_lint_check(
        self,
        findings: List[str],
        round_idx: int,
        on_event: Optional[Callable] = None,
    ) -> None:
        """自动对编辑过的文件做 lint"""
        for file_path in list(self.edited_files):
            if not os.path.isfile(file_path):
                continue
            try:
                passes = await self.lint_runner.run(file_path)
                if passes:
                    if on_event:
                        await on_event("lint_pass", {"file": file_path})
                else:
                    msg = f"⚠️ Lint 提醒: {file_path} 有代码风格问题"
                    findings.append(msg)
                    if on_event:
                        await on_event("lint_error", {"file": file_path})
            except Exception as e:
                logger.debug(f"Lint 跳过 {file_path}: {e}")

    # ── 环境信息 ──

    @staticmethod
    def _gather_environment(working_dir: str) -> str:
        """收集环境信息（含项目上下文）"""
        parts = []
        try:
            files = os.listdir(working_dir)[:50]
            parts.append(f"- 工作目录: {working_dir}")
            parts.append(f"- 目录文件: {', '.join(files[:30])}")
        except Exception:
            parts.append(f"- 工作目录: {working_dir}")

        # ── 项目上下文检测 ──
        project_info = AgentLoop._detect_project_context(working_dir)
        if project_info:
            parts.append(f"- 项目类型: {project_info.get('type', '?')}")
            deps = project_info.get('dependencies', [])
            if deps:
                parts.append(f"- 依赖: {', '.join(deps[:15])}")
            if project_info.get('framework'):
                parts.append(f"- 框架: {project_info['framework']}")
            if project_info.get('python_version'):
                parts.append(f"- Python: {project_info['python_version']}")

        # ── 目录结构关键入口检测 ──
        try:
            key_dirs = []
            for d in ['src', 'tests', 'lib', 'app', 'utils', 'scripts', 'config']:
                if os.path.isdir(os.path.join(working_dir, d)):
                    key_dirs.append(d)
            if key_dirs:
                parts.append(f"- 关键目录: {', '.join(key_dirs)}")
        except Exception:
            pass

        # ── 项目规则文件（.jarvis-rules.md / .jarvis-rules.yaml） ──
        for rule_file in [".jarvis-rules.md", ".jarvis-rules.yaml", ".jarvis-rules.yml", "JARVIS_RULES.md"]:
            rule_path = os.path.join(working_dir, rule_file)
            if os.path.isfile(rule_path):
                try:
                    with open(rule_path, "r", encoding="utf-8") as f:
                        rules_content = f.read().strip()
                    if len(rules_content) > 10:
                        parts.append(f"\n### 项目规则（{rule_file}）\n{rules_content[:2000]}")
                        logger.info(f"📋 读取项目规则: {rule_path} ({len(rules_content)} 字符)")
                    break
                except Exception as e:
                    logger.debug(f"读取规则文件失败 {rule_file}: {e}")

        # ── Git 信息 ──
        try:
            r = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=working_dir, capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0 and r.stdout.strip():
                parts.append(f"- Git 分支: {r.stdout.strip()}")

            r2 = subprocess.run(
                ["git", "status", "--short"],
                cwd=working_dir, capture_output=True, text=True, timeout=5
            )
            if r2.returncode == 0 and r2.stdout.strip():
                lines = r2.stdout.strip().split("\n")[:10]
                parts.append(f"- Git 变更: {', '.join(l.strip() for l in lines)}")
        except Exception:
            pass

        return "## 工作环境\n" + "\n".join(parts)

    @staticmethod
    def _detect_project_context(working_dir: str) -> dict:
        """检测项目上下文：类型、依赖、框架"""
        info = {}

        # Python: requirements.txt
        for req_file in ['requirements.txt', 'requirements-dev.txt']:
            req_path = os.path.join(working_dir, req_file)
            if os.path.isfile(req_path):
                try:
                    with open(req_path, 'r') as f:
                        deps = [l.strip() for l in f if l.strip() and not l.startswith('#') and not l.startswith('-')]
                    if deps:
                        info['type'] = 'Python'
                        info['dependencies'] = [d.split('==')[0].split('>=')[0].split('<')[0].strip() for d in deps[:20]]
                    break
                except Exception:
                    pass

        # Python: pyproject.toml
        pp_path = os.path.join(working_dir, 'pyproject.toml')
        if os.path.isfile(pp_path):
            info['type'] = 'Python'
            try:
                import tomllib
                with open(pp_path, 'rb') as f:
                    pp = tomllib.load(f)
                # 提取依赖
                deps = []
                for section in ['dependencies', 'optional-dependencies']:
                    if section in pp.get('project', {}):
                        raw = pp['project'][section]
                        if isinstance(raw, list):
                            deps.extend([d.split('>=')[0].split('==')[0].split('!=')[0].strip() for d in raw if isinstance(d, str)])
                if deps:
                    info['dependencies'] = deps[:15]
                # Python version
                req_py = pp.get('project', {}).get('requires-python', '')
                if req_py:
                    info['python_version'] = req_py
                # Framework
                if deps:
                    frameworks = ['flask', 'django', 'fastapi', 'pandas', 'numpy', 'requests', 'sqlalchemy', 'click', 'typer', 'httpx', 'scrapy']
                    found = [f for f in frameworks if any(f in d.lower() for d in deps)]
                    if found:
                        info['framework'] = found[0]
            except Exception:
                pass

        # Node: package.json
        pkg_path = os.path.join(working_dir, 'package.json')
        if os.path.isfile(pkg_path):
            info['type'] = 'Node.js'
            try:
                with open(pkg_path, 'r') as f:
                    pkg = json.load(f)
                # 提取所有依赖
                all_deps = {}
                for key in ['dependencies', 'devDependencies']:
                    all_deps.update(pkg.get(key, {}))
                if all_deps:
                    info['dependencies'] = list(all_deps.keys())[:15]
            except Exception:
                pass

        return info

    # ── 死循环检测 ──

    @staticmethod
    def _detect_loop(tool_calls_log: List[Dict]) -> bool:
        """检测工具调用的重复模式"""
        if len(tool_calls_log) < 8:
            return False

        recent = tool_calls_log[-8:]
        names = [t.get("tool", "") for t in recent]
        recent_errors = [t.get("error") for t in recent]

        # 1. 最近 4 次全部失败
        if len(recent_errors) >= 4 and all(e is not None for e in recent_errors[-4:]):
            return True

        # 2. 完全相同的工具+参数重复 5 次
        signatures = []
        for t in recent:
            sig = (t.get("tool", ""), json.dumps(t.get("args", {}), sort_keys=True))
            signatures.append(sig)
        if len(set(signatures[-5:])) == 1:
            return True

        # 3. 交替模式: 短周期 + 至少3个完整周期
        if len(names) >= 6:
            for cycle_len in (2, 3):  # 只检测 2/3 周期（4周期需要12样本但只有8）
                needed = cycle_len * 3
                if len(names) >= needed:  # 确保有足够样本
                    cycle = names[:cycle_len]
                    expected = cycle * 3
                    if expected == names[:needed] and len(set(cycle)) >= 2:
                        return True

        return False
