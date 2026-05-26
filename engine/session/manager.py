"""
会话管理器 - 管理多个会话的生命周期
"""

import logging
from typing import Optional, Dict, Any, List
from datetime import datetime

from .session import Session
from ..storage.store import MessageStore
from ..storage.file_store import FileMessageStore

logger = logging.getLogger(__name__)


class SessionManager:
    """
    会话管理器

    负责：
    - 创建/删除会话
    - 保存/加载会话
    - 切换当前会话
    - 管理历史会话
    """

    def __init__(self, store: Optional[MessageStore] = None, storage_dir: str = "./sessions"):
        """
        Args:
            store: 存储实例
            storage_dir: 存储目录（如果使用默认文件存储）
        """
        self._store = store or FileMessageStore(storage_dir)
        self._sessions: Dict[str, Session] = {}
        self._current_session_id: Optional[str] = None

    async def create_session(
        self,
        user_id: Optional[str] = None,
        title: str = "",
        metadata: Optional[Dict] = None,
    ) -> Session:
        """创建新会话"""
        session = Session(
            user_id=user_id,
            title=title,
            metadata=metadata or {},
        )
        self._sessions[session.session_id] = session
        self._current_session_id = session.session_id

        # 保存到存储
        await self._store.save_session(session)

        logger.info(f"Created session: {session.session_id}")
        return session

    async def get_session(self, session_id: str) -> Optional[Session]:
        """获取会话（先从内存，再从存储加载）"""
        # 先从内存获取
        if session_id in self._sessions:
            session = self._sessions[session_id]
            session.touch()
            return session

        # 从存储加载
        session = await self._store.load_session(session_id)
        if session:
            self._sessions[session_id] = session
            logger.debug(f"Loaded session from storage: {session_id}")

        return session

    async def save_session(self, session: Session) -> None:
        """保存会话"""
        session.touch()
        self._sessions[session.session_id] = session
        await self._store.save_session(session)
        logger.debug(f"Saved session: {session.session_id}")

    async def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        if session_id in self._sessions:
            del self._sessions[session_id]

        if self._current_session_id == session_id:
            self._current_session_id = None

        return await self._store.delete_session(session_id)

    async def list_sessions(
        self,
        user_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """列出会话摘要"""
        summaries = await self._store.list_sessions(user_id)

        # 排序：最新的在前
        summaries.sort(key=lambda x: x.get("updated_at", ""), reverse=True)

        return summaries[offset:offset + limit]

    async def get_or_create_session(
        self,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        title: str = "",
    ) -> Session:
        """获取或创建会话"""
        if session_id:
            session = await self.get_session(session_id)
            if session:
                return session

        return await self.create_session(user_id=user_id, title=title)

    def set_current_session(self, session_id: str) -> bool:
        """设置当前会话"""
        if session_id in self._sessions:
            self._current_session_id = session_id
            return True
        return False

    def get_current_session(self) -> Optional[Session]:
        """获取当前会话"""
        if self._current_session_id:
            return self._sessions.get(self._current_session_id)
        return None

    async def rename_session(self, session_id: str, new_title: str) -> bool:
        """重命名会话"""
        session = await self.get_session(session_id)
        if not session:
            return False

        session.title = new_title
        await self.save_session(session)
        return True

    async def archive_session(self, session_id: str) -> bool:
        """归档会话"""
        session = await self.get_session(session_id)
        if not session:
            return False

        session.is_archived = True
        await self.save_session(session)
        return True

    async def unarchive_session(self, session_id: str) -> bool:
        """取消归档"""
        session = await self.get_session(session_id)
        if not session:
            return False

        session.is_archived = False
        await self.save_session(session)
        return True

    async def clear_old_sessions(self, days: int = 30) -> int:
        """清理旧会话"""
        sessions = await self._store.list_sessions()

        deleted = 0
        for summary in sessions:
            updated_at = datetime.fromisoformat(summary.get("updated_at", ""))
            if (datetime.now() - updated_at).days > days:
                if await self._store.delete_session(summary["session_id"]):
                    deleted += 1
                if summary["session_id"] in self._sessions:
                    del self._sessions[summary["session_id"]]

        logger.info(f"Cleared {deleted} old sessions")
        return deleted

    async def close(self) -> None:
        """关闭管理器，保存所有会话"""
        for session in self._sessions.values():
            await self._store.save_session(session)
        logger.info("SessionManager closed")
