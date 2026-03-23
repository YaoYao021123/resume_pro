from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class UserRecord:
    user_id: str
    identities: dict[str, str]
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class SessionRecord:
    session_id: str
    user_id: str
    refresh_token: str
    access_token: str
    created_at: datetime = field(default_factory=utcnow)
    rotated_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass
class UsageCounterRecord:
    user_id: str
    mode: str
    period_type: str
    period_start: datetime
    limit: int
    used: int = 0
    reserved: int = 0


@dataclass
class EntitlementReservationRecord:
    reservation_id: str
    user_id: str
    request_id: str
    mode: str
    period_type: str | None
    period_start: datetime | None
    status: str
    created_at: datetime = field(default_factory=utcnow)
    expires_at: datetime | None = None


@dataclass
class EntitlementFinalizeEventRecord:
    idempotency_key: str
    reservation_id: str
    result: str
    finalized: bool
    consumed: bool
    released: bool
    remaining: int | None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class PaymentOrderRecord:
    order_no: str
    user_id: str
    plan: str
    channel: str
    amount_cents: int
    currency: str
    status: str
    provider_trade_no: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    expires_at: datetime | None = None
    paid_at: datetime | None = None
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class SubscriptionRecord:
    user_id: str
    plan: str
    status: str
    start_at: datetime
    end_at: datetime
    updated_at: datetime = field(default_factory=utcnow)



@dataclass
class ByokKeyRecord:
    key_id: str
    user_id: str
    provider: str
    encrypted_key: str
    fingerprint: str
    masked_key: str
    key_length: int
    active: bool = True
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    deactivated_at: datetime | None = None
