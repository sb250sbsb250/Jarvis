"""
会话实体 — 简化版

用纯 List[Dict] 存消息，不再依赖 MessageList。
存储格式: {"session_id": ..., "messages": [...], "summary": "..."}
"""

from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4


@dataclass
class Session:
    """会话实体 — 长期层存储"""

    # 标识
    session_id: str = field(default_factory=lambda: f"sess_{uuid4().hex[:12]}")

    # 核心数据
    messages: List[Dict] = field(default_factory=list)  # 最近 N 轮完整消息
    summary: str = ""  # 更早对话的 LLM 摘要

    # 元数据
    title: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    # 时间
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    # 状态
    is_active: bool = True
    is_archived: bool = False

    def __len__(self) -> int:
        return len(self.messages)

    def __bool__(self) -> bool:
        return True

    def touch(self) -> None:
        self.updated_at = datetime.now()

    def clear(self) -> None:
        self.messages.clear()
        self.summary = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "messages": self.messages,
            "summary": self.summary,
            "title": self.title,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "is_active": self.is_active,
            "is_archived": self.is_archived,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Session":
        return cls(
            session_id=data.get("session_id", ""),
            messages=data.get("messages", []),
            summary=data.get("summary", ""),
            title=data.get("title", ""),
            metadata=data.get("metadata", {}),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(),
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else datetime.now(),
            is_active=data.get("is_active", True),
            is_archived=data.get("is_archived", False),
        )
