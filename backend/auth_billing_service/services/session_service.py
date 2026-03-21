from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend.auth_billing_service.models import SessionRecord


class SessionNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int


class SessionService:
    def __init__(self, access_ttl_seconds: int = 2 * 60 * 60, max_active_sessions: int = 3) -> None:
        self._access_ttl_seconds = access_ttl_seconds
        self._max_active_sessions = max_active_sessions
        self._sessions_by_refresh: dict[str, SessionRecord] = {}
        self._sessions_by_user: dict[str, list[SessionRecord]] = {}

    def reset(self) -> None:
        self._sessions_by_refresh.clear()
        self._sessions_by_user.clear()

    def issue_tokens(self, user_id: str) -> IssuedTokens:
        session = SessionRecord(
            session_id=str(uuid4()),
            user_id=user_id,
            refresh_token=self._new_refresh_token(),
            access_token=self._new_access_token(),
        )
        user_sessions = self._sessions_by_user.setdefault(user_id, [])
        user_sessions.append(session)
        self._sessions_by_refresh[session.refresh_token] = session
        self._enforce_max_active_sessions(user_id)
        return IssuedTokens(
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            expires_in=self._access_ttl_seconds,
        )

    def rotate_refresh_token(self, refresh_token: str) -> IssuedTokens:
        session = self._sessions_by_refresh.get(refresh_token)
        if session is None or not session.active:
            raise SessionNotFoundError('refresh token is invalid or revoked')

        self._sessions_by_refresh.pop(refresh_token, None)
        session.refresh_token = self._new_refresh_token()
        session.access_token = self._new_access_token()
        session.rotated_at = datetime.now(timezone.utc)
        self._sessions_by_refresh[session.refresh_token] = session
        return IssuedTokens(
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            expires_in=self._access_ttl_seconds,
        )

    def revoke_by_refresh_token(self, refresh_token: str) -> None:
        session = self._sessions_by_refresh.pop(refresh_token, None)
        if session is None:
            return
        session.revoked_at = datetime.now(timezone.utc)

    def _enforce_max_active_sessions(self, user_id: str) -> None:
        user_sessions = self._sessions_by_user.get(user_id, [])
        active = [s for s in user_sessions if s.active]
        if len(active) <= self._max_active_sessions:
            return

        active.sort(key=lambda item: item.created_at)
        extra = len(active) - self._max_active_sessions
        for stale in active[:extra]:
            stale.revoked_at = datetime.now(timezone.utc)
            self._sessions_by_refresh.pop(stale.refresh_token, None)

    @staticmethod
    def _new_access_token() -> str:
        expire_at = datetime.now(timezone.utc) + timedelta(hours=2)
        return f"at_{uuid4().hex}_{int(expire_at.timestamp())}"

    @staticmethod
    def _new_refresh_token() -> str:
        return f"rt_{uuid4().hex}"
