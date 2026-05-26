"""
会话实体定义（DAG 架构版）

移除了 StateMachine 依赖，改用 DAG 执行上下文。
"""

from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from ..message.message_list import MessageList


@dataclass
class Session:
    """会话实体"""

    # 标识
    session_id: str = field(default_factory=lambda: f"sess_{uuid4().hex[:12]}")

    # 用户标识
    user_id: Optional[str] = None

    # 内容（DAG 版本：消息列表 + 可选检查点）
    messages: MessageList = field(default_factory=MessageList)

    # 检查点（用于 DAG 执行恢复）
    checkpoint: Optional[Dict[str, Any]] = None
    pending_approval: Optional[Dict[str, Any]] = None

    # 元数据
    title: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    # 时间
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    last_accessed_at: datetime = field(default_factory=datetime.now)

    # 状态
    is_active: bool = True
    is_archived: bool = False

    def touch(self) -> None:
        """更新访问时间"""
        self.updated_at = datetime.now()
        self.last_accessed_at = datetime.now()

    def add_message(self, message: Any) -> None:
        """添加消息"""
        self.messages.add(message)
        self.updated_at = datetime.now()

    def add_user_message(self, content: str) -> None:
        """添加用户消息"""
        self.messages.add_user(content)
        self.updated_at = datetime.now()

    def add_assistant_message(self, content: str, tool_calls: Optional[List] = None) -> None:
        """添加助手消息"""
        self.messages.add_assistant(content, tool_calls)
        self.updated_at = datetime.now()

    def add_tool_message(self, call_id: str, content: str) -> None:
        """添加工具消息"""
        self.messages.add_tool(call_id, content)
        self.updated_at = datetime.now()

    def get_summary(self) -> Dict[str, Any]:
        """获取会话摘要"""
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "title": self.title,
            "message_count": len(self.messages),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_accessed_at": self.last_accessed_at.isoformat(),
            "is_active": self.is_active,
            "is_archived": self.is_archived,
            "has_checkpoint": self.checkpoint is not None,
        }

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于持久化）"""
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "messages": [msg.to_dict() for msg in self.messages.get_all()],
            "checkpoint": self.checkpoint,
            "pending_approval": self.pending_approval,
            "title": self.title,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_accessed_at": self.last_accessed_at.isoformat(),
            "is_active": self.is_active,
            "is_archived": self.is_archived,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Session":
        """从字典恢复"""
        from ..core.types import Message

        messages = MessageList()
        for msg_data in data.get("messages", []):
            messages.add(Message.from_dict(msg_data))

        session = cls(
            session_id=data.get("session_id"),
            user_id=data.get("user_id"),
            messages=messages,
            checkpoint=data.get("checkpoint"),
            pending_approval=data.get("pending_approval"),
            title=data.get("title", ""),
            metadata=data.get("metadata", {}),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(),
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else datetime.now(),
            last_accessed_at=datetime.fromisoformat(data["last_accessed_at"]) if data.get("last_accessed_at") else datetime.now(),
            is_active=data.get("is_active", True),
            is_archived=data.get("is_archived", False),
        )

        return session
