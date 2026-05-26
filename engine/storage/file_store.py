"""
文件存储实现 - 使用 JSON 文件存储会话
"""

import json
import os
import logging
from typing import Optional, Dict, Any, List
from pathlib import Path

from .store import MessageStore
from ..session.session import Session

logger = logging.getLogger(__name__)


class FileMessageStore(MessageStore):
    """基于文件的消息存储"""

    def __init__(self, storage_dir: str = "./sessions"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # 索引文件
        self.index_file = self.storage_dir / "index.json"
        self._load_index()

    def _load_index(self) -> None:
        """加载索引"""
        self._index: Dict[str, Dict] = {}
        if self.index_file.exists():
            try:
                with open(self.index_file, "r") as f:
                    self._index = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load index: {e}")
                self._index = {}

    def _save_index(self) -> None:
        """保存索引"""
        try:
            with open(self.index_file, "w") as f:
                json.dump(self._index, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to save index: {e}")

    def _get_session_path(self, session_id: str) -> Path:
        """获取会话文件路径"""
        return self.storage_dir / f"{session_id}.json"

    async def save_session(self, session: Session) -> None:
        """保存会话"""
        session.touch()
        data = session.to_dict()

        file_path = self._get_session_path(session.session_id)
        try:
            with open(file_path, "w") as f:
                json.dump(data, f, indent=2, default=str)

            # 更新索引
            self._index[session.session_id] = {
                "session_id": session.session_id,
                "user_id": session.user_id,
                "title": session.title,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "message_count": len(session.messages),
                "is_archived": session.is_archived,
            }
            self._save_index()

            logger.debug(f"Saved session: {session.session_id}")
        except Exception as e:
            logger.error(f"Failed to save session {session.session_id}: {e}")
            raise

    async def load_session(self, session_id: str) -> Optional[Session]:
        """加载会话"""
        file_path = self._get_session_path(session_id)
        if not file_path.exists():
            return None

        try:
            with open(file_path, "r") as f:
                data = json.load(f)

            session = Session.from_dict(data)
            logger.debug(f"Loaded session: {session_id}")
            return session
        except Exception as e:
            logger.error(f"Failed to load session {session_id}: {e}")
            return None

    async def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        file_path = self._get_session_path(session_id)
        if not file_path.exists():
            return False

        try:
            file_path.unlink()
            if session_id in self._index:
                del self._index[session_id]
                self._save_index()
            logger.info(f"Deleted session: {session_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete session {session_id}: {e}")
            return False

    async def session_exists(self, session_id: str) -> bool:
        """检查会话是否存在"""
        return self._get_session_path(session_id).exists()

    async def list_sessions(
        self,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """列出会话摘要"""
        sessions = list(self._index.values())

        if user_id:
            sessions = [s for s in sessions if s.get("user_id") == user_id]

        # 按更新时间倒序
        sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)

        return sessions
