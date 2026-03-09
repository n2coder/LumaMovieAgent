from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List
from uuid import uuid4

import jwt
from fastapi import HTTPException

from app.config import Settings


GREETING_TEXT = "Hi, I am Luma. Tell me what kind of movie you feel like watching."


@dataclass
class SessionState:
    session_id: str
    history: List[dict]


class SessionTokenManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.secret = settings.session_jwt_secret
        self.ttl = timedelta(minutes=max(1, settings.session_ttl_minutes))
        self.max_messages = max(2, settings.session_max_messages)

    def start_session(self) -> tuple[str, str, str]:
        session_id = uuid4().hex
        greeting = GREETING_TEXT
        history = [{"role": "assistant", "content": greeting}]
        token = self.encode(session_id=session_id, history=history)
        return session_id, token, greeting

    def encode(self, session_id: str, history: List[dict]) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "sid": session_id,
            "history": history[-self.max_messages :],
            "iat": int(now.timestamp()),
            "exp": int((now + self.ttl).timestamp()),
        }
        return jwt.encode(payload, self.secret, algorithm="HS256")

    def decode(self, token: str) -> SessionState:
        if not token:
            raise HTTPException(status_code=401, detail="session token is required")
        try:
            payload = jwt.decode(token, self.secret, algorithms=["HS256"])
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(status_code=401, detail="session expired") from exc
        except jwt.InvalidTokenError as exc:
            raise HTTPException(status_code=401, detail="invalid session token") from exc

        sid = str(payload.get("sid", "")).strip()
        if not sid:
            raise HTTPException(status_code=401, detail="invalid session token payload")
        history = payload.get("history", [])
        if not isinstance(history, list):
            history = []
        normalized = []
        for item in history:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                normalized.append({"role": role, "content": content})
        return SessionState(session_id=sid, history=normalized[-self.max_messages :])

    def append(self, token: str, role: str, content: str) -> tuple[SessionState, str]:
        state = self.decode(token)
        if role not in {"user", "assistant"}:
            raise HTTPException(status_code=400, detail="invalid message role")
        content = (content or "").strip()
        if content:
            state.history.append({"role": role, "content": content})
            state.history = state.history[-self.max_messages :]
        new_token = self.encode(state.session_id, state.history)
        return state, new_token

    def update_history(self, token: str, user_text: str | None, assistant_text: str | None) -> tuple[SessionState, str]:
        state = self.decode(token)
        if user_text:
            state.history.append({"role": "user", "content": user_text.strip()})
        if assistant_text:
            state.history.append({"role": "assistant", "content": assistant_text.strip()})
        state.history = state.history[-self.max_messages :]
        return state, self.encode(state.session_id, state.history)
