"""
会话管理器 — 简化版

管理会话生命周期，用 JSON 文件持久化。
Session 存 List[Dict] + summary，不再依赖 MessageList。
"""

import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
from pathlib import Path

from .session import Session

logger = logging.getLogger(__name__)


class JsonFileStore:
    """基于 JSON 文件的会话存储"""

    def __init__(self, store_dir: str):
        self._dir = Path(store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, sid: str) -> Path:
        return self._dir / f"{sid}.json"

    async def save_session(self, session: Session) -> None:
        path = self._session_path(session.session_id)
        data = session.to_dict()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def load_session(self, sid: str) -> Optional[Session]:
        path = self._session_path(sid)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Session.from_dict(data)
        except Exception as e:
            logger.error(f"加载会话失败 {sid}: {e}")
            return None

    async def delete_session(self, sid: str) -> bool:
        path = self._session_path(sid)
        if path.exists():
            path.unlink()
            return True
        return False

    async def list_sessions(self) -> List[Dict[str, Any]]:
        summaries = []
        for p in sorted(self._dir.glob("*.json"), reverse=True):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                summaries.append({
                    "session_id": data.get("session_id", p.stem),
                    "title": data.get("title", ""),
                    "messages": len(data.get("messages", [])),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                })
            except Exception:
                summaries.append({"session_id": p.stem, "messages": 0})
        return summaries


class SessionManager:
    """会话管理器 — 创建/保存/加载/删除会话"""

    def __init__(self, storage_dir: str = "./sessions"):
        self._store = JsonFileStore(storage_dir)
        self._sessions: Dict[str, Session] = {}

    async def create_session(self, title: str = "") -> Session:
        session = Session(title=title)
        self._sessions[session.session_id] = session
        await self._store.save_session(session)
        logger.info(f"Created session: {session.session_id}")
        return session

    async def get_session(self, session_id: str) -> Optional[Session]:
        if session_id in self._sessions:
            self._sessions[session_id].touch()
            return self._sessions[session_id]

        session = await self._store.load_session(session_id)
        if session:
            self._sessions[session_id] = session
            logger.debug(f"Loaded session: {session_id}")
        return session

    async def get_or_create_session(self, session_id: Optional[str] = None) -> Session:
        if session_id:
            session = await self.get_session(session_id)
            if session:
                return session
        return await self.create_session()

    async def save_session(self, session: Session) -> None:
        session.touch()
        self._sessions[session.session_id] = session
        await self._store.save_session(session)

    async def delete_session(self, session_id: str) -> bool:
        if session_id in self._sessions:
            del self._sessions[session_id]
        return await self._store.delete_session(session_id)

    async def list_sessions(self) -> List[Dict[str, Any]]:
        return await self._store.list_sessions()

    async def close(self) -> None:
        for session in self._sessions.values():
            await self._store.save_session(session)
        logger.info("SessionManager closed")
