from __future__ import annotations

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from backend.auth_billing_service.services.entitlement_service import EntitlementService


class EntitlementIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._now = datetime(2026, 3, 15, 12, 0, tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)
        self.service = EntitlementService(now_provider=lambda: self._now)

    def test_byok_bypass_has_no_counter_side_effect(self):
        for idx in range(100):
            decision = self.service.reserve(user_id='u-byok', mode='byok', request_id=f'byok-{idx}')
            self.assertTrue(decision.allow)

        self.assertIsNone(self.service.get_counter(user_id='u-byok', mode='platform_key'))

    def test_same_request_id_returns_same_reservation(self):
        first = self.service.reserve(user_id='u1', mode='platform_key', request_id='req-1')
        second = self.service.reserve(user_id='u1', mode='platform_key', request_id='req-1')

        self.assertTrue(first.allow)
        self.assertTrue(second.allow)
        self.assertEqual(first.reservation_id, second.reservation_id)

        counter = self.service.get_counter(user_id='u1', mode='platform_key')
        self.assertIsNotNone(counter)
        self.assertEqual(counter.reserved, 1)
        self.assertEqual(counter.used, 0)

    def test_finalize_replay_is_idempotent(self):
        reserve = self.service.reserve(user_id='u1', mode='platform_key', request_id='req-finalize')
        first = self.service.finalize(
            reservation_id=reserve.reservation_id,
            result='success',
            idempotency_key='idem-1',
        )
        second = self.service.finalize(
            reservation_id=reserve.reservation_id,
            result='success',
            idempotency_key='idem-1',
        )

        self.assertEqual(first, second)

        counter = self.service.get_counter(user_id='u1', mode='platform_key')
        self.assertIsNotNone(counter)
        self.assertEqual(counter.reserved, 0)
        self.assertEqual(counter.used, 1)

    def test_finalize_fail_releases_reserved_without_consuming(self):
        reserve = self.service.reserve(user_id='u2', mode='platform_key', request_id='req-fail')
        outcome = self.service.finalize(
            reservation_id=reserve.reservation_id,
            result='fail',
            idempotency_key='idem-fail',
        )

        self.assertTrue(outcome.finalized)
        self.assertFalse(outcome.consumed)
        self.assertTrue(outcome.released)

        counter = self.service.get_counter(user_id='u2', mode='platform_key')
        self.assertIsNotNone(counter)
        self.assertEqual(counter.reserved, 0)
        self.assertEqual(counter.used, 0)


if __name__ == '__main__':
    unittest.main()
