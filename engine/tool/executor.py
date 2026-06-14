"""
engine/tool/executor.py — 工具执行器（懒加载触发点）
"""
import asyncio
import json
import logging
from typing import List, Optional

from ..core.types import ToolCall, ToolResult
from .registry import ToolRegistry
from .policy import ToolPolicy, AccessLevel

logger = logging.getLogger("ToolExecutor")


class ToolExecutor:
    """工具执行器（带超时控制 + 权限策略）"""

    def __init__(self, registry: Optional[ToolRegistry] = None,
                 default_timeout: float = 30.0,
                 policy: Optional[ToolPolicy] = None):
        self.registry = registry or ToolRegistry()
        self._max_parallel = 10
        self._default_timeout = default_timeout
        self.policy = policy

    def set_max_parallel(self, max_parallel: int) -> None:
        self._max_parallel = max_parallel

    async def execute_one(self, call: ToolCall, timeout: Optional[float] = None, max_retries: int = 2) -> ToolResult:
        """执行单个工具调用（带超时控制 + 权限检查 + 自动重试）"""
        # ── 权限检查 ──
        if self.policy:
            access = self.policy.check(call.name, call.id)
            if access == AccessLevel.DENY:
                return ToolResult.fail(
                    call.id, call.name,
                    f"工具 '{call.name}' 已被禁止执行"
                )
            elif access == AccessLevel.REQUIRE_APPROVAL:
                logger.warning(f"⏸️ 工具 '{call.name}' 等待审批 (call_id={call.id[:12]})")
                return ToolResult.fail(
                    call.id, call.name,
                    f"工具 '{call.name}' 需要人类审批才能执行"
                )

        tool = self.registry.get(call.name)
        if not tool:
            return ToolResult.error(call.id, call.name, f"工具 '{call.name}' 未注册")

        is_valid, error_msg = tool.validate_args(call.name, call.arguments)
        if not is_valid:
            return ToolResult.error(call.id, call.name, error_msg or "参数无效")

        # 使用工具级重试配置
        tool_retry_errors = getattr(tool, 'retryable_exceptions', (ConnectionError, TimeoutError, OSError))
        tool_max_retries = getattr(tool, 'max_retries', max_retries)

        last_error = None
        retryable_errors = tool_retry_errors
        timeout_val = timeout if timeout is not None else self._default_timeout

        logger.info(
            f"🔧 执行工具: {call.name} | "
            f"参数: {json.dumps(call.arguments, ensure_ascii=False)[:200]}"
        )

        for attempt in range(tool_max_retries + 1):
            try:
                if attempt > 0:
                    backoff = 2 ** attempt  # 指数退避: 2s, 4s
                    cause = last_error.error_message if last_error and hasattr(last_error, 'error_message') else (str(last_error) if last_error else '超时')
                    logger.warning(f"🔄 工具 '{call.name}' 第 {attempt}/{tool_max_retries} 次重试 (原因: {cause}) (等待 {backoff}s)")
                    await asyncio.sleep(backoff)

                result = await asyncio.wait_for(
                    tool.execute(call.id, **call.arguments),
                    timeout=timeout_val
                )

                # 工具自身返回错误但可重试
                if result.is_success or attempt >= tool_max_retries:
                    return result

                last_error = result
                logger.warning(f"工具 '{call.name}' 返回错误: {result.error_message}, 准备重试")

            except asyncio.TimeoutError:
                last_error = ToolResult.error(call.id, call.name, f"工具执行超时 ({timeout_val}s)")
                if attempt >= tool_max_retries:
                    return last_error
                logger.warning(f"工具 '{call.name}' 超时，准备重试 (attempt {attempt + 1})")

            except retryable_errors as e:
                last_error = ToolResult.error(call.id, call.name, str(e))
                if attempt >= tool_max_retries:
                    return last_error
                logger.warning(f"工具 '{call.name}' 可重试异常: {e}, 准备重试")

            except Exception as e:
                logger.exception(f"工具 {call.name} 异常: {e}")
                return ToolResult.error(call.id, call.name, str(e))

        return last_error or ToolResult.error(call.id, call.name, "执行失败: 未知错误")

    async def execute_parallel(
        self,
        calls: List[ToolCall],
        timeout: Optional[float] = None,
    ) -> List[ToolResult]:
        """并行执行多个工具（带超时）"""
        if not calls:
            return []

        semaphore = asyncio.Semaphore(self._max_parallel)

        async def execute_with_limit(call: ToolCall):
            async with semaphore:
                return await self.execute_one(call, timeout=timeout)

        results = await asyncio.gather(
            *[execute_with_limit(c) for c in calls],
            return_exceptions=True
        )

        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                final_results.append(
                    ToolResult.error(calls[i].id, calls[i].name, str(result))
                )
            else:
                final_results.append(result)
        return final_results
