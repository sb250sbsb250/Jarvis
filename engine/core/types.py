"""
core/types.py — 核心类型定义

v3.0: ToolResult 简化，支持新的原子工具架构
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4


class Role(str, Enum):
    """消息角色"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    """工具调用定义"""
    id: str
    name: str
    arguments: Dict[str, Any]

    @classmethod
    def create(cls, name: str, arguments: Dict[str, Any]) -> "ToolCall":
        return cls(id=f"call_{uuid4().hex[:8]}", name=name, arguments=arguments)

    def to_openai_format(self) -> Dict:
        """转换为 OpenAI 格式"""
        import json
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments),
            }
        }

    @classmethod
    def from_openai_format(cls, data: Dict) -> "ToolCall":
        """从 OpenAI 格式解析"""
        import json
        function = data.get("function", {})
        raw_args = function.get("arguments", "{}")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
        else:
            args = raw_args
        return cls(
            id=data.get("id", ""),
            name=function.get("name", ""),
            arguments=args,
        )


@dataclass
class ToolResult:
    """工具执行结果

    v3.0 简化版: success → bool, error → Optional[str]
    保持向后兼容（is_success/is_error 属性）
    """
    call_id: str
    tool_name: str
    success: bool
    content: Any = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, call_id: str, tool_name: str, content: Any, **metadata) -> "ToolResult":
        """创建成功结果"""
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            success=True,
            content=content,
            metadata=metadata,
        )

    @classmethod
    def fail(cls, call_id: str, tool_name: str, error: str, **metadata) -> "ToolResult":
        """创建失败结果"""
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            success=False,
            error=error,
            metadata=metadata,
        )

    # ── 向后兼容 ──
    @property
    def is_success(self) -> bool:
        return self.success

    @property
    def is_error(self) -> bool:
        return not self.success

    @property
    def error_message(self) -> Optional[str]:
        return self.error




@dataclass
class Message:
    """消息实体"""
    role: Role
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    def to_dict(self) -> Dict:
        """转换为字典（OpenAI 兼容格式）"""
        result = {"role": self.role.value}
        if self.content is not None:
            result["content"] = self.content
        if self.reasoning_content:
            result["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            result["tool_calls"] = [
                tc.to_openai_format() if hasattr(tc, 'to_openai_format')
                else tc
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        if self.name:
            result["name"] = self.name
        return result

    @classmethod
    def from_dict(cls, data: Dict) -> "Message":
        """从字典创建"""
        tool_calls = None
        if "tool_calls" in data and data["tool_calls"]:
            tool_calls = [ToolCall.from_openai_format(tc) for tc in data["tool_calls"]]
        return cls(
            role=Role(data.get("role", "user")),
            content=data.get("content"),
            reasoning_content=data.get("reasoning_content"),
            tool_calls=tool_calls,
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
        )

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(cls, content: str, tool_calls: Optional[List] = None, reasoning_content: Optional[str] = None) -> "Message":
        """创建助手消息，自动将原始 dict 转换为 ToolCall 对象"""
        if tool_calls:
            converted = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    converted.append(ToolCall.from_openai_format(tc))
                else:
                    converted.append(tc)
            tool_calls = converted
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls, reasoning_content=reasoning_content)

    @classmethod
    def tool(cls, call_id: str, content: str) -> "Message":
        return cls(role=Role.TOOL, tool_call_id=call_id, content=content)

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role=Role.SYSTEM, content=content)


@dataclass
class ToolCallRecord:
    """
    统一工具调用记录 — Claude Code 风格

    替代 tool_calls_log 中的松散 dict。
    支持格式化为人类可读的单行日志。
    """
    tool: str
    args: Dict[str, Any]
    round: int
    call_id: str = ""
    error: Optional[str] = None
    result: Optional[str] = None
    duration_ms: float = 0.0
    started_at: float = 0.0
    backup_path: Optional[str] = None
    approved: bool = True
    auto_approved: bool = True

    def format_oneline(self) -> str:
        """
        Claude Code 风格的单行格式化输出。

        示例:
          [R3] ✅ code_write(path="app.py") 0.8s
          [R3] ❌ shell_run(command="npm test") Error: timeout 2.1s
        """
        status = "✅" if not self.error else "❌"
        args_parts = []
        for k, v in self.args.items():
            val_str = str(v)[:40]
            args_parts.append(f'{k}="{val_str}"')
        args_str = ", ".join(args_parts)
        time_str = f" {self.duration_ms/1000:.1f}s" if self.duration_ms else ""
        err_str = f" Error: {self.error[:60]}" if self.error else ""
        return f"[R{self.round}] {status} {self.tool}({args_str}){time_str}{err_str}"

    def to_dict(self) -> Dict:
        """转为可序列化的 dict（用于 checkpoint 保存）"""
        d = {
            "tool": self.tool,
            "args": self.args,
            "round": self.round,
            "call_id": self.call_id,
            "error": self.error,
            "result": self.result[:200] if self.result else None,
            "duration_ms": self.duration_ms,
        }
        if self.backup_path:
            d["backup_path"] = self.backup_path
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> "ToolCallRecord":
        """从 dict 恢复"""
        return cls(
            tool=data.get("tool", ""),
            args=data.get("args", {}),
            round=data.get("round", 0),
            call_id=data.get("call_id", ""),
            error=data.get("error"),
            result=data.get("result"),
            duration_ms=data.get("duration_ms", 0.0),
            backup_path=data.get("backup_path"),
        )
