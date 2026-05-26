"""
存储抽象接口
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List

from ..session.session import Session


class MessageStore(ABC):
    """消息存储抽象接口"""

    @abstractmethod
    async def save_session(self, session: Session) -> None:
        """保存会话"""
        pass

    @abstractmethod
    async def load_session(self, session_id: str) -> Optional[Session]:
        """加载会话"""
        pass

    @abstractmethod
    async def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        pass

    @abstractmethod
    async def session_exists(self, session_id: str) -> bool:
        """检查会话是否存在"""
        pass

    @abstractmethod
    async def list_sessions(
        self,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """列出会话摘要"""
        pass
