from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend.auth_billing_service.models import UserRecord

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_PHONE_RE = re.compile(r'^\+?\d{7,15}$')


class AuthServiceError(Exception):
    pass


class InvalidTargetError(AuthServiceError):
    pass


class InvalidCodeError(AuthServiceError):
    pass


class ThrottledError(AuthServiceError):
    pass


@dataclass(frozen=True)
class SendCodeResult:
    sent: bool
    verification_backend: str


@dataclass
class VerificationRecord:
    code: str
    expires_at: datetime


class MemoryVerificationStore:
    backend_name = 'memory'

    def __init__(self, code_ttl_seconds: int, throttle_seconds: int) -> None:
        self._code_ttl_seconds = code_ttl_seconds
        self._throttle_seconds = throttle_seconds
        self._codes: dict[str, VerificationRecord] = {}
        self._last_sent: dict[str, datetime] = {}

    def reset(self) -> None:
        self._codes.clear()
        self._last_sent.clear()

    def send_code(self, key: str, code: str) -> None:
        now = datetime.now(timezone.utc)
        last_sent = self._last_sent.get(key)
        if last_sent is not None and (now - last_sent).total_seconds() < self._throttle_seconds:
            raise ThrottledError('send code throttled')

        self._last_sent[key] = now
        self._codes[key] = VerificationRecord(
            code=code,
            expires_at=now + timedelta(seconds=self._code_ttl_seconds),
        )

    def verify_code(self, key: str, code: str) -> bool:
        record = self._codes.get(key)
        if record is None:
            return False
        if record.expires_at < datetime.now(timezone.utc):
            self._codes.pop(key, None)
            return False
        return record.code == code


class RedisVerificationStore(MemoryVerificationStore):
    backend_name = 'redis'

    def __init__(self, redis_client, code_ttl_seconds: int, throttle_seconds: int) -> None:
        super().__init__(code_ttl_seconds=code_ttl_seconds, throttle_seconds=throttle_seconds)
        self._redis = redis_client

    def reset(self) -> None:
        super().reset()

    def send_code(self, key: str, code: str) -> None:
        throttle_key = f'auth:throttle:{key}'
        code_key = f'auth:code:{key}'
        if not self._redis.set(throttle_key, '1', ex=self._throttle_seconds, nx=True):
            raise ThrottledError('send code throttled')
        self._redis.set(code_key, code, ex=self._code_ttl_seconds)

    def verify_code(self, key: str, code: str) -> bool:
        code_key = f'auth:code:{key}'
        stored = self._redis.get(code_key)
        if stored is None:
            return False
        if isinstance(stored, bytes):
            stored = stored.decode('utf-8')
        return stored == code


class AuthService:
    def __init__(self, redis_url: str, code_ttl_seconds: int = 5 * 60, throttle_seconds: int = 60) -> None:
        self._users_by_identity: dict[str, UserRecord] = {}
        self._users_by_id: dict[str, UserRecord] = {}
        self._store = self._build_verification_store(
            redis_url=redis_url,
            code_ttl_seconds=code_ttl_seconds,
            throttle_seconds=throttle_seconds,
        )

    @property
    def verification_backend(self) -> str:
        return self._store.backend_name

    def reset(self) -> None:
        self._users_by_identity.clear()
        self._users_by_id.clear()
        self._store.reset()

    def send_code(self, channel: str, target: str) -> SendCodeResult:
        key = self._validate_and_build_key(channel, target)
        self._store.send_code(key=key, code='000000')
        return SendCodeResult(sent=True, verification_backend=self._store.backend_name)

    def verify_login_code(self, channel: str, target: str, code: str) -> bool:
        key = self._validate_and_build_key(channel, target)
        return self._store.verify_code(key=key, code=code)

    def upsert_user(self, channel: str, target: str) -> UserRecord:
        key = self._validate_and_build_key(channel, target)
        existing = self._users_by_identity.get(key)
        if existing is not None:
            existing.updated_at = datetime.now(timezone.utc)
            return existing

        user = UserRecord(user_id=str(uuid4()), identities={channel: target})
        self._users_by_identity[key] = user
        self._users_by_id[user.user_id] = user
        return user

    @staticmethod
    def _validate_and_build_key(channel: str, target: str) -> str:
        if channel not in {'email', 'phone'}:
            raise InvalidTargetError('unsupported channel')

        if channel == 'email' and not _EMAIL_RE.match(target):
            raise InvalidTargetError('invalid email target')
        if channel == 'phone' and not _PHONE_RE.match(target):
            raise InvalidTargetError('invalid phone target')

        return f'{channel}:{target}'

    @staticmethod
    def _build_verification_store(
        redis_url: str,
        code_ttl_seconds: int,
        throttle_seconds: int,
    ) -> MemoryVerificationStore:
        try:
            import redis  # type: ignore

            client = redis.Redis.from_url(redis_url, decode_responses=True)
            client.ping()
            return RedisVerificationStore(
                redis_client=client,
                code_ttl_seconds=code_ttl_seconds,
                throttle_seconds=throttle_seconds,
            )
        except Exception:
            return MemoryVerificationStore(code_ttl_seconds=code_ttl_seconds, throttle_seconds=throttle_seconds)
