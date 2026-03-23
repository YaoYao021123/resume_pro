from __future__ import annotations

import hashlib
import hmac
import os
import time
import unittest

from fastapi.testclient import TestClient

from backend.auth_billing_service.main import app, reset_runtime_state_for_tests
from web.server import _run_generate_with_entitlement


class GenerateEntitlementIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_runtime_state_for_tests()
        self._old_secret = os.environ.get('AUTH_BILLING_SERVICE_SECRET')
        os.environ['AUTH_BILLING_SERVICE_SECRET'] = 'unit-test-secret'

    def tearDown(self) -> None:
        if self._old_secret is None:
            os.environ.pop('AUTH_BILLING_SERVICE_SECRET', None)
        else:
            os.environ['AUTH_BILLING_SERVICE_SECRET'] = self._old_secret

    @staticmethod
    def _auth_headers(user_id: str = 'owner:p1') -> dict[str, str]:
        ts = str(int(time.time()))
        msg = f'auth|{user_id}|{ts}'
        sig = hmac.new(b'unit-test-secret', msg.encode('utf-8'), hashlib.sha256).hexdigest()
        return {
            'X-Auth-Validated': '1',
            'X-Auth-User-Id': user_id,
            'X-Auth-Timestamp': ts,
            'X-Auth-Signature': sig,
        }

    def test_generate_denied_when_reserve_rejects(self):
        generated = {'called': False}

        def _generate(*_args, **_kwargs):
            generated['called'] = True
            return {'success': True}

        status, body = _run_generate_with_entitlement(
            data={'jd': 'test jd', 'mode': 'platform_key'},
            headers=self._auth_headers(),
            active_person_id='p1',
            generate_func=_generate,
            reserve_func=lambda **_: {'allow': False, 'error_code': 'QUOTA_EXCEEDED_MONTHLY_FREE'},
            finalize_func=lambda **_: {'finalized': True},
            enqueue_func=lambda _: None,
            enforce_auth_billing=True,
        )

        self.assertEqual(status, 403)
        self.assertEqual(body.get('error_code'), 'QUOTA_EXCEEDED_MONTHLY_FREE')
        self.assertFalse(generated['called'])

    def test_finalize_timeout_enqueues_pending_finalize_job(self):
        pending: list[dict] = []

        status, body = _run_generate_with_entitlement(
            data={'jd': 'test jd', 'mode': 'platform_key'},
            headers=self._auth_headers(),
            active_person_id='p1',
            generate_func=lambda *_args, **_kwargs: {'success': True, 'output_dir': 'x'},
            reserve_func=lambda **_: {'allow': True, 'reservation_id': 'rsv-1'},
            finalize_func=lambda **_: (_ for _ in ()).throw(TimeoutError('finalize timeout')),
            enqueue_func=pending.append,
            enforce_auth_billing=True,
        )

        self.assertEqual(status, 200)
        self.assertTrue(body.get('success'))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].get('reservation_id'), 'rsv-1')
        self.assertEqual(pending[0].get('result'), 'success')

    def test_finalize_failure_enqueues_pending_finalize_job(self):
        pending: list[dict] = []

        status, body = _run_generate_with_entitlement(
            data={'jd': 'test jd', 'mode': 'platform_key'},
            headers=self._auth_headers(),
            active_person_id='p1',
            generate_func=lambda *_args, **_kwargs: {'success': True, 'output_dir': 'x'},
            reserve_func=lambda **_: {'allow': True, 'reservation_id': 'rsv-9'},
            finalize_func=lambda **_: (_ for _ in ()).throw(RuntimeError('finalize failed')),
            enqueue_func=pending.append,
            enforce_auth_billing=True,
        )

        self.assertEqual(status, 200)
        self.assertTrue(body.get('success'))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].get('reservation_id'), 'rsv-9')
        self.assertEqual(pending[0].get('result'), 'success')

    def test_generate_rejects_unbound_person_id(self):
        captured: dict[str, str] = {}

        def _reserve(**kwargs):
            captured['person_id'] = kwargs['person_id']
            return {'allow': False, 'error_code': 'PERSON_NOT_AUTHORIZED'}

        status, body = _run_generate_with_entitlement(
            data={'jd': 'test jd', 'mode': 'platform_key', 'person_id': 'other-person'},
            headers=self._auth_headers(),
            active_person_id='p1',
            generate_func=lambda *_args, **_kwargs: {'success': True},
            reserve_func=_reserve,
            finalize_func=lambda **_: {'finalized': True},
            enqueue_func=lambda _: None,
            enforce_auth_billing=True,
        )

        self.assertEqual(status, 403)
        self.assertEqual(body.get('error_code'), 'PERSON_NOT_AUTHORIZED')
        self.assertEqual(captured.get('person_id'), 'other-person')

    def test_generate_denied_when_reserve_times_out(self):
        generated = {'called': False}

        def _generate(*_args, **_kwargs):
            generated['called'] = True
            return {'success': True}

        status, body = _run_generate_with_entitlement(
            data={'jd': 'test jd', 'mode': 'platform_key'},
            headers=self._auth_headers(),
            active_person_id='p1',
            generate_func=_generate,
            reserve_func=lambda **_: (_ for _ in ()).throw(TimeoutError('reserve timeout')),
            finalize_func=lambda **_: {'finalized': True},
            enqueue_func=lambda _: None,
            enforce_auth_billing=True,
        )

        self.assertEqual(status, 503)
        self.assertFalse(generated['called'])
        self.assertEqual(body.get('error_code'), 'ENTITLEMENT_RESERVE_TIMEOUT')

    def test_body_user_id_is_ignored(self):
        captured: dict[str, str] = {}

        def _reserve(**kwargs):
            captured['user_id'] = kwargs['user_id']
            return {'allow': True, 'reservation_id': 'rsv-2'}

        status, body = _run_generate_with_entitlement(
            data={'jd': 'test jd', 'mode': 'platform_key', 'user_id': 'spoofed-user'},
            headers=self._auth_headers(),
            active_person_id='p1',
            generate_func=lambda *_args, **_kwargs: {'success': True},
            reserve_func=_reserve,
            finalize_func=lambda **_: {'finalized': True},
            enqueue_func=lambda _: None,
            enforce_auth_billing=True,
        )

        self.assertEqual(status, 200)
        self.assertTrue(body.get('success'))
        self.assertEqual(captured.get('user_id'), 'owner:p1')

    def test_generate_failure_triggers_finalize_fail(self):
        captured: list[dict] = []

        def _finalize(**kwargs):
            captured.append(kwargs)
            return {'finalized': True}

        status, body = _run_generate_with_entitlement(
            data={'jd': 'test jd', 'mode': 'platform_key'},
            headers=self._auth_headers(),
            active_person_id='p1',
            generate_func=lambda *_args, **_kwargs: {'success': False, 'error': 'mock failed'},
            reserve_func=lambda **_: {'allow': True, 'reservation_id': 'rsv-fail-1'},
            finalize_func=_finalize,
            enqueue_func=lambda _: None,
            enforce_auth_billing=True,
        )

        self.assertEqual(status, 200)
        self.assertFalse(body.get('success'))
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].get('result'), 'fail')

    def test_generate_rejects_invalid_auth_signature(self):
        status, body = _run_generate_with_entitlement(
            data={'jd': 'test jd', 'mode': 'platform_key'},
            headers={
                'X-Auth-Validated': '1',
                'X-Auth-User-Id': 'owner:p1',
                'X-Auth-Timestamp': str(int(time.time())),
                'X-Auth-Signature': 'invalid',
            },
            active_person_id='p1',
            generate_func=lambda *_args, **_kwargs: {'success': True},
            reserve_func=lambda **_: {'allow': True, 'reservation_id': 'rsv-1'},
            finalize_func=lambda **_: {'finalized': True},
            enqueue_func=lambda _: None,
            enforce_auth_billing=True,
        )

        self.assertEqual(status, 401)
        self.assertEqual(body.get('error_code'), 'AUTH_INVALID_SIGNATURE')


class EntitlementTrustContractTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_runtime_state_for_tests()
        self.client = TestClient(app)
        self._old_secret = os.environ.get('AUTH_BILLING_SERVICE_SECRET')
        os.environ['AUTH_BILLING_SERVICE_SECRET'] = 'unit-test-secret'

    def tearDown(self) -> None:
        if self._old_secret is None:
            os.environ.pop('AUTH_BILLING_SERVICE_SECRET', None)
        else:
            os.environ['AUTH_BILLING_SERVICE_SECRET'] = self._old_secret

    @staticmethod
    def _sign(action: str, user_id: str, request_id: str, reservation_id: str = '', idempotency_key: str = '', result: str = ''):
        ts = str(int(time.time()))
        message = f'{action}|{user_id}|{request_id}|{reservation_id}|{idempotency_key}|{result}|{ts}'
        sig = hmac.new(b'unit-test-secret', message.encode('utf-8'), hashlib.sha256).hexdigest()
        return {
            'X-Auth-User-Id': user_id,
            'X-Service-Request-Id': request_id,
            'X-Service-Reservation-Id': reservation_id,
            'X-Service-Idempotency-Key': idempotency_key,
            'X-Service-Result': result,
            'X-Service-Timestamp': ts,
            'X-Service-Signature': sig,
        }

    def test_entitlement_backend_rejects_missing_service_signature(self):
        resp = self.client.post(
            '/entitlements/reserve',
            json={'mode': 'platform_key', 'request_id': 'req-1', 'person_id': 'p1'},
        )
        self.assertEqual(resp.status_code, 401)

    def test_entitlement_backend_rejects_invalid_service_signature(self):
        headers = {
            'X-Auth-User-Id': 'owner:p1',
            'X-Service-Request-Id': 'req-2',
            'X-Service-Timestamp': str(int(time.time())),
            'X-Service-Signature': 'bad-signature',
        }
        resp = self.client.post(
            '/entitlements/reserve',
            headers=headers,
            json={'mode': 'platform_key', 'request_id': 'req-2', 'person_id': 'p1'},
        )
        self.assertEqual(resp.status_code, 401)

    def test_finalize_endpoint_rejects_missing_service_signature(self):
        resp = self.client.post(
            '/entitlements/finalize',
            json={'reservation_id': 'rsv-x', 'result': 'success', 'idempotency_key': 'idem-x'},
        )
        self.assertEqual(resp.status_code, 401)

    def test_finalize_endpoint_rejects_invalid_service_signature(self):
        headers = {
            'X-Auth-User-Id': 'owner:p1',
            'X-Service-Request-Id': 'req-3',
            'X-Service-Timestamp': str(int(time.time())),
            'X-Service-Signature': 'bad-signature',
        }
        resp = self.client.post(
            '/entitlements/finalize',
            headers=headers,
            json={'reservation_id': 'rsv-x', 'result': 'success', 'idempotency_key': 'idem-x'},
        )
        self.assertEqual(resp.status_code, 401)

    def test_reserve_endpoint_rejects_request_id_mismatch(self):
        headers = self._sign(action='reserve', user_id='owner:p1', request_id='req-header')
        resp = self.client.post(
            '/entitlements/reserve',
            headers=headers,
            json={'mode': 'platform_key', 'request_id': 'req-body', 'person_id': 'p1'},
        )
        self.assertEqual(resp.status_code, 401)

    def test_finalize_endpoint_rejects_payload_header_mismatch(self):
        headers = self._sign(
            action='finalize',
            user_id='owner:p1',
            request_id='req-3',
            reservation_id='rsv-x',
            idempotency_key='idem-x',
            result='success',
        )
        resp = self.client.post(
            '/entitlements/finalize',
            headers=headers,
            json={'reservation_id': 'rsv-x', 'result': 'fail', 'idempotency_key': 'idem-x', 'request_id': 'req-3'},
        )
        self.assertEqual(resp.status_code, 401)

    def test_entitlement_backend_requires_configured_secret(self):
        os.environ.pop('AUTH_BILLING_SERVICE_SECRET', None)
        headers = {
            'X-Auth-User-Id': 'owner:p1',
            'X-Service-Request-Id': 'req-4',
            'X-Service-Timestamp': str(int(time.time())),
            'X-Service-Signature': 'bad-signature',
        }
        resp = self.client.post(
            '/entitlements/reserve',
            headers=headers,
            json={'mode': 'platform_key', 'request_id': 'req-4', 'person_id': 'p1'},
        )
        self.assertEqual(resp.status_code, 500)


if __name__ == '__main__':
    unittest.main()
