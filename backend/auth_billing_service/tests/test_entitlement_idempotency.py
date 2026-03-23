from __future__ import annotations

import hashlib
import hmac
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from backend.auth_billing_service import main as auth_main
from backend.auth_billing_service.main import app, get_observability_snapshot_for_tests, reset_runtime_state_for_tests
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

    def test_request_id_idempotency_is_mode_scoped(self):
        byok = self.service.reserve(user_id='u-scope', mode='byok', request_id='same-id')
        plat = self.service.reserve(user_id='u-scope', mode='platform_key', request_id='same-id')
        self.assertTrue(byok.allow)
        self.assertTrue(plat.allow)
        self.assertIsNone(byok.reservation_id)
        self.assertIsNotNone(plat.reservation_id)

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

    def test_finalize_idempotency_key_is_reservation_scoped(self):
        r1 = self.service.reserve(user_id='u-a', mode='platform_key', request_id='r1')
        r2 = self.service.reserve(user_id='u-a', mode='platform_key', request_id='r2')
        d1 = self.service.finalize(
            reservation_id=r1.reservation_id,
            result='success',
            idempotency_key='same-key',
        )
        d2 = self.service.finalize(
            reservation_id=r2.reservation_id,
            result='success',
            idempotency_key='same-key',
        )
        self.assertTrue(d1.consumed)
        self.assertTrue(d2.consumed)

        counter = self.service.get_counter(user_id='u-a', mode='platform_key')
        self.assertIsNotNone(counter)
        self.assertEqual(counter.used, 2)
        self.assertEqual(counter.reserved, 0)

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

    def test_finalize_retry_worker_retries_and_dead_letters_after_5(self):
        from backend.auth_billing_service.workers.finalize_retry_worker import run_finalize_retry_once

        attempts = {'count': 0}

        def _finalize(**_kwargs):
            attempts['count'] += 1
            raise RuntimeError('backend timeout')

        jobs = [
            {
                'reservation_id': 'rsv-x',
                'request_id': 'req-x',
                'user_id': 'u-x',
                'result': 'success',
                'idempotency_key': 'idem-x',
                'status': 'pending',
                'retry_count': 0,
                'next_retry_at': self._now.isoformat(),
            }
        ]

        for _ in range(5):
            run_finalize_retry_once(jobs=jobs, finalize_func=_finalize, now=self._now)
            if jobs[0]['status'] != 'dead_letter':
                self._now = datetime.fromisoformat(jobs[0]['next_retry_at'])

        self.assertEqual(attempts['count'], 5)
        self.assertEqual(jobs[0]['status'], 'dead_letter')
        self.assertEqual(jobs[0]['retry_count'], 5)

    def test_recycle_worker_skips_success_pending_finalize(self):
        from backend.auth_billing_service.workers.reservation_recycle_worker import run_reservation_recycle_once

        reserve = self.service.reserve(user_id='u-recycle', mode='platform_key', request_id='req-recycle')
        reservation = self.service.get_reservation(reserve.reservation_id)
        reservation.expires_at = self._now - timedelta(minutes=1)

        pending_finalize_jobs = [
            {
                'reservation_id': reserve.reservation_id,
                'result': 'success',
                'status': 'pending',
            }
        ]
        recycle_jobs: list[dict] = []

        outcome = run_reservation_recycle_once(
            entitlement_service=self.service,
            pending_finalize_jobs=pending_finalize_jobs,
            recycle_jobs=recycle_jobs,
            now=self._now,
        )
        self.assertEqual(outcome['recycled'], 0)
        self.assertEqual(outcome['queued'], 0)
        self.assertEqual(self.service.get_reservation(reserve.reservation_id).status, 'reserved')
        counter = self.service.get_counter(user_id='u-recycle')
        self.assertIsNotNone(counter)
        self.assertEqual(counter.reserved, 1)

    def test_recycle_worker_retries_failures_before_dead_letter(self):
        from backend.auth_billing_service.workers.reservation_recycle_worker import run_reservation_recycle_once

        reserve = self.service.reserve(user_id='u-retry', mode='platform_key', request_id='req-retry')
        reservation = self.service.get_reservation(reserve.reservation_id)
        reservation.expires_at = self._now - timedelta(minutes=1)

        recycle_jobs: list[dict] = []
        attempts = {'count': 0}

        def _release(*, reservation_id: str):
            self.assertEqual(reservation_id, reserve.reservation_id)
            attempts['count'] += 1
            raise RuntimeError('release failed')

        for _ in range(5):
            run_reservation_recycle_once(
                entitlement_service=self.service,
                pending_finalize_jobs=[],
                recycle_jobs=recycle_jobs,
                now=self._now,
                release_func=_release,
            )
            self.assertEqual(len(recycle_jobs), 1)
            if recycle_jobs[0]['status'] != 'dead_letter':
                self._now = datetime.fromisoformat(recycle_jobs[0]['next_retry_at'])

        self.assertEqual(attempts['count'], 5)
        self.assertEqual(recycle_jobs[0]['status'], 'dead_letter')
        self.assertEqual(recycle_jobs[0]['retry_count'], 5)

    def test_recycle_worker_does_not_resurrect_dead_letter_job(self):
        from backend.auth_billing_service.workers.reservation_recycle_worker import run_reservation_recycle_once

        reserve = self.service.reserve(user_id='u-no-resurrect', mode='platform_key', request_id='req-no-resurrect')
        reservation = self.service.get_reservation(reserve.reservation_id)
        reservation.expires_at = self._now - timedelta(minutes=1)

        recycle_jobs: list[dict] = []
        attempts = {'count': 0}

        def _release(*, reservation_id: str):
            self.assertEqual(reservation_id, reserve.reservation_id)
            attempts['count'] += 1
            raise RuntimeError('release failed')

        for _ in range(5):
            run_reservation_recycle_once(
                entitlement_service=self.service,
                pending_finalize_jobs=[],
                recycle_jobs=recycle_jobs,
                now=self._now,
                release_func=_release,
            )
            if recycle_jobs[0]['status'] != 'dead_letter':
                self._now = datetime.fromisoformat(recycle_jobs[0]['next_retry_at'])

        self.assertEqual(recycle_jobs[0]['status'], 'dead_letter')
        self.assertEqual(recycle_jobs[0]['retry_count'], 5)
        self.assertEqual(attempts['count'], 5)

        outcome = run_reservation_recycle_once(
            entitlement_service=self.service,
            pending_finalize_jobs=[],
            recycle_jobs=recycle_jobs,
            now=self._now,
            release_func=_release,
        )

        self.assertEqual(outcome['queued'], 0)
        self.assertEqual(outcome['retried'], 0)
        self.assertEqual(outcome['dead_letter'], 0)
        self.assertEqual(outcome['recycled'], 0)
        self.assertEqual(attempts['count'], 5)
        self.assertEqual(len(recycle_jobs), 1)
        self.assertEqual(recycle_jobs[0]['status'], 'dead_letter')
        self.assertEqual(recycle_jobs[0]['retry_count'], 5)

    def test_finalize_retry_worker_handles_malformed_next_retry_at(self):
        from backend.auth_billing_service.workers.finalize_retry_worker import run_finalize_retry_once

        calls: list[str] = []

        def _finalize(**kwargs):
            calls.append(kwargs['reservation_id'])

        jobs = [
            {
                'reservation_id': 'rsv-malformed',
                'request_id': 'req-malformed',
                'user_id': 'u-malformed',
                'result': 'success',
                'idempotency_key': 'idem-malformed',
                'status': 'pending',
                'retry_count': 0,
                'next_retry_at': 'not-a-timestamp',
            },
            {
                'reservation_id': 'rsv-normal',
                'request_id': 'req-normal',
                'user_id': 'u-normal',
                'result': 'success',
                'idempotency_key': 'idem-normal',
                'status': 'pending',
                'retry_count': 0,
                'next_retry_at': self._now.isoformat(),
            },
        ]

        outcome = run_finalize_retry_once(jobs=jobs, finalize_func=_finalize, now=self._now)

        self.assertEqual(outcome['processed'], 2)
        self.assertEqual(outcome['done'], 2)
        self.assertEqual(set(calls), {'rsv-malformed', 'rsv-normal'})
        self.assertEqual(jobs[0]['status'], 'done')
        self.assertEqual(jobs[1]['status'], 'done')

    def test_recycle_worker_handles_malformed_next_retry_at(self):
        from backend.auth_billing_service.workers.reservation_recycle_worker import run_reservation_recycle_once

        reserve_bad = self.service.reserve(user_id='u-malformed', mode='platform_key', request_id='req-malformed')
        reservation_bad = self.service.get_reservation(reserve_bad.reservation_id)
        reservation_bad.expires_at = self._now - timedelta(minutes=1)

        reserve_ok = self.service.reserve(user_id='u-normal', mode='platform_key', request_id='req-normal')
        reservation_ok = self.service.get_reservation(reserve_ok.reservation_id)
        reservation_ok.expires_at = self._now - timedelta(minutes=1)

        recycle_jobs = [
            {
                'reservation_id': reserve_bad.reservation_id,
                'status': 'pending',
                'retry_count': 0,
                'next_retry_at': 'bad-time-value',
                'last_error': '',
            },
            {
                'reservation_id': reserve_ok.reservation_id,
                'status': 'pending',
                'retry_count': 0,
                'next_retry_at': self._now.isoformat(),
                'last_error': '',
            },
        ]
        calls: list[str] = []

        def _release(*, reservation_id: str):
            calls.append(reservation_id)
            return True

        outcome = run_reservation_recycle_once(
            entitlement_service=self.service,
            pending_finalize_jobs=[],
            recycle_jobs=recycle_jobs,
            now=self._now,
            release_func=_release,
        )

        self.assertEqual(outcome['recycled'], 2)
        self.assertEqual(outcome['queued'], 0)
        self.assertEqual(set(calls), {reserve_bad.reservation_id, reserve_ok.reservation_id})
        self.assertEqual(recycle_jobs[0]['status'], 'done')
        self.assertEqual(recycle_jobs[1]['status'], 'done')


class EntitlementObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_runtime_state_for_tests()
        auth_main._migration_service._owner_repository.create_owner_for_person_id('p1')
        self._old_secret = os.environ.get('AUTH_BILLING_SERVICE_SECRET')
        os.environ['AUTH_BILLING_SERVICE_SECRET'] = 'unit-test-secret'
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()

    def tearDown(self) -> None:
        self.client_cm.__exit__(None, None, None)
        if self._old_secret is None:
            os.environ.pop('AUTH_BILLING_SERVICE_SECRET', None)
        else:
            os.environ['AUTH_BILLING_SERVICE_SECRET'] = self._old_secret

    @staticmethod
    def _sign(
        *,
        action: str,
        user_id: str,
        request_id: str,
        reservation_id: str = '',
        idempotency_key: str = '',
        result: str = '',
    ) -> dict[str, str]:
        ts = str(int(time.time()))
        message = f'{action}|{user_id}|{request_id}|{reservation_id}|{idempotency_key}|{result}|{ts}'
        signature = hmac.new(b'unit-test-secret', message.encode('utf-8'), hashlib.sha256).hexdigest()
        return {
            'X-Auth-User-Id': user_id,
            'X-Service-Request-Id': request_id,
            'X-Service-Reservation-Id': reservation_id,
            'X-Service-Idempotency-Key': idempotency_key,
            'X-Service-Result': result,
            'X-Service-Timestamp': ts,
            'X-Service-Signature': signature,
        }

    def test_reserve_finalize_metrics_emitted(self):
        reserve_headers = self._sign(action='reserve', user_id='owner:p1', request_id='req-metrics')
        reserve_resp = self.client.post(
            '/entitlements/reserve',
            headers=reserve_headers,
            json={'mode': 'platform_key', 'request_id': 'req-metrics', 'person_id': 'p1'},
        )
        self.assertEqual(reserve_resp.status_code, 200)
        reservation_id = reserve_resp.json().get('reservation_id')
        self.assertTrue(reservation_id)

        finalize_headers = self._sign(
            action='finalize',
            user_id='owner:p1',
            request_id='req-metrics',
            reservation_id=reservation_id,
            idempotency_key='idem-metrics',
            result='success',
        )
        finalize_resp = self.client.post(
            '/entitlements/finalize',
            headers=finalize_headers,
            json={
                'reservation_id': reservation_id,
                'result': 'success',
                'idempotency_key': 'idem-metrics',
            },
        )
        self.assertEqual(finalize_resp.status_code, 200)

        snapshot = get_observability_snapshot_for_tests()
        self.assertGreaterEqual(snapshot['metrics']['reserve_total'], 1)
        self.assertGreaterEqual(snapshot['metrics']['finalize_total'], 1)
        self.assertGreaterEqual(snapshot['metrics']['finalize_success_total'], 1)
        self.assertGreaterEqual(snapshot['metrics']['finalize_success_rate_5m'], 0.99)
        self.assertNotIn('finalize_success_rate_below_threshold', snapshot['alerts'])

    def test_dead_letter_alert_triggered_after_max_retry(self):
        from backend.auth_billing_service.workers.finalize_retry_worker import run_finalize_retry_once
        from backend.auth_billing_service.workers.reservation_recycle_worker import run_reservation_recycle_once

        now = datetime(2026, 3, 15, 12, 0, tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)
        retry_jobs = [
            {
                'reservation_id': 'rsv-dead',
                'request_id': 'req-dead',
                'user_id': 'u-dead',
                'result': 'success',
                'idempotency_key': 'idem-dead',
                'status': 'pending',
                'retry_count': 0,
                'next_retry_at': now.isoformat(),
            }
        ]

        def _finalize_fail(**_kwargs):
            raise RuntimeError('finalize timeout')

        for _ in range(5):
            run_finalize_retry_once(jobs=retry_jobs, finalize_func=_finalize_fail, now=now)
            if retry_jobs[0]['status'] != 'dead_letter':
                now = datetime.fromisoformat(retry_jobs[0]['next_retry_at'])

        service = EntitlementService(now_provider=lambda: now)
        reserve = service.reserve(user_id='u-recycle-alert', mode='platform_key', request_id='req-recycle-alert')
        reservation = service.get_reservation(reserve.reservation_id)
        reservation.expires_at = now - timedelta(minutes=1)
        recycle_jobs: list[dict] = []

        def _release_fail(*, reservation_id: str):
            self.assertEqual(reservation_id, reserve.reservation_id)
            raise RuntimeError('release failed')

        for _ in range(5):
            run_reservation_recycle_once(
                entitlement_service=service,
                pending_finalize_jobs=[],
                recycle_jobs=recycle_jobs,
                now=now,
                release_func=_release_fail,
            )
            if recycle_jobs[0]['status'] != 'dead_letter':
                now = datetime.fromisoformat(recycle_jobs[0]['next_retry_at'])

        snapshot = get_observability_snapshot_for_tests()
        self.assertGreater(snapshot['metrics']['dead_letter_count'], 0)
        self.assertIn('dead_letter_detected', snapshot['alerts'])

    def test_invalid_signature_alert_triggers_over_threshold(self):
        for idx in range(6):
            response = self.client.post(
                '/entitlements/reserve',
                headers={
                    'X-Auth-User-Id': 'owner:p1',
                    'X-Service-Request-Id': f'req-invalid-{idx}',
                    'X-Service-Timestamp': str(int(time.time())),
                    'X-Service-Signature': 'bad-signature',
                },
                json={'mode': 'platform_key', 'request_id': f'req-invalid-{idx}', 'person_id': 'p1'},
            )
            self.assertEqual(response.status_code, 401)

        snapshot = get_observability_snapshot_for_tests()
        self.assertGreater(snapshot['metrics']['invalid_signature_count_5m'], 5)
        self.assertIn('invalid_signature_threshold_exceeded', snapshot['alerts'])


if __name__ == '__main__':
    unittest.main()
