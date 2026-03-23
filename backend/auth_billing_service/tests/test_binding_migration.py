import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.auth_billing_service import main as auth_main
from backend.auth_billing_service.services.migration_service import (
    InMemoryOwnerRepository,
    MigrationBootstrapError,
    MigrationService,
)


class BindingMigrationTests(unittest.TestCase):
    def _make_data_dir(self, person_ids: list[str]) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        data_dir = Path(temp_dir.name) / 'data'
        data_dir.mkdir(parents=True, exist_ok=True)
        persons_payload = {
            'active': person_ids[0] if person_ids else None,
            'persons': [{'id': person_id} for person_id in person_ids],
        }
        (data_dir / 'persons.json').write_text(json.dumps(persons_payload), encoding='utf-8')
        return data_dir

    def test_bootstrap_creates_one_owner_per_person_id(self):
        data_dir = self._make_data_dir(['alice', 'bob'])
        service = MigrationService(data_dir=data_dir, owner_repository=InMemoryOwnerRepository())

        result = service.bootstrap_owner_bindings()

        self.assertEqual(result.created_owners, 2)
        self.assertEqual(result.total_owners, 2)
        self.assertEqual(service.get_owner_id('alice'), 'owner:alice')
        self.assertEqual(service.get_owner_id('bob'), 'owner:bob')

    def test_bootstrap_is_idempotent_on_repeated_runs(self):
        data_dir = self._make_data_dir(['alice', 'bob'])
        service = MigrationService(data_dir=data_dir, owner_repository=InMemoryOwnerRepository())

        first = service.bootstrap_owner_bindings()
        second = service.bootstrap_owner_bindings()

        self.assertEqual(first.created_owners, 2)
        self.assertEqual(second.created_owners, 0)
        self.assertEqual(second.total_owners, 2)

    def test_startup_bootstrap_failure_blocks_startup_path(self):
        class _FailingMigrationService:
            def bootstrap_owner_bindings(self):
                raise MigrationBootstrapError('boom')

            def reset(self):
                return None

        original_service = auth_main._migration_service
        auth_main._migration_service = _FailingMigrationService()
        self.addCleanup(setattr, auth_main, '_migration_service', original_service)

        with self.assertRaises(RuntimeError):
            with TestClient(auth_main.app):
                pass


if __name__ == '__main__':
    unittest.main()
