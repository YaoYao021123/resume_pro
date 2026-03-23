from __future__ import annotations

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from backend.auth_billing_service.services.entitlement_service import EntitlementService


class EntitlementReserveFinalizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._now = self._bj(2026, 3, 15, 10, 0)
        self.service = EntitlementService(now_provider=lambda: self._now)

    @staticmethod
    def _bj(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
        return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)

    def _reserve(self, user_id: str, request_id: str, mode: str = 'platform_key'):
        return self.service.reserve(user_id=user_id, mode=mode, request_id=request_id)

    def test_free_user_monthly_limit_3(self):
        for idx in range(3):
            result = self._reserve(user_id='u-free', mode='platform_key', request_id=f'free-{idx}')
            self.assertTrue(result.allow)

        blocked = self._reserve(user_id='u-free', mode='platform_key', request_id='free-over')
        self.assertFalse(blocked.allow)
        self.assertEqual(blocked.error_code, 'QUOTA_EXCEEDED_MONTHLY_FREE')

    def test_member_weekly_limit_50(self):
        self.service.set_membership_active(user_id='u-member', active=True)
        for idx in range(50):
            result = self._reserve(user_id='u-member', mode='platform_key', request_id=f'member-{idx}')
            self.assertTrue(result.allow)

        blocked = self._reserve(user_id='u-member', mode='platform_key', request_id='member-over')
        self.assertFalse(blocked.allow)
        self.assertEqual(blocked.error_code, 'QUOTA_EXCEEDED_WEEKLY_MEMBER')

    def test_monthly_reset_uses_beijing_boundary(self):
        for idx in range(3):
            self.assertTrue(self._reserve(user_id='u-month', mode='platform_key', request_id=f'month-{idx}').allow)

        self.assertFalse(self._reserve(user_id='u-month', mode='platform_key', request_id='month-blocked').allow)

        self._now = self._bj(2026, 4, 1, 0, 0)
        after_reset = self._reserve(user_id='u-month', mode='platform_key', request_id='month-after-reset')
        self.assertTrue(after_reset.allow)

    def test_weekly_reset_uses_beijing_monday_boundary(self):
        self.service.set_membership_active(user_id='u-week', active=True)
        self._now = self._bj(2026, 3, 22, 23, 59)  # Sunday in Beijing
        for idx in range(50):
            self.assertTrue(self._reserve(user_id='u-week', mode='platform_key', request_id=f'week-{idx}').allow)

        self.assertFalse(self._reserve(user_id='u-week', mode='platform_key', request_id='week-blocked').allow)

        self._now = self._bj(2026, 3, 23, 0, 0)  # Monday 00:00 in Beijing
        after_reset = self._reserve(user_id='u-week', mode='platform_key', request_id='week-after-reset')
        self.assertTrue(after_reset.allow)


if __name__ == '__main__':
    unittest.main()
