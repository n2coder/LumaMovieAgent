from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Dict, List
from uuid import uuid4


GREETING_TEXT = (
    "Hi! I'm Luma, your AI Movie Assistant. What kind of movies are you in the mood for today?"
)


@dataclass
class SessionState:
    session_id: str
    created_at: datetime
    updated_at: datetime
    history: List[dict] = field(default_factory=list)


class ConversationManager:
    def __init__(self, ttl_minutes: int = 60, max_messages: int = 24):
        self._ttl = timedelta(minutes=ttl_minutes)
        self._max_messages = max_messages
        self._sessions: Dict[str, SessionState] = {}
        self._lock = Lock()

    def start_session(self) -> SessionState:
        with self._lock:
            self._cleanup_expired()
            now = datetime.now(timezone.utc)
            session_id = uuid4().hex
            state = SessionState(session_id=session_id, created_at=now, updated_at=now)
            state.history.append({"role": "assistant", "content": GREETING_TEXT})
            self._sessions[session_id] = state
            return state

    def get_history(self, session_id: str) -> List[dict]:
        with self._lock:
            state = self._sessions.get(session_id)
            if not state:
                return []
            if self._is_expired(state):
                del self._sessions[session_id]
                return []
            state.updated_at = datetime.now(timezone.utc)
            return list(state.history)

    def get_recent_history(self, session_id: str, max_messages: int = 6) -> List[dict]:
        history = self.get_history(session_id)
        if not history:
            return []
        max_messages = max(1, max_messages)
        return history[-max_messages:]

    def add_user_message(self, session_id: str, content: str) -> bool:
        return self._add_message(session_id=session_id, role="user", content=content)

    def add_assistant_message(self, session_id: str, content: str) -> bool:
        return self._add_message(session_id=session_id, role="assistant", content=content)

    def close_session(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]

    def _add_message(self, session_id: str, role: str, content: str) -> bool:
        with self._lock:
            state = self._sessions.get(session_id)
            if not state:
                return False
            if self._is_expired(state):
                del self._sessions[session_id]
                return False
            state.history.append({"role": role, "content": content})
            state.history = state.history[-self._max_messages :]
            state.updated_at = datetime.now(timezone.utc)
            return True

    def _is_expired(self, state: SessionState) -> bool:
        return datetime.now(timezone.utc) - state.updated_at > self._ttl

    def _cleanup_expired(self) -> None:
        now = datetime.now(timezone.utc)
        expired_ids = [sid for sid, s in self._sessions.items() if now - s.updated_at > self._ttl]
        for sid in expired_ids:
            del self._sessions[sid]
