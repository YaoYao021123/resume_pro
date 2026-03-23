from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.auth_billing_service.models import ByokKeyRecord, utcnow

_ALLOWED_PROVIDERS = {
    'openai',
    'gemini',
    'anthropic',
    'glm',
    'kimi',
    'minimax',
    'grok',
    'qwen',
    'doubao',
    'other',
}
_KEY_ALLOWED_CHARS_RE = re.compile(r'^[!-~]+$')
_KEY_MIN_LEN = 16
_KEY_MAX_LEN = 256


class ByokValidationError(Exception):
    pass


class ByokConfigurationError(Exception):
    pass


@dataclass(frozen=True)
class ByokView:
    user_id: str
    provider: str
    has_active_key: bool
    masked_key: str | None
    fingerprint: str | None


@dataclass(frozen=True)
class EffectiveByokConfig:
    provider: str
    source: str
    api_key: str | None
    masked_key: str | None
    fingerprint: str | None


class ByokService:
    def __init__(self, encryption_secret: str | None = None) -> None:
        self._lock = RLock()
        self._records: list[ByokKeyRecord] = []
        self._encryption_secret = (encryption_secret or '').strip() or None
        self._secret_min_length = 16

    def reset(self) -> None:
        with self._lock:
            self._records.clear()

    def upsert(self, *, user_id: str, provider: str, api_key: str) -> ByokView:
        self._require_crypto_ready()
        norm_user = self._validate_user_id(user_id)
        norm_provider = self._validate_provider(provider)
        norm_key = self._validate_api_key(api_key)
        now = utcnow()

        with self._lock:
            for rec in self._records:
                if rec.user_id == norm_user and rec.provider == norm_provider and rec.active:
                    rec.active = False
                    rec.updated_at = now
                    rec.deactivated_at = now

            masked_key = self._mask_key(norm_key)
            fingerprint = self._fingerprint(norm_key)
            created = ByokKeyRecord(
                key_id=f'byok_{uuid4().hex}',
                user_id=norm_user,
                provider=norm_provider,
                encrypted_key=self._encrypt(norm_key),
                fingerprint=fingerprint,
                masked_key=masked_key,
                key_length=len(norm_key),
                active=True,
                created_at=now,
                updated_at=now,
                deactivated_at=None,
            )
            self._records.append(created)
            return ByokView(
                user_id=norm_user,
                provider=norm_provider,
                has_active_key=True,
                masked_key=masked_key,
                fingerprint=fingerprint,
            )

    def get(self, *, user_id: str, provider: str) -> ByokView:
        self._require_crypto_ready()
        norm_user = self._validate_user_id(user_id)
        norm_provider = self._validate_provider(provider)

        with self._lock:
            active = self._find_active_record(user_id=norm_user, provider=norm_provider)
            if active is None:
                return ByokView(
                    user_id=norm_user,
                    provider=norm_provider,
                    has_active_key=False,
                    masked_key=None,
                    fingerprint=None,
                )
            return ByokView(
                user_id=norm_user,
                provider=norm_provider,
                has_active_key=True,
                masked_key=active.masked_key,
                fingerprint=active.fingerprint,
            )

    def delete(self, *, user_id: str, provider: str) -> bool:
        self._require_crypto_ready()
        norm_user = self._validate_user_id(user_id)
        norm_provider = self._validate_provider(provider)
        now = utcnow()

        with self._lock:
            deleted = False
            for rec in self._records:
                if rec.user_id == norm_user and rec.provider == norm_provider and rec.active:
                    rec.active = False
                    rec.updated_at = now
                    rec.deactivated_at = now
                    deleted = True
            return deleted

    def resolve_effective_config(
        self,
        *,
        user_id: str,
        provider: str,
        request_key: str | None,
    ) -> EffectiveByokConfig:
        self._require_crypto_ready()
        norm_user = self._validate_user_id(user_id)
        norm_provider = self._validate_provider(provider)

        if request_key is not None and request_key.strip():
            norm_request_key = self._validate_api_key(request_key)
            return EffectiveByokConfig(
                provider=norm_provider,
                source='request_key',
                api_key=norm_request_key,
                masked_key=self._mask_key(norm_request_key),
                fingerprint=self._fingerprint(norm_request_key),
            )

        with self._lock:
            active = self._find_active_record(user_id=norm_user, provider=norm_provider)
            if active is None:
                return EffectiveByokConfig(
                    provider=norm_provider,
                    source='none',
                    api_key=None,
                    masked_key=None,
                    fingerprint=None,
                )
            return EffectiveByokConfig(
                provider=norm_provider,
                source='stored_byok',
                api_key=self._decrypt(active.encrypted_key),
                masked_key=active.masked_key,
                fingerprint=active.fingerprint,
            )

    @staticmethod
    def _validate_user_id(user_id: str) -> str:
        norm = str(user_id).strip()
        if not norm:
            raise ByokValidationError('user_id is required')
        return norm

    @staticmethod
    def _validate_provider(provider: str) -> str:
        norm = str(provider).strip().lower()
        if norm not in _ALLOWED_PROVIDERS:
            raise ByokValidationError('unsupported provider')
        return norm

    @staticmethod
    def _validate_api_key(api_key: str) -> str:
        norm = str(api_key).strip()
        if not norm:
            raise ByokValidationError('api_key is required')
        if len(norm) < _KEY_MIN_LEN or len(norm) > _KEY_MAX_LEN:
            raise ByokValidationError('api_key length is invalid')
        if not _KEY_ALLOWED_CHARS_RE.match(norm):
            raise ByokValidationError('api_key contains invalid characters')
        return norm

    def _find_active_record(self, *, user_id: str, provider: str) -> ByokKeyRecord | None:
        for rec in reversed(self._records):
            if rec.user_id == user_id and rec.provider == provider and rec.active:
                return rec
        return None

    @staticmethod
    def _fingerprint(api_key: str) -> str:
        return hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:12]

    @staticmethod
    def _mask_key(api_key: str) -> str:
        if len(api_key) <= 8:
            return '*' * len(api_key)
        return f'{api_key[:4]}{"*" * (len(api_key) - 8)}{api_key[-4:]}'

    def _encrypt(self, plaintext: str) -> str:
        secret = self._secret_bytes()
        nonce = os.urandom(12)
        plaintext_bytes = plaintext.encode('utf-8')
        cipher = AESGCM(secret).encrypt(nonce, plaintext_bytes, None)
        version = b'v2'
        return base64.urlsafe_b64encode(version + nonce + cipher).decode('utf-8')

    def _decrypt(self, encrypted: str) -> str:
        secret = self._secret_bytes()
        try:
            raw = base64.urlsafe_b64decode(encrypted.encode('utf-8'))
        except Exception as exc:
            raise ByokValidationError('stored api_key is corrupted') from exc

        version_len = 2
        nonce_len = 12
        min_len = version_len + nonce_len + 16
        if len(raw) < min_len:
            raise ByokValidationError('stored api_key is corrupted')

        version = raw[:version_len]
        nonce = raw[version_len: version_len + nonce_len]
        cipher = raw[version_len + nonce_len:]
        if version != b'v2':
            raise ByokValidationError('stored api_key is corrupted')

        try:
            plain = AESGCM(secret).decrypt(nonce, cipher, None)
            return plain.decode('utf-8')
        except Exception as exc:
            raise ByokValidationError('stored api_key is corrupted') from exc

    def _require_crypto_ready(self) -> None:
        self._secret_bytes()

    def _secret_bytes(self) -> bytes:
        secret = self._encryption_secret or os.getenv('AUTH_BILLING_BYOK_SECRET', '').strip()
        if not secret:
            raise ByokConfigurationError('AUTH_BILLING_BYOK_SECRET is required')
        if len(secret) < self._secret_min_length:
            raise ByokConfigurationError('AUTH_BILLING_BYOK_SECRET must be at least 16 characters')
        digest = hashlib.sha256(secret.encode('utf-8')).digest()
        return digest
