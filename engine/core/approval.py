"""
engine/core/approval.py — 审批门

Claude Code 风格的工具执行拦截器。
高风险操作（file_write, shell_run, git_push 等）需要审批。

两种模式:
- auto 模式 (默认): 自动放行 + SSE 通知前端
- manual 模式: 暂停执行，等待用户通过 API 确认/拒绝
"""

import asyncio
import logging
from typing import Callable, Dict, Optional, Any

from ..tool.policy import ToolPolicy, AccessLevel

logger = logging.getLogger(__name__)


# ── 高风险工具描述 ──
HIGH_RISK_TOOLS: Dict[str, str] = {
    "file_write": "写入/覆盖文件",
    "file_append": "追加文件内容",
    "shell_run": "执行 Shell 命令",
    "git_push": "推送到远程仓库",
    "git_commit": "提交代码",
    "code_write": "修改代码文件",
    "code_create": "创建代码文件",
    "code_append": "追加代码",
}


class ApprovalGate:
    """
    审批门 — Claude Code 风格的工具执行拦截器

    用法:
        gate = ApprovalGate(policy, auto_approve=True)

        # 工具执行前调用
        approved = await gate.check_and_wait(tool_name, tool_args, call_id, on_event)
        if not approved:
            raise PermissionError(...)
    """

    def __init__(self, policy: ToolPolicy, auto_approve: bool = True):
        self._policy = policy
        self._auto_approve = auto_approve
        # call_id -> asyncio.Event (manual 模式下阻塞等待)
        self._pending: Dict[str, asyncio.Event] = {}
        # call_id -> bool (审批结果)
        self._results: Dict[str, bool] = {}
        # 已超时或显式拒绝的 call_id（防止 approve() 穿透到 policy）
        self._denied: set = set()

    def set_auto_approve(self, enabled: bool) -> None:
        """切换自动审批模式"""
        self._auto_approve = enabled
        logger.info(f"审批门: auto_approve={enabled}")

    def is_auto_approve(self) -> bool:
        return self._auto_approve

    async def check_and_wait(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        call_id: str,
        on_event: Optional[Callable] = None,
    ) -> bool:
        """
        检查工具是否需要审批，必要时等待用户确认。

        Args:
            tool_name: 工具名称
            tool_args: 工具参数
            call_id: 调用 ID
            on_event: SSE 事件回调

        Returns:
            True = 允许执行
            False = 被拒绝
        """
        # 动态检查（考虑参数内容）
        level = self._policy.check_dynamic(tool_name, tool_args, call_id)

        if level == AccessLevel.ALLOW:
            return True

        if level == AccessLevel.DENY:
            logger.warning(f"🔒 工具被禁止: {tool_name}")
            return False

        # REQUIRE_APPROVAL
        risk_desc = HIGH_RISK_TOOLS.get(tool_name, f"执行 {tool_name}")

        if self._auto_approve:
            # 自动模式：放行 + 通知前端
            logger.info(f"🔓 自动审批: {tool_name} ({risk_desc})")
            if on_event:
                await on_event("approval", {
                    "tool": tool_name,
                    "call_id": call_id,
                    "risk_level": "high" if tool_name in ("file_write", "shell_run", "git_push") else "medium",
                    "auto_approved": True,
                    "message": f"已自动审批: {risk_desc}",
                    "args_preview": _args_preview(tool_args),
                })
            return True

        # 手动模式：暂停等待
        logger.info(f"⏸️ 等待审批: {tool_name} ({risk_desc}) call_id={call_id[:12]}")

        event = asyncio.Event()
        self._pending[call_id] = event

        if on_event:
            await on_event("approval", {
                "tool": tool_name,
                "call_id": call_id,
                "risk_level": "high" if tool_name in ("file_write", "shell_run", "git_push") else "medium",
                "auto_approved": False,
                "message": f"等待审批: {risk_desc}",
                "args_preview": _args_preview(tool_args),
            })

        # 等待用户响应（超时 5 分钟）
        try:
            await asyncio.wait_for(event.wait(), timeout=300)
        except asyncio.TimeoutError:
            logger.warning(f"⏰ 审批超时: {tool_name} (call_id={call_id[:12]})")
            self._pending.pop(call_id, None)
            self._denied.add(call_id)  # 标记为已拒绝，防止 approve() 穿透
            return False

        result = self._results.pop(call_id, False)
        self._pending.pop(call_id, None)

        if result:
            logger.info(f"✅ 已批准: {tool_name}")
        else:
            logger.info(f"❌ 已拒绝: {tool_name}")

        return result

    def approve(self, call_id: str) -> bool:
        """批准一个待审批的调用"""
        if call_id in self._denied:
            return False  # 已超时/拒绝，不再穿透到 policy
        if call_id in self._pending:
            self._results[call_id] = True
            self._pending[call_id].set()
            return True
        # 也尝试通过 policy 批准
        return self._policy.approve(call_id)

    def deny(self, call_id: str) -> bool:
        """拒绝一个待审批的调用"""
        if call_id in self._pending:
            self._results[call_id] = False
            self._pending[call_id].set()
            self._denied.add(call_id)
            return True
        return self._policy.deny(call_id)

    def get_pending(self) -> Dict[str, str]:
        """获取所有待审批的 call_id -> tool_name"""
        return {cid: self._policy._pending_approvals.get(cid, "") for cid in self._pending}


def _args_preview(args: Dict[str, Any], max_len: int = 200) -> str:
    """参数预览（用于审批请求展示）"""
    import json
    try:
        s = json.dumps(args, ensure_ascii=False)
        return s[:max_len] + ("..." if len(s) > max_len else "")
    except Exception:
        return str(args)[:max_len]
