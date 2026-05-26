"""
core/types.py — 核心类型定义

被所有模块依赖的基础类型。
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


class ToolResultStatus(str, Enum):
    """工具执行结果状态"""
    SUCCESS = "success"
    ERROR = "error"
    RETRY = "retry"


@dataclass
class ToolResult:
    """工具执行结果"""
    call_id: str
    tool_name: str
    status: ToolResultStatus
    content: Any
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, call_id: str, tool_name: str, content: Any, **metadata) -> "ToolResult":
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            status=ToolResultStatus.SUCCESS,
            content=content,
            metadata=metadata,
        )

    @classmethod
    def error(cls, call_id: str, tool_name: str, error_message: str, **metadata) -> "ToolResult":
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            status=ToolResultStatus.ERROR,
            content=None,
            error_message=error_message,
            metadata=metadata,
        )

    @classmethod
    def retry(cls, call_id: str, tool_name: str, error_message: str, **metadata) -> "ToolResult":
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            status=ToolResultStatus.RETRY,
            content=None,
            error_message=error_message,
            metadata=metadata,
        )

    def is_success(self) -> bool:
        return self.status == ToolResultStatus.SUCCESS

    def is_error(self) -> bool:
        return self.status == ToolResultStatus.ERROR

    def is_retry(self) -> bool:
        return self.status == ToolResultStatus.RETRY


@dataclass
class Message:
    """消息实体"""
    role: Role
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    def to_dict(self) -> Dict:
        """转换为字典（OpenAI 兼容格式）"""
        result = {"role": self.role.value}
        if self.content is not None:
            result["content"] = self.content
        if self.tool_calls:
            result["tool_calls"] = [
                tc.to_openai_format() if hasattr(tc, 'to_openai_format')
                else tc  # 兼容原始 dict（从序列化恢复或外部传入）
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
            tool_calls=tool_calls,
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
        )

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(cls, content: str, tool_calls: Optional[List] = None) -> "Message":
        """创建助手消息，自动将原始 dict 转换为 ToolCall 对象"""
        if tool_calls:
            converted = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    converted.append(ToolCall.from_openai_format(tc))
                else:
                    converted.append(tc)
            tool_calls = converted
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool(cls, call_id: str, content: str) -> "Message":
        return cls(role=Role.TOOL, tool_call_id=call_id, content=content)

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role=Role.SYSTEM, content=content)
