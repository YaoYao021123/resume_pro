import unittest

from fastapi.testclient import TestClient

from backend.auth_billing_service.main import app, reset_runtime_state_for_tests
from backend.auth_billing_service.services.auth_service import AuthService


class AuthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_runtime_state_for_tests()
        self.client = TestClient(app)
        self._seq = 0

    def test_health_endpoint_returns_ok(self):
        resp = self.client.get('/health')
        self.assertEqual(resp.status_code, 200)

    def _next_email(self) -> str:
        self._seq += 1
        return f'user{self._seq}@example.com'

    def _next_phone(self) -> str:
        self._seq += 1
        return f'1380000{self._seq:04d}'

    def _login(self, target: str, channel: str = 'email') -> dict:
        send_resp = self.client.post('/auth/send-code', json={'channel': channel, 'target': target})
        self.assertEqual(send_resp.status_code, 200)
        login_resp = self.client.post(
            '/auth/login',
            json={'channel': channel, 'target': target, 'code': '000000'},
        )
        self.assertEqual(login_resp.status_code, 200)
        return login_resp.json()

    def test_send_code_accepts_email_and_phone(self):
        email_resp = self.client.post(
            '/auth/send-code',
            json={'channel': 'email', 'target': self._next_email()},
        )
        phone_resp = self.client.post(
            '/auth/send-code',
            json={'channel': 'phone', 'target': self._next_phone()},
        )
        self.assertEqual(email_resp.status_code, 200)
        self.assertEqual(phone_resp.status_code, 200)

    def test_login_returns_400_for_invalid_target(self):
        login_resp = self.client.post(
            '/auth/login',
            json={'channel': 'email', 'target': 'bad-email', 'code': '000000'},
        )
        self.assertEqual(login_resp.status_code, 400)

    def test_send_code_returns_explicit_backend_and_applies_throttling(self):
        target = self._next_email()
        first = self.client.post('/auth/send-code', json={'channel': 'email', 'target': target})
        second = self.client.post('/auth/send-code', json={'channel': 'email', 'target': target})
        self.assertEqual(first.status_code, 200)
        self.assertIn(first.json().get('verification_backend'), {'memory', 'redis'})
        self.assertEqual(second.status_code, 429)

    def test_auth_service_falls_back_to_memory_when_redis_unavailable(self):
        service = AuthService(redis_url='redis://127.0.0.1:0/0')
        self.assertEqual(service.verification_backend, 'memory')

    def test_login_returns_access_token_and_refresh_token(self):
        body = self._login(target=self._next_email())
        self.assertIn('access_token', body)
        self.assertIn('refresh_token', body)
        self.assertIn('user', body)

    def test_refresh_rotates_refresh_token(self):
        login_body = self._login(target=self._next_email())
        old_refresh = login_body['refresh_token']

        refresh_resp = self.client.post('/auth/refresh', json={'refresh_token': old_refresh})
        self.assertEqual(refresh_resp.status_code, 200)
        self.assertNotEqual(refresh_resp.json().get('refresh_token'), old_refresh)

        old_again = self.client.post('/auth/refresh', json={'refresh_token': old_refresh})
        self.assertEqual(old_again.status_code, 401)

    def test_login_verification_code_is_single_use(self):
        target = self._next_email()
        self.client.post('/auth/send-code', json={'channel': 'email', 'target': target})
        first = self.client.post('/auth/login', json={'channel': 'email', 'target': target, 'code': '000000'})
        second = self.client.post('/auth/login', json={'channel': 'email', 'target': target, 'code': '000000'})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 401)

    def test_logout_revokes_refresh_token(self):
        login_body = self._login(target=self._next_email())
        refresh_token = login_body['refresh_token']

        logout_resp = self.client.post('/auth/logout', json={'refresh_token': refresh_token})
        self.assertEqual(logout_resp.status_code, 200)

        refresh_resp = self.client.post('/auth/refresh', json={'refresh_token': refresh_token})
        self.assertEqual(refresh_resp.status_code, 401)

    def test_enforces_max_three_active_sessions_per_user(self):
        target = self._next_email()
        self.client.post('/auth/send-code', json={'channel': 'email', 'target': target})
        tokens = []
        for _ in range(4):
            login_resp = self.client.post(
                '/auth/login',
                json={'channel': 'email', 'target': target, 'code': '000000'},
            )
            self.assertEqual(login_resp.status_code, 200)
            tokens.append(login_resp.json()['refresh_token'])

        oldest_refresh = self.client.post('/auth/refresh', json={'refresh_token': tokens[0]})
        newest_refresh = self.client.post('/auth/refresh', json={'refresh_token': tokens[-1]})
        self.assertEqual(oldest_refresh.status_code, 401)
        self.assertEqual(newest_refresh.status_code, 200)


if __name__ == '__main__':
    unittest.main()
