"""
tool/policy.py — 工具权限策略

定义工具的访问控制：
- ALLOW: 无条件允许
- DENY: 无条件拒绝
- REQUIRE_APPROVAL: 需要人类审批

使用方式：
    policy = ToolPolicy()
    policy.set("shell_execute", AccessLevel.REQUIRE_APPROVAL)
    policy.set("file_write", AccessLevel.REQUIRE_APPROVAL)

    executor = ToolExecutor(registry, policy=policy)
"""

from dataclasses import dataclass, field
from typing import Dict, Optional
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class AccessLevel(Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


DEFAULT_POLICY: Dict[str, AccessLevel] = {
    # Shell — 默认禁止，需显式授权
    "shell_execute": AccessLevel.DENY,
    "shell_run": AccessLevel.REQUIRE_APPROVAL,

    # 文件写操作 — 默认需审批
    "file_write": AccessLevel.REQUIRE_APPROVAL,
    "file_append": AccessLevel.REQUIRE_APPROVAL,

    # 代码编辑 — 默认需审批（小修改动态放行）
    "code_write": AccessLevel.REQUIRE_APPROVAL,
    "code_create": AccessLevel.REQUIRE_APPROVAL,
    "code_append": AccessLevel.REQUIRE_APPROVAL,

    # 进程管理 — 默认禁止
    "process_list": AccessLevel.DENY,

    # Git 写操作 — 默认需审批
    "git_commit": AccessLevel.REQUIRE_APPROVAL,
    "git_push": AccessLevel.REQUIRE_APPROVAL,

    # 其余工具默认 allow
}


@dataclass
class ToolPolicy:
    """
    工具权限策略

    支持规则的动态添加和查询。
    approve() 和 deny() 用于在运行时临时覆盖（如人类确认后）。
    """

    _rules: Dict[str, AccessLevel] = field(default_factory=lambda: dict(DEFAULT_POLICY))
    _pending_approvals: Dict[str, str] = field(default_factory=dict)  # tool_name -> call_id

    def get(self, tool_name: str) -> AccessLevel:
        """获取工具的访问级别"""
        return self._rules.get(tool_name, AccessLevel.ALLOW)

    def set(self, tool_name: str, level: AccessLevel) -> None:
        """设置工具的访问级别"""
        self._rules[tool_name] = level
        logger.info(f"策略: {tool_name} -> {level.value}")

    def check(self, tool_name: str, call_id: str = "") -> AccessLevel:
        """
        检查工具是否允许执行

        Returns:
            AccessLevel: ALLOW / DENY / REQUIRE_APPROVAL
        """
        level = self.get(tool_name)

        if level == AccessLevel.DENY:
            logger.warning(f"🔒 工具被禁止: {tool_name}")
        elif level == AccessLevel.REQUIRE_APPROVAL:
            self._pending_approvals[call_id] = tool_name
            logger.info(f"📝 需要审批: {tool_name} (call_id={call_id[:12]})")

        return level

    def check_dynamic(self, tool_name: str, tool_args: Dict, call_id: str = "") -> AccessLevel:
        """
        带参数感知的动态检查。

        某些工具在小修改时自动放行（如 code_write ≤50 行）。
        """
        # code_write/code_append/code_create: 小修改自动放行
        if tool_name in ("code_write", "code_append", "code_create"):
            new_text = tool_args.get("new_text", "")
            if isinstance(new_text, str):
                line_count = len(new_text.splitlines())
                if line_count <= 50:
                    return AccessLevel.ALLOW

        return self.check(tool_name, call_id)

    def approve(self, call_id: str) -> bool:
        """批准一个待审批的调用"""
        if call_id in self._pending_approvals:
            tool_name = self._pending_approvals.pop(call_id)
            logger.info(f"✅ 已批准: {tool_name}")
            return True
        return False

    def deny(self, call_id: str) -> bool:
        """拒绝一个待审批的调用"""
        if call_id in self._pending_approvals:
            tool_name = self._pending_approvals.pop(call_id)
            logger.info(f"❌ 已拒绝: {tool_name}")
            return True
        return False

    def reset(self) -> None:
        """重置为默认策略"""
        self._rules = dict(DEFAULT_POLICY)
        self._pending_approvals.clear()
        logger.info("策略已重置为默认值")
