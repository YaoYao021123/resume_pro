import unittest

from fastapi.testclient import TestClient

from backend.auth_billing_service.main import app


class AuthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_endpoint_returns_ok(self):
        resp = self.client.get('/health')
        self.assertEqual(resp.status_code, 200)

    def test_auth_routes_not_ready_yet(self):
        resp = self.client.post(
            '/auth/login',
            json={'channel': 'email', 'target': 'a@b.com', 'code': '000000'},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json().get('detail'), 'auth routes are not ready yet')


if __name__ == '__main__':
    unittest.main()
