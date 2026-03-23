from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.auth_billing_service import main as auth_main
from backend.auth_billing_service.main import app, reset_runtime_state_for_tests
from web.server import _run_generate_with_entitlement
from tools.model_config import get_model_config


class ByokApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_runtime_state_for_tests()
        self._old_service_secret = os.environ.get('AUTH_BILLING_SERVICE_SECRET')
        self._old_byok_secret = os.environ.get('AUTH_BILLING_BYOK_SECRET')
        os.environ['AUTH_BILLING_SERVICE_SECRET'] = 'unit-test-service-secret'
        os.environ['AUTH_BILLING_BYOK_SECRET'] = 'unit-test-byok-secret-1234'
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def tearDown(self) -> None:
        if self._old_service_secret is None:
            os.environ.pop('AUTH_BILLING_SERVICE_SECRET', None)
        else:
            os.environ['AUTH_BILLING_SERVICE_SECRET'] = self._old_service_secret
        if self._old_byok_secret is None:
            os.environ.pop('AUTH_BILLING_BYOK_SECRET', None)
        else:
            os.environ['AUTH_BILLING_BYOK_SECRET'] = self._old_byok_secret

    def _auth_headers(self, *, user_id: str = 'user-1', valid: bool = True) -> dict[str, str]:
        ts = str(int(time.time()))
        signature = 'invalid-signature'
        if valid:
            payload = f'auth|{user_id}|{ts}'
            signature = hmac.new(
                os.environ['AUTH_BILLING_SERVICE_SECRET'].encode('utf-8'),
                payload.encode('utf-8'),
                hashlib.sha256,
            ).hexdigest()
        return {
            'X-Auth-Validated': '1',
            'X-Auth-User-Id': user_id,
            'X-Auth-Timestamp': ts,
            'X-Auth-Signature': signature,
        }

    def _upsert(
        self,
        *,
        auth_user_id: str = 'user-1',
        user_id: str = 'user-1',
        provider: str = 'openai',
        api_key: str,
        headers: dict[str, str] | None = None,
    ):
        return self.client.post(
            '/byok/upsert',
            json={
                'user_id': user_id,
                'provider': provider,
                'api_key': api_key,
            },
            headers=headers if headers is not None else self._auth_headers(user_id=auth_user_id),
        )

    def _get(
        self,
        *,
        auth_user_id: str = 'user-1',
        user_id: str = 'user-1',
        provider: str = 'openai',
        headers: dict[str, str] | None = None,
    ):
        return self.client.get(
            f'/byok/{provider}',
            params={'user_id': user_id},
            headers=headers if headers is not None else self._auth_headers(user_id=auth_user_id),
        )

    def _delete(
        self,
        *,
        auth_user_id: str = 'user-1',
        user_id: str = 'user-1',
        provider: str = 'openai',
        headers: dict[str, str] | None = None,
    ):
        return self.client.delete(
            f'/byok/{provider}',
            params={'user_id': user_id},
            headers=headers if headers is not None else self._auth_headers(user_id=auth_user_id),
        )

    def _assert_byok_invalid(self, resp) -> None:
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body.get('detail', {}).get('error_code'), 'BYOK_INVALID')

    def test_byok_requires_auth_context(self):
        resp = self._upsert(user_id='spoofed', api_key='sk-live-auth-required-123456', headers={})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json().get('detail'), 'AUTH_REQUIRED')

    def test_byok_rejects_invalid_auth_signature(self):
        resp = self._upsert(
            auth_user_id='u-auth',
            user_id='spoofed',
            api_key='sk-live-auth-invalid-123456',
            headers=self._auth_headers(user_id='u-auth', valid=False),
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json().get('detail'), 'AUTH_INVALID_SIGNATURE')

    def test_cross_user_access_is_rejected(self):
        upsert = self._upsert(auth_user_id='user-a', user_id='user-a', provider='openai', api_key='sk-user-a-1234567890abcd')
        foreign_get = self._get(auth_user_id='user-b', user_id='user-a', provider='openai')
        foreign_delete = self._delete(auth_user_id='user-b', user_id='user-a', provider='openai')
        owner_get = self._get(auth_user_id='user-a', user_id='user-a', provider='openai')

        self.assertEqual(upsert.status_code, 200)
        self.assertEqual(foreign_get.status_code, 200)
        self.assertEqual(foreign_get.json().get('user_id'), 'user-b')
        self.assertFalse(foreign_get.json().get('has_active_key'))

        self.assertEqual(foreign_delete.status_code, 200)
        self.assertEqual(foreign_delete.json().get('user_id'), 'user-b')
        self.assertFalse(foreign_delete.json().get('deleted'))

        self.assertEqual(owner_get.status_code, 200)
        self.assertEqual(owner_get.json().get('user_id'), 'user-a')
        self.assertTrue(owner_get.json().get('has_active_key'))

    def test_client_supplied_user_id_is_ignored(self):
        upsert = self._upsert(
            auth_user_id='owner-1',
            user_id='spoofed-user-id',
            provider='openai',
            api_key='sk-live-owner-1234567890abcd',
        )
        current = self._get(auth_user_id='owner-1', user_id='some-other-user', provider='openai')

        self.assertEqual(upsert.status_code, 200)
        self.assertEqual(current.status_code, 200)
        self.assertEqual(upsert.json().get('user_id'), 'owner-1')
        self.assertEqual(current.json().get('user_id'), 'owner-1')
        self.assertTrue(current.json().get('has_active_key'))

    def test_upsert_byok_replaces_active_key(self):
        first_key = 'sk-live-first-1234567890'
        second_key = 'sk-live-second-0987654321'

        first = self._upsert(auth_user_id='u-replace', user_id='spoof-1', provider='openai', api_key=first_key)
        second = self._upsert(auth_user_id='u-replace', user_id='spoof-2', provider='openai', api_key=second_key)
        current = self._get(auth_user_id='u-replace', user_id='spoof-3', provider='openai')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(current.status_code, 200)

        body = current.json()
        self.assertTrue(body.get('has_active_key'))
        self.assertEqual(body.get('provider'), 'openai')
        self.assertEqual(body.get('user_id'), 'u-replace')
        self.assertNotEqual(first.json().get('fingerprint'), second.json().get('fingerprint'))
        self.assertEqual(second.json().get('fingerprint'), body.get('fingerprint'))

    def test_delete_byok_deactivates_key(self):
        upsert = self._upsert(auth_user_id='u-delete', user_id='spoofed', provider='openai', api_key='sk-delete-1234567890')
        deleted = self._delete(auth_user_id='u-delete', user_id='spoofed', provider='openai')
        current = self._get(auth_user_id='u-delete', user_id='spoofed', provider='openai')

        self.assertEqual(upsert.status_code, 200)
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json().get('deleted'))
        self.assertEqual(current.status_code, 200)
        self.assertFalse(current.json().get('has_active_key'))

    def test_precedence_request_key_over_stored_active_key(self):
        stored_key = 'sk-stored-abcdef1234567890'
        request_key = 'sk-request-fedcba0987654321'
        upsert = self._upsert(auth_user_id='u-precedence', user_id='spoofed', provider='openai', api_key=stored_key)
        self.assertEqual(upsert.status_code, 200)

        from backend.auth_billing_service.services.byok_service import ByokService
        service = ByokService(encryption_secret='unit-test-byok-secret-1234')
        service.upsert(user_id='u-precedence', provider='openai', api_key=stored_key)
        resolved = service.resolve_effective_config(
            user_id='u-precedence',
            provider='openai',
            request_key=request_key,
        )
        fallback = service.resolve_effective_config(
            user_id='u-precedence',
            provider='openai',
            request_key=None,
        )

        self.assertEqual(resolved.source, 'request_key')
        self.assertEqual(resolved.api_key, request_key)
        self.assertEqual(fallback.source, 'stored_byok')
        self.assertEqual(fallback.api_key, stored_key)

    def test_api_never_returns_plaintext_key(self):
        plain = 'sk-very-secret-1234567890-abcdef'

        upsert = self._upsert(auth_user_id='u-plain', user_id='spoofed', provider='openai', api_key=plain)
        current = self._get(auth_user_id='u-plain', user_id='spoofed', provider='openai')

        self.assertEqual(upsert.status_code, 200)
        self.assertEqual(current.status_code, 200)

        for payload in (upsert.json(), current.json()):
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(plain, serialized)
            self.assertNotIn('encrypted_key', payload)
            self.assertNotIn('api_key', payload)
            self.assertTrue(payload.get('masked_key'))
            self.assertTrue(payload.get('fingerprint'))

    def test_empty_key_rejected_byok_invalid(self):
        resp = self._upsert(auth_user_id='u-invalid', user_id='spoofed', provider='openai', api_key='   ')
        self._assert_byok_invalid(resp)

    def test_provider_allowlist_enforced(self):
        resp = self._upsert(
            auth_user_id='u-invalid',
            user_id='spoofed',
            provider='unknown_vendor',
            api_key='sk-valid-1234567890',
        )
        self._assert_byok_invalid(resp)

    def test_key_length_and_charset_validation(self):
        too_short = self._upsert(auth_user_id='u-invalid', user_id='spoofed', provider='openai', api_key='short-key')
        bad_charset = self._upsert(
            auth_user_id='u-invalid',
            user_id='spoofed',
            provider='openai',
            api_key='sk-valid-12345678\nwith-newline',
        )

        self._assert_byok_invalid(too_short)
        self._assert_byok_invalid(bad_charset)

    def test_generate_resume_accepts_request_level_ai_config(self):
        from tools.generate_resume import generate_resume

        override = {
            'enabled': True,
            'provider': 'openai',
            'model': 'gpt-5-mini',
            'base_url': 'https://override.invalid/v1',
            'api_key': 'sk-override-1234567890abcd',
            'api_style': 'openai',
            'supports_json_object': True,
            'supports_thinking_off': False,
        }

        with patch('tools.generate_resume._maybe_migrate'), \
             patch('tools.generate_resume._profile_setup_error', return_value=None), \
             patch('tools.generate_resume._experiences_setup_error', return_value=None), \
             patch('tools.generate_resume._parse_profile', return_value={'name_zh': '测试用户', 'projects': [], 'awards': []}), \
             patch('tools.generate_resume.load_all_experiences', return_value=[]), \
             patch('tools.generate_resume.extract_jd_keywords', return_value={'tech': [], 'domain': [], 'skill': [], 'company': '', 'role': ''}), \
             patch('tools.generate_resume._apply_experience_selection_rules', return_value=[]), \
             patch('tools.generate_resume._filter_projects', return_value=[]), \
             patch('tools.generate_resume._filter_awards', return_value=[]), \
             patch('tools.generate_resume.get_model_config', return_value={
                 'enabled': False,
                 'provider': 'gemini',
                 'model': 'gemini-3-flash-preview',
                 'base_url': 'https://global.invalid',
                 'api_key': 'sk-global-should-not-be-used',
                 'api_style': 'openai',
                 'supports_json_object': True,
                 'supports_thinking_off': False,
             }) as get_model_config_mock, \
             patch('tools.generate_resume._call_ai_resume_planner', side_effect=RuntimeError('forced planner failure')) as planner_mock:
            result = generate_resume(
                '测试 JD',
                company='测试公司',
                role='测试岗位',
                person_id='p1',
                prefer_ai=True,
                ai_config_override=override,
            )

        self.assertFalse(result.get('success'))
        planner_mock.assert_called_once()
        used_config = planner_mock.call_args.args[0]
        self.assertEqual(used_config.get('api_key'), override['api_key'])
        self.assertEqual(used_config.get('model'), override['model'])
        self.assertEqual(used_config.get('provider'), override['provider'])
        get_model_config_mock.assert_not_called()

    def test_byok_api_key_not_logged_in_plaintext(self):
        from tools import gen_log
        from tools.generate_resume import generate_resume

        raw_key = 'sk-plain-should-never-appear-1234567890'
        expected_fingerprint = hashlib.sha256(raw_key.encode('utf-8')).hexdigest()[:12]
        expected_masked = f'{raw_key[:4]}{"*" * (len(raw_key) - 8)}{raw_key[-4:]}'
        override = {
            'enabled': True,
            'provider': 'openai',
            'model': 'gpt-5-mini',
            'base_url': 'https://override.invalid/v1',
            'api_key': raw_key,
            'api_style': 'openai',
            'supports_json_object': True,
            'supports_thinking_off': False,
        }

        gen_log.clear()
        with patch('tools.generate_resume._maybe_migrate'), \
             patch('tools.generate_resume._profile_setup_error', return_value=None), \
             patch('tools.generate_resume._experiences_setup_error', return_value=None), \
             patch('tools.generate_resume._parse_profile', return_value={'name_zh': '测试用户', 'projects': [], 'awards': []}), \
             patch('tools.generate_resume.load_all_experiences', return_value=[]), \
             patch('tools.generate_resume.extract_jd_keywords', return_value={'tech': [], 'domain': [], 'skill': [], 'company': '', 'role': ''}), \
             patch('tools.generate_resume._apply_experience_selection_rules', return_value=[]), \
             patch('tools.generate_resume._filter_projects', return_value=[]), \
             patch('tools.generate_resume._filter_awards', return_value=[]), \
             patch(
                 'tools.generate_resume._call_ai_resume_planner',
                 side_effect=RuntimeError(f'provider rejected key={raw_key}'),
             ):
            result = generate_resume(
                '测试 JD',
                company='测试公司',
                role='测试岗位',
                person_id='p1',
                prefer_ai=True,
                ai_config_override=override,
            )

        self.assertFalse(result.get('success'))
        merged = json.dumps({'result': result, 'logs': gen_log.get_all()}, ensure_ascii=False)
        self.assertNotIn(raw_key, merged)
        self.assertIn(expected_fingerprint, merged)
        self.assertIn(expected_masked, merged)

    def test_byok_generation_skips_reserve_finalize_calls(self):
        calls = {'reserve': 0, 'finalize': 0}
        captured_kwargs: dict = {}

        def _reserve(**_kwargs):
            calls['reserve'] += 1
            return {'allow': True, 'reservation_id': 'rsv-should-not-exist'}

        def _finalize(**_kwargs):
            calls['finalize'] += 1
            return {'finalized': True}

        def _generate(*_args, **kwargs):
            captured_kwargs.update(kwargs)
            return {'success': True, 'output_dir': 'x'}

        byok_payload = {
            'provider': 'openai',
            'model': 'gpt-5-mini',
            'api_key': 'sk-byok-request-override-123456',
        }
        status, body = _run_generate_with_entitlement(
            data={'jd': 'test jd', 'mode': 'byok', 'byok': byok_payload},
            headers=self._auth_headers(user_id='owner:p1'),
            active_person_id='p1',
            generate_func=_generate,
            reserve_func=_reserve,
            finalize_func=_finalize,
            enqueue_func=lambda _: None,
            enforce_auth_billing=True,
        )

        self.assertEqual(status, 200)
        self.assertTrue(body.get('success'))
        self.assertEqual(calls['reserve'], 0)
        self.assertEqual(calls['finalize'], 0)
        self.assertIn('ai_config_override', captured_kwargs)
        self.assertEqual(captured_kwargs['ai_config_override'].get('api_key'), byok_payload['api_key'])
        self.assertEqual(captured_kwargs['ai_config_override'].get('provider'), byok_payload['provider'])
        self.assertEqual(captured_kwargs['ai_config_override'].get('model'), byok_payload['model'])

    def test_byok_generation_does_not_write_usage_counter(self):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        from backend.auth_billing_service.services.entitlement_service import EntitlementService

        now = datetime(2026, 3, 15, 12, 0, tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)
        entitlement = EntitlementService(now_provider=lambda: now)

        def _reserve(**kwargs):
            decision = entitlement.reserve(
                user_id=kwargs['user_id'],
                mode='platform_key',
                request_id=kwargs['request_id'],
            )
            return {
                'allow': decision.allow,
                'reservation_id': decision.reservation_id,
                'error_code': decision.error_code,
            }

        status, body = _run_generate_with_entitlement(
            data={
                'jd': 'test jd',
                'mode': 'byok',
                'byok': {
                    'provider': 'openai',
                    'model': 'gpt-5-mini',
                    'api_key': 'sk-byok-nocounter-1234567890',
                },
            },
            headers=self._auth_headers(user_id='owner:p1'),
            active_person_id='p1',
            generate_func=lambda *_args, **_kwargs: {'success': True, 'output_dir': 'x'},
            reserve_func=_reserve,
            finalize_func=lambda **_kwargs: {'finalized': True},
            enqueue_func=lambda _: None,
            enforce_auth_billing=True,
        )

        self.assertEqual(status, 200)
        self.assertTrue(body.get('success'))
        self.assertIsNone(entitlement.get_counter(user_id='owner:p1', mode='platform_key'))


    def test_byok_generate_invalid_provider_rejected_with_byok_invalid(self):
        called = {'generate': 0}

        def _generate(*_args, **_kwargs):
            called['generate'] += 1
            return {'success': True, 'output_dir': 'x'}

        status, body = _run_generate_with_entitlement(
            data={
                'jd': 'test jd',
                'mode': 'byok',
                'byok': {
                    'provider': 'unknown_vendor',
                    'model': 'gpt-5-mini',
                    'api_key': 'sk-byok-invalid-provider-1234567890',
                },
            },
            headers=self._auth_headers(user_id='owner:p1'),
            active_person_id='p1',
            generate_func=_generate,
            reserve_func=lambda **_kwargs: {'allow': True, 'reservation_id': 'rsv-unused'},
            finalize_func=lambda **_kwargs: {'finalized': True},
            enqueue_func=lambda _job: None,
            enforce_auth_billing=True,
        )

        self.assertEqual(status, 400)
        self.assertEqual(body.get('error_code'), 'BYOK_INVALID')
        self.assertEqual(called['generate'], 0)

    def test_byok_generate_invalid_key_rejected_with_byok_invalid(self):
        called = {'generate': 0}

        def _generate(*_args, **_kwargs):
            called['generate'] += 1
            return {'success': True, 'output_dir': 'x'}

        status, body = _run_generate_with_entitlement(
            data={
                'jd': 'test jd',
                'mode': 'byok',
                'byok': {
                    'provider': 'openai',
                    'model': 'gpt-5-mini',
                    'api_key': 'short-key',
                },
            },
            headers=self._auth_headers(user_id='owner:p1'),
            active_person_id='p1',
            generate_func=_generate,
            reserve_func=lambda **_kwargs: {'allow': True, 'reservation_id': 'rsv-unused'},
            finalize_func=lambda **_kwargs: {'finalized': True},
            enqueue_func=lambda _job: None,
            enforce_auth_billing=True,
        )

        self.assertEqual(status, 400)
        self.assertEqual(body.get('error_code'), 'BYOK_INVALID')
        self.assertEqual(called['generate'], 0)

    def test_byok_generate_ignores_request_base_url_override(self):
        captured_kwargs: dict = {}

        def _generate(*_args, **kwargs):
            captured_kwargs.update(kwargs)
            return {'success': True, 'output_dir': 'x'}

        providers = {
            str(item.get('id', '')).strip().lower(): item
            for item in get_model_config().get('providers', [])
            if isinstance(item, dict)
        }
        expected_base_url = str(providers['openai'].get('default_base_url', '')).strip()

        status, body = _run_generate_with_entitlement(
            data={
                'jd': 'test jd',
                'mode': 'byok',
                'byok': {
                    'provider': 'openai',
                    'model': 'gpt-5-mini',
                    'api_key': 'sk-byok-valid-baseurl-test-123456',
                    'base_url': 'https://malicious.example/v1',
                },
            },
            headers=self._auth_headers(user_id='owner:p1'),
            active_person_id='p1',
            generate_func=_generate,
            reserve_func=lambda **_kwargs: {'allow': True, 'reservation_id': 'rsv-unused'},
            finalize_func=lambda **_kwargs: {'finalized': True},
            enqueue_func=lambda _job: None,
            enforce_auth_billing=True,
        )

        self.assertEqual(status, 200)
        self.assertTrue(body.get('success'))
        self.assertIn('ai_config_override', captured_kwargs)
        self.assertEqual(captured_kwargs['ai_config_override'].get('base_url'), expected_base_url)


if __name__ == '__main__':
    unittest.main()
