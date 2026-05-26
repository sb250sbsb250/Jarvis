"""
工具执行器 - 执行工具调用
（懒加载触发点：这里调用 registry.get() 时才实例化）
"""
import asyncio
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

    async def execute_one(self, call: ToolCall, timeout: Optional[float] = None) -> ToolResult:
        """执行单个工具调用（带超时控制 + 权限检查）"""
        # ── 权限检查 ──
        if self.policy:
            access = self.policy.check(call.name, call.id)
            if access == AccessLevel.DENY:
                return ToolResult.error(
                    call.id, call.name,
                    f"工具 '{call.name}' 已被禁止执行"
                )
            elif access == AccessLevel.REQUIRE_APPROVAL:
                # 挂起等待人类审批
                logger.warning(f"⏸️ 工具 '{call.name}' 等待审批 (call_id={call.id[:12]})")
                return ToolResult.error(
                    call.id, call.name,
                    f"工具 '{call.name}' 需要人类审批才能执行"
                )

        tool = self.registry.get(call.name)

        if not tool:
            return ToolResult.error(
                call.id, call.name,
                f"工具 '{call.name}' 未注册"
            )

        is_valid, error_msg = tool.validate_args(call.arguments)
        if not is_valid:
            return ToolResult.error(call.id, call.name, error_msg or "参数无效")

        try:
            timeout_val = timeout if timeout is not None else self._default_timeout
            logger.debug(f"执行工具: {call.name} (超时: {timeout_val}s)")
            result = await asyncio.wait_for(
                tool.execute(call.id, **call.arguments),
                timeout=timeout_val
            )
            return result
        except asyncio.TimeoutError:
            return ToolResult.error(
                call.id, call.name,
                f"工具执行超时 ({timeout_val}s)"
            )
        except Exception as e:
            logger.exception(f"工具 {call.name} 异常: {e}")
            return ToolResult.error(call.id, call.name, str(e))

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
