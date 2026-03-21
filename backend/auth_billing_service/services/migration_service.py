from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MigrationBootstrapError(RuntimeError):
    pass


@dataclass
class OwnerBinding:
    owner_id: str
    person_id: str
    created_at: datetime = field(default_factory=utcnow)


class OwnerRepository(Protocol):
    def get_owner_id_by_person_id(self, person_id: str) -> str | None:
        ...

    def create_owner_for_person_id(self, person_id: str) -> str:
        ...

    def count(self) -> int:
        ...

    def reset(self) -> None:
        ...


class InMemoryOwnerRepository:
    def __init__(self) -> None:
        self._owners_by_person_id: dict[str, OwnerBinding] = {}

    def get_owner_id_by_person_id(self, person_id: str) -> str | None:
        binding = self._owners_by_person_id.get(person_id)
        return binding.owner_id if binding else None

    def create_owner_for_person_id(self, person_id: str) -> str:
        existing = self._owners_by_person_id.get(person_id)
        if existing:
            return existing.owner_id

        owner_id = f'owner:{person_id}'
        self._owners_by_person_id[person_id] = OwnerBinding(owner_id=owner_id, person_id=person_id)
        return owner_id

    def count(self) -> int:
        return len(self._owners_by_person_id)

    def reset(self) -> None:
        self._owners_by_person_id.clear()


@dataclass(frozen=True)
class BootstrapResult:
    discovered_person_ids: tuple[str, ...]
    created_owners: int
    total_owners: int


class MigrationService:
    def __init__(self, data_dir: Path | str, owner_repository: OwnerRepository) -> None:
        self._data_dir = Path(data_dir)
        self._owner_repository = owner_repository

    def bootstrap_owner_bindings(self) -> BootstrapResult:
        person_ids = self._load_person_ids()
        created_owners = 0
        for person_id in person_ids:
            if self._owner_repository.get_owner_id_by_person_id(person_id) is None:
                self._owner_repository.create_owner_for_person_id(person_id)
                created_owners += 1

        return BootstrapResult(
            discovered_person_ids=tuple(person_ids),
            created_owners=created_owners,
            total_owners=self._owner_repository.count(),
        )

    def get_owner_id(self, person_id: str) -> str | None:
        return self._owner_repository.get_owner_id_by_person_id(person_id)

    def reset(self) -> None:
        self._owner_repository.reset()

    def _load_person_ids(self) -> list[str]:
        persons_path = self._data_dir / 'persons.json'
        if not persons_path.exists():
            return []

        try:
            payload = json.loads(persons_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationBootstrapError(f'failed to read persons.json: {exc}') from exc

        if not isinstance(payload, dict):
            raise MigrationBootstrapError('invalid persons.json payload')

        raw_persons = payload.get('persons', [])
        if not isinstance(raw_persons, list):
            raise MigrationBootstrapError('invalid persons list in persons.json')

        person_ids: list[str] = []
        seen: set[str] = set()
        for item in raw_persons:
            if not isinstance(item, dict):
                continue
            person_id = item.get('id')
            if not isinstance(person_id, str) or not person_id:
                continue
            if person_id in seen:
                continue
            seen.add(person_id)
            person_ids.append(person_id)
        return person_ids
