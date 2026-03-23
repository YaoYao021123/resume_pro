from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.auth_billing_service.models import (
    EntitlementFinalizeEventRecord,
    EntitlementReservationRecord,
    UsageCounterRecord,
)


class EntitlementError(Exception):
    pass


@dataclass(frozen=True)
class ReserveDecision:
    allow: bool
    reservation_id: str | None
    remaining_after_reserve: int | None
    reset_at: datetime | None
    error_code: str | None = None


@dataclass(frozen=True)
class FinalizeDecision:
    finalized: bool
    consumed: bool
    released: bool
    remaining: int | None


class EntitlementService:
    _BJ_TZ = ZoneInfo('Asia/Shanghai')

    def __init__(self, now_provider=None, reservation_ttl_seconds: int = 15 * 60) -> None:
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._reservation_ttl = timedelta(seconds=reservation_ttl_seconds)
        self._lock = RLock()
        self._membership_active: dict[str, bool] = {}
        self._usage_counters: dict[tuple[str, str, str, datetime], UsageCounterRecord] = {}
        self._reservations_by_id: dict[str, EntitlementReservationRecord] = {}
        self._reserve_by_user_request: dict[tuple[str, str, str], ReserveDecision] = {}
        self._finalize_events_by_reservation: dict[str, EntitlementFinalizeEventRecord] = {}
        self._finalize_decision_by_idempotency: dict[tuple[str, str], FinalizeDecision] = {}

    def reset(self) -> None:
        with self._lock:
            self._membership_active.clear()
            self._usage_counters.clear()
            self._reservations_by_id.clear()
            self._reserve_by_user_request.clear()
            self._finalize_events_by_reservation.clear()
            self._finalize_decision_by_idempotency.clear()

    def set_membership_active(self, user_id: str, active: bool) -> None:
        with self._lock:
            self._membership_active[user_id] = active

    def reserve(self, user_id: str, mode: str, request_id: str) -> ReserveDecision:
        with self._lock:
            idempotency_key = (user_id, mode, request_id)
            cached = self._reserve_by_user_request.get(idempotency_key)
            if cached is not None:
                return cached

            if mode == 'byok':
                decision = ReserveDecision(
                    allow=True,
                    reservation_id=None,
                    remaining_after_reserve=None,
                    reset_at=None,
                )
                self._reserve_by_user_request[idempotency_key] = decision
                return decision

            if mode != 'platform_key':
                raise EntitlementError('unsupported entitlement mode')

            now = self._now_provider().astimezone(timezone.utc)
            period_type, period_limit, exceed_error = self._plan_for_user(user_id)
            period_start = self._period_start(now=now, period_type=period_type)
            counter = self._counter_for(
                user_id=user_id,
                mode=mode,
                period_type=period_type,
                period_start=period_start,
                period_limit=period_limit,
            )

            if counter.used + counter.reserved >= counter.limit:
                decision = ReserveDecision(
                    allow=False,
                    reservation_id=None,
                    remaining_after_reserve=max(counter.limit - counter.used - counter.reserved, 0),
                    reset_at=self._next_reset(period_start=period_start, period_type=period_type),
                    error_code=exceed_error,
                )
                self._reserve_by_user_request[idempotency_key] = decision
                return decision

            counter.reserved += 1
            reservation_id = f'rsv_{uuid4().hex}'
            self._reservations_by_id[reservation_id] = EntitlementReservationRecord(
                reservation_id=reservation_id,
                user_id=user_id,
                request_id=request_id,
                mode=mode,
                period_type=period_type,
                period_start=period_start,
                status='reserved',
                created_at=now,
                expires_at=now + self._reservation_ttl,
            )
            decision = ReserveDecision(
                allow=True,
                reservation_id=reservation_id,
                remaining_after_reserve=counter.limit - counter.used - counter.reserved,
                reset_at=self._next_reset(period_start=period_start, period_type=period_type),
                error_code=None,
            )
            self._reserve_by_user_request[idempotency_key] = decision
            return decision

    def finalize(self, reservation_id: str, result: str, idempotency_key: str) -> FinalizeDecision:
        with self._lock:
            replay = self._finalize_decision_by_idempotency.get((reservation_id, idempotency_key))
            if replay is not None:
                return replay

            if result not in {'success', 'fail'}:
                raise EntitlementError('unsupported finalize result')

            reservation = self._reservations_by_id.get(reservation_id)
            if reservation is None:
                raise EntitlementError('reservation not found')

            prior = self._finalize_events_by_reservation.get(reservation_id)
            if prior is not None:
                decision = FinalizeDecision(
                    finalized=prior.finalized,
                    consumed=prior.consumed,
                    released=prior.released,
                    remaining=prior.remaining,
                )
                self._finalize_decision_by_idempotency[(reservation_id, idempotency_key)] = decision
                return decision

            if reservation.status != 'reserved':
                raise EntitlementError('reservation is not reservable')

            counter = None
            if reservation.mode == 'platform_key' and reservation.period_type and reservation.period_start:
                counter_key = (reservation.user_id, reservation.mode, reservation.period_type, reservation.period_start)
                counter = self._usage_counters.get(counter_key)

            if counter is not None and counter.reserved > 0:
                counter.reserved -= 1

            consumed = result == 'success'
            released = result == 'fail'
            if counter is not None and consumed:
                counter.used += 1

            reservation.status = 'finalized' if consumed else 'released'
            remaining = None if counter is None else counter.limit - counter.used - counter.reserved
            decision = FinalizeDecision(
                finalized=True,
                consumed=consumed,
                released=released,
                remaining=remaining,
            )
            event = EntitlementFinalizeEventRecord(
                idempotency_key=idempotency_key,
                reservation_id=reservation_id,
                result=result,
                finalized=decision.finalized,
                consumed=decision.consumed,
                released=decision.released,
                remaining=decision.remaining,
                created_at=self._now_provider().astimezone(timezone.utc),
            )
            self._finalize_events_by_reservation[reservation_id] = event
            self._finalize_decision_by_idempotency[(reservation_id, idempotency_key)] = decision
            return decision

    def get_counter(self, user_id: str, mode: str = 'platform_key') -> UsageCounterRecord | None:
        if mode != 'platform_key':
            return None
        with self._lock:
            now = self._now_provider().astimezone(timezone.utc)
            period_type, period_limit, _ = self._plan_for_user(user_id)
            period_start = self._period_start(now=now, period_type=period_type)
            return self._usage_counters.get((user_id, mode, period_type, period_start))

    def get_reservation(self, reservation_id: str) -> EntitlementReservationRecord | None:
        with self._lock:
            return self._reservations_by_id.get(reservation_id)

    def has_success_finalize_event(self, reservation_id: str) -> bool:
        with self._lock:
            event = self._finalize_events_by_reservation.get(reservation_id)
            return bool(event and event.result == 'success')

    def list_expired_reservations(self, now: datetime | None = None) -> list[EntitlementReservationRecord]:
        current = (now or self._now_provider()).astimezone(timezone.utc)
        with self._lock:
            return [
                reservation
                for reservation in self._reservations_by_id.values()
                if reservation.status == 'reserved'
                and reservation.expires_at is not None
                and reservation.expires_at <= current
            ]

    def release_reservation(self, reservation_id: str) -> bool:
        with self._lock:
            reservation = self._reservations_by_id.get(reservation_id)
            if reservation is None:
                raise EntitlementError('reservation not found')
            if reservation.status != 'reserved':
                return False

            counter = None
            if reservation.mode == 'platform_key' and reservation.period_type and reservation.period_start:
                counter_key = (reservation.user_id, reservation.mode, reservation.period_type, reservation.period_start)
                counter = self._usage_counters.get(counter_key)
            if counter is not None and counter.reserved > 0:
                counter.reserved -= 1

            reservation.status = 'released'
            return True

    def _plan_for_user(self, user_id: str) -> tuple[str, int, str]:
        if self._membership_active.get(user_id, False):
            return ('week', 50, 'QUOTA_EXCEEDED_WEEKLY_MEMBER')
        return ('month', 3, 'QUOTA_EXCEEDED_MONTHLY_FREE')

    def _counter_for(
        self,
        user_id: str,
        mode: str,
        period_type: str,
        period_start: datetime,
        period_limit: int,
    ) -> UsageCounterRecord:
        key = (user_id, mode, period_type, period_start)
        existing = self._usage_counters.get(key)
        if existing is not None:
            return existing

        created = UsageCounterRecord(
            user_id=user_id,
            mode=mode,
            period_type=period_type,
            period_start=period_start,
            limit=period_limit,
            used=0,
            reserved=0,
        )
        self._usage_counters[key] = created
        return created

    def _period_start(self, now: datetime, period_type: str) -> datetime:
        bj_now = now.astimezone(self._BJ_TZ)
        if period_type == 'month':
            start_bj = bj_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period_type == 'week':
            start_of_day = bj_now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_bj = start_of_day - timedelta(days=start_of_day.weekday())
        else:
            raise EntitlementError('unsupported period type')
        return start_bj.astimezone(timezone.utc)

    def _next_reset(self, period_start: datetime, period_type: str) -> datetime:
        start_bj = period_start.astimezone(self._BJ_TZ)
        if period_type == 'month':
            if start_bj.month == 12:
                next_bj = start_bj.replace(year=start_bj.year + 1, month=1)
            else:
                next_bj = start_bj.replace(month=start_bj.month + 1)
        elif period_type == 'week':
            next_bj = start_bj + timedelta(days=7)
        else:
            raise EntitlementError('unsupported period type')
        return next_bj.astimezone(timezone.utc)
