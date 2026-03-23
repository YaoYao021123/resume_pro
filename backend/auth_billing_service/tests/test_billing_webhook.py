from __future__ import annotations

import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from backend.auth_billing_service import main as auth_main
from backend.auth_billing_service.main import app, reset_runtime_state_for_tests


class BillingWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_runtime_state_for_tests()
        self.client = TestClient(app)
        self._old_webhook_secret = os.environ.get('AUTH_BILLING_PAYMENT_WEBHOOK_SECRET')
        os.environ['AUTH_BILLING_PAYMENT_WEBHOOK_SECRET'] = 'unit-payment-secret'
        self._base_now = datetime(2026, 3, 22, 12, 0, tzinfo=timezone.utc)
        auth_main._payment_service.set_now_provider(lambda: self._base_now)

    def tearDown(self) -> None:
        if self._old_webhook_secret is None:
            os.environ.pop('AUTH_BILLING_PAYMENT_WEBHOOK_SECRET', None)
        else:
            os.environ['AUTH_BILLING_PAYMENT_WEBHOOK_SECRET'] = self._old_webhook_secret
        auth_main._payment_service.set_now_provider(None)

    @staticmethod
    def _sign_payload(payload: dict, secret: str = 'unit-payment-secret') -> str:
        message = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hmac.new(secret.encode('utf-8'), message, hashlib.sha256).hexdigest()

    def _create_order(self, *, user_id: str = 'user-1', channel: str = 'wechat') -> dict:
        resp = self.client.post(
            '/billing/create-order',
            json={'plan': 'member_weekly50', 'channel': channel, 'user_id': user_id},
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def _post_webhook(self, *, channel: str, payload: dict, valid_signature: bool = True):
        sig = self._sign_payload(payload)
        if not valid_signature:
            sig = 'bad-signature'
        return self.client.post(
            f'/billing/webhook/{channel}',
            json=payload,
            headers={'X-Payment-Signature': sig},
        )

    def test_create_order_returns_provider_payload(self):
        resp = self.client.post('/billing/create-order', json={'plan': 'member_weekly50', 'channel': 'wechat'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('order_no', resp.json())

    def test_create_order_uses_default_price_and_currency(self):
        resp = self.client.post('/billing/create-order', json={'plan': 'member_weekly50', 'channel': 'alipay'})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['amount_cents'], 2990)
        self.assertEqual(data['currency'], 'CNY')

    def test_webhook_invalid_signature_rejected(self):
        order = self._create_order(channel='wechat')
        payload = {
            'order_no': order['order_no'],
            'provider_trade_no': 'wx_trade_001',
            'status': 'paid',
        }
        resp = self._post_webhook(channel='wechat', payload=payload, valid_signature=False)
        self.assertEqual(resp.status_code, 401)

    def test_duplicate_webhook_is_idempotent(self):
        order = self._create_order(user_id='dup-user', channel='wechat')
        payload = {
            'order_no': order['order_no'],
            'provider_trade_no': 'wx_trade_002',
            'status': 'paid',
        }

        first = self._post_webhook(channel='wechat', payload=payload)
        second = self._post_webhook(channel='wechat', payload=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        sub = auth_main._payment_service.get_subscription('dup-user')
        self.assertIsNotNone(sub)
        self.assertEqual(sub.status, 'active')
        self.assertEqual(sub.end_at, self._base_now + timedelta(days=30))

    def test_duplicate_payment_matches_order_and_provider_trade_no(self):
        order = self._create_order(user_id='pair-user', channel='alipay')
        order_no = order['order_no']

        first_payload = {'order_no': order_no, 'provider_trade_no': 'ali_trade_A', 'status': 'paid'}
        duplicate_payload = {'order_no': order_no, 'provider_trade_no': 'ali_trade_A', 'status': 'paid'}
        mismatch_payload = {'order_no': order_no, 'provider_trade_no': 'ali_trade_B', 'status': 'paid'}

        first = self._post_webhook(channel='alipay', payload=first_payload)
        duplicate = self._post_webhook(channel='alipay', payload=duplicate_payload)
        mismatch = self._post_webhook(channel='alipay', payload=mismatch_payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(mismatch.status_code, 409)

        updated = auth_main._payment_service.get_order(order_no)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.provider_trade_no, 'ali_trade_A')

    def test_refund_recomputes_subscription_without_blanket_deactivate(self):
        first_order = self._create_order(user_id='rollback-user', channel='wechat')
        first_payload = {
            'order_no': first_order['order_no'],
            'provider_trade_no': 'wx_trade_rollback_1',
            'status': 'paid',
        }
        self.assertEqual(self._post_webhook(channel='wechat', payload=first_payload).status_code, 200)

        auth_main._payment_service.set_now_provider(lambda: self._base_now + timedelta(days=10))
        second_order = self._create_order(user_id='rollback-user', channel='alipay')
        second_payload = {
            'order_no': second_order['order_no'],
            'provider_trade_no': 'ali_trade_rollback_2',
            'status': 'paid',
        }
        self.assertEqual(self._post_webhook(channel='alipay', payload=second_payload).status_code, 200)

        auth_main._payment_service.set_now_provider(lambda: self._base_now + timedelta(days=11))
        refund_payload = {
            'order_no': first_order['order_no'],
            'provider_trade_no': 'wx_trade_rollback_1',
            'status': 'refunded',
        }
        refund = self._post_webhook(channel='wechat', payload=refund_payload)
        self.assertEqual(refund.status_code, 200)

        sub = auth_main._payment_service.get_subscription('rollback-user')
        self.assertIsNotNone(sub)
        self.assertEqual(sub.status, 'active')
        self.assertEqual(sub.start_at, self._base_now + timedelta(days=10))
        self.assertEqual(sub.end_at, self._base_now + timedelta(days=40))

    def test_order_expiry_after_30_minutes(self):
        order = self._create_order(user_id='expiry-user', channel='wechat')
        order_no = order['order_no']

        auth_main._payment_service.set_now_provider(lambda: self._base_now + timedelta(minutes=31))
        auth_main._payment_service.expire_orders()

        expired_order = auth_main._payment_service.get_order(order_no)
        self.assertIsNotNone(expired_order)
        self.assertEqual(expired_order.status, 'expired')

        payload = {
            'order_no': order_no,
            'provider_trade_no': 'wx_trade_expired',
            'status': 'paid',
        }
        resp = self._post_webhook(channel='wechat', payload=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(auth_main._payment_service.get_subscription('expiry-user'))

    def test_subscription_renewal_extends_active_and_resets_from_now_when_expired(self):
        first_order = self._create_order(user_id='renew-user', channel='wechat')
        first_payload = {
            'order_no': first_order['order_no'],
            'provider_trade_no': 'wx_trade_renew_1',
            'status': 'paid',
        }
        self.assertEqual(self._post_webhook(channel='wechat', payload=first_payload).status_code, 200)
        sub = auth_main._payment_service.get_subscription('renew-user')
        self.assertIsNotNone(sub)
        self.assertEqual(sub.start_at, self._base_now)
        self.assertEqual(sub.end_at, self._base_now + timedelta(days=30))

        auth_main._payment_service.set_now_provider(lambda: self._base_now + timedelta(days=10))
        second_order = self._create_order(user_id='renew-user', channel='alipay')
        second_payload = {
            'order_no': second_order['order_no'],
            'provider_trade_no': 'ali_trade_renew_2',
            'status': 'paid',
        }
        self.assertEqual(self._post_webhook(channel='alipay', payload=second_payload).status_code, 200)
        sub = auth_main._payment_service.get_subscription('renew-user')
        self.assertIsNotNone(sub)
        self.assertEqual(sub.start_at, self._base_now)
        self.assertEqual(sub.end_at, self._base_now + timedelta(days=60))

        auth_main._payment_service.set_now_provider(lambda: self._base_now + timedelta(days=70))
        third_order = self._create_order(user_id='renew-user', channel='wechat')
        third_payload = {
            'order_no': third_order['order_no'],
            'provider_trade_no': 'wx_trade_renew_3',
            'status': 'paid',
        }
        self.assertEqual(self._post_webhook(channel='wechat', payload=third_payload).status_code, 200)
        sub = auth_main._payment_service.get_subscription('renew-user')
        self.assertIsNotNone(sub)
        self.assertEqual(sub.start_at, self._base_now + timedelta(days=70))
        self.assertEqual(sub.end_at, self._base_now + timedelta(days=100))


if __name__ == '__main__':
    unittest.main()
