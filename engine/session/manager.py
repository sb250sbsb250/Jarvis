"""
会话管理器 - 管理多个会话的生命周期
"""

import json
import logging
import os
from typing import Optional, Dict, Any, List
from datetime import datetime
from pathlib import Path

from .session import Session
from ..storage.store import MessageStore
from ..message.message_list import MessageList

logger = logging.getLogger(__name__)


class JsonFileStore(MessageStore):
    """基于 JSON 文件的会话存储"""

    def __init__(self, store_dir: str):
        self._dir = Path(store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger(__name__)

    def _session_path(self, sid: str) -> Path:
        return self._dir / f"{sid}.json"

    async def save_session(self, session: Session) -> None:
        path = self._session_path(session.session_id)
        data = {
            "session_id": session.session_id,
            "messages": session.messages.to_dict_list()
            if hasattr(session.messages, 'to_dict_list')
            else [{"role": m.role, "content": m.content}
                  for m in getattr(session.messages, '_messages', [])],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def load_session(self, sid: str) -> Optional[Session]:
        path = self._session_path(sid)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            session = Session(session_id=data["session_id"])
            for m in data.get("messages", []):
                role = m["role"]
                if role == "user":
                    session.messages.add_user(m.get("content", ""))
                elif role == "assistant":
                    session.messages.add_assistant(m.get("content", ""))
                elif role == "tool":
                    session.messages.add_tool(
                        call_id=m.get("tool_call_id", ""),
                        content=m.get("content", ""),
                    )
                else:
                    session.messages._messages.append(
                        type(session.messages._messages[0])(
                            role=role,
                            content=m.get("content", ""),
                        )
                    )
            return session
        except Exception as e:
            self._logger.error(f"加载会话失败 {sid}: {e}")
            return None

    async def delete_session(self, sid: str) -> None:
        path = self._session_path(sid)
        if path.exists():
            path.unlink()

    async def list_sessions(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """返回会话摘要列表，与 MessageStore 接口一致"""
        summaries = []
        for p in self._dir.glob("*.json"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                summaries.append({
                    "session_id": data.get("session_id", p.stem),
                    "messages": len(data.get("messages", [])),
                })
            except Exception:
                summaries.append({
                    "session_id": p.stem,
                    "messages": 0,
                })
        return summaries

    async def session_exists(self, sid: str) -> bool:
        return self._session_path(sid).exists()


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
            storage_dir: 存储目录（如果不传 store，自动创建 JSON 文件存储）
        """
        self._store = store or self._create_default_store(storage_dir)
        self._sessions: Dict[str, Session] = {}
        self._current_session_id: Optional[str] = None

    @staticmethod
    def _create_default_store(storage_dir: str) -> "MessageStore":
        """创建默认的 JSON 文件消息存储"""
        return JsonFileStore(storage_dir)

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
