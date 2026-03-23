from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from threading import RLock
from uuid import uuid4

from backend.auth_billing_service.models import PaymentOrderRecord, SubscriptionRecord


class PaymentError(Exception):
    pass


class PaymentConflictError(PaymentError):
    pass


class PaymentService:
    _SUPPORTED_CHANNELS = {'wechat', 'alipay'}
    _SUPPORTED_PLANS = {'member_weekly50'}

    def __init__(self, now_provider=None) -> None:
        self._lock = RLock()
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._orders: dict[str, PaymentOrderRecord] = {}
        self._subscriptions: dict[str, SubscriptionRecord] = {}

    def reset(self) -> None:
        with self._lock:
            self._orders.clear()
            self._subscriptions.clear()

    def set_now_provider(self, provider) -> None:
        with self._lock:
            self._now_provider = provider or (lambda: datetime.now(timezone.utc))

    def create_order(self, *, user_id: str, plan: str, channel: str) -> PaymentOrderRecord:
        if plan not in self._SUPPORTED_PLANS:
            raise PaymentError('unsupported plan')
        if channel not in self._SUPPORTED_CHANNELS:
            raise PaymentError('unsupported payment channel')

        now = self._now_provider().astimezone(timezone.utc)
        order_no = f'ord_{uuid4().hex}'
        order = PaymentOrderRecord(
            order_no=order_no,
            user_id=user_id,
            plan=plan,
            channel=channel,
            amount_cents=2990,
            currency='CNY',
            status='pending',
            created_at=now,
            expires_at=now + timedelta(minutes=30),
            updated_at=now,
        )
        with self._lock:
            self._orders[order_no] = order
        return order

    def expire_orders(self) -> None:
        now = self._now_provider().astimezone(timezone.utc)
        with self._lock:
            for order in self._orders.values():
                if order.status == 'pending' and order.expires_at is not None and order.expires_at <= now:
                    order.status = 'expired'
                    order.updated_at = now

    def verify_webhook_signature(self, payload: dict, signature: str) -> bool:
        secret = os.getenv('AUTH_BILLING_PAYMENT_WEBHOOK_SECRET', '').strip()
        if not secret:
            raise PaymentError('payment webhook secret not configured')
        message = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        expected = hmac.new(secret.encode('utf-8'), message, hashlib.sha256).hexdigest()
        return bool(signature) and hmac.compare_digest(signature, expected)

    def process_webhook(self, *, channel: str, payload: dict) -> PaymentOrderRecord:
        if channel not in self._SUPPORTED_CHANNELS:
            raise PaymentError('unsupported payment channel')

        order_no = str(payload.get('order_no', '')).strip()
        provider_trade_no = str(payload.get('provider_trade_no', '')).strip()
        status = str(payload.get('status', '')).strip()
        if not order_no or not provider_trade_no:
            raise PaymentError('order_no and provider_trade_no are required')
        if status not in {'paid', 'refunded', 'revoked'}:
            raise PaymentError('unsupported payment status')

        now = self._now_provider().astimezone(timezone.utc)
        with self._lock:
            order = self._orders.get(order_no)
            if order is None:
                raise PaymentError('order not found')
            if order.channel != channel:
                raise PaymentError('payment channel mismatch')

            if order.provider_trade_no and order.provider_trade_no != provider_trade_no:
                raise PaymentConflictError('provider_trade_no conflict for order_no')

            if order.provider_trade_no == provider_trade_no and order.status == status:
                return order

            if status == 'paid':
                if order.status == 'pending' and order.expires_at and order.expires_at <= now:
                    order.status = 'expired'
                    order.updated_at = now
                    return order
                if order.status == 'expired':
                    return order
                if order.status != 'paid':
                    order.status = 'paid'
                    order.provider_trade_no = provider_trade_no
                    order.paid_at = now
                    order.updated_at = now
                    self._recompute_subscription(user_id=order.user_id, changed_at=now)
                return order

            if status in {'refunded', 'revoked'}:
                order.status = status
                order.provider_trade_no = provider_trade_no
                order.updated_at = now
                self._recompute_subscription(user_id=order.user_id, changed_at=now)
                return order

            raise PaymentError('unsupported payment status')

    def get_order(self, order_no: str) -> PaymentOrderRecord | None:
        with self._lock:
            return self._orders.get(order_no)

    def get_subscription(self, user_id: str) -> SubscriptionRecord | None:
        with self._lock:
            return self._subscriptions.get(user_id)

    def is_member_active(self, user_id: str) -> bool:
        now = self._now_provider().astimezone(timezone.utc)
        with self._lock:
            sub = self._subscriptions.get(user_id)
            return bool(sub and sub.status == 'active' and sub.end_at > now)

    def _recompute_subscription(self, *, user_id: str, changed_at: datetime) -> None:
        paid_orders = sorted(
            (
                order
                for order in self._orders.values()
                if order.user_id == user_id and order.status == 'paid' and order.paid_at is not None
            ),
            key=lambda order: (order.paid_at, order.order_no),
        )

        if not paid_orders:
            self._subscriptions.pop(user_id, None)
            return

        current_start: datetime | None = None
        current_end: datetime | None = None
        for order in paid_orders:
            paid_at = order.paid_at
            if paid_at is None:
                continue
            if current_end is None or current_end <= paid_at:
                current_start = paid_at
                current_end = paid_at + timedelta(days=30)
            else:
                current_end = current_end + timedelta(days=30)

        if current_start is None or current_end is None:
            self._subscriptions.pop(user_id, None)
            return

        status = 'active' if current_end > changed_at else 'inactive'
        self._subscriptions[user_id] = SubscriptionRecord(
            user_id=user_id,
            plan='member_weekly50',
            status=status,
            start_at=current_start,
            end_at=current_end,
            updated_at=changed_at,
        )
