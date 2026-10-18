"""Small local SQLite journal; persist dispatch intent before any external write."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from agentic_incident_remediation.actions.models import RunRecord


class RunStore:
    def __init__(self, path: Path, repository: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("run journal must not be a symbolic link")
        self.path = path
        self.repository = repository
        with self._connect() as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS runs "
                "(id TEXT PRIMARY KEY, repository TEXT NOT NULL, payload TEXT NOT NULL)"
            )
        path.chmod(0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def list(self) -> tuple[RunRecord, ...]:
        with self._connect() as database:
            rows = database.execute(
                "SELECT payload FROM runs WHERE repository=? ORDER BY rowid DESC",
                (self.repository,),
            ).fetchall()
        return tuple(RunRecord.model_validate_json(row[0]) for row in rows)

    def get(self, identifier: UUID) -> RunRecord:
        with self._connect() as database:
            row = database.execute(
                "SELECT payload FROM runs WHERE id=? AND repository=?",
                (str(identifier), self.repository),
            ).fetchone()
        if row is None:
            raise LookupError("run is not in this dashboard's local history")
        return RunRecord.model_validate_json(row[0])

    def save(self, record: RunRecord) -> None:
        if record.repository != self.repository:
            raise ValueError("run belongs to a different repository")
        with self._connect() as database:
            database.execute(
                "INSERT INTO runs VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload "
                "WHERE runs.repository=excluded.repository",
                (str(record.id), record.repository, record.model_dump_json()),
            )

    def begin(self, record: RunRecord) -> tuple[RunRecord, bool]:
        """Serialize consent intents across threads/processes, including ambiguous dispatches."""

        if record.repository != self.repository:
            raise ValueError("run belongs to a different repository")
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute(
                "SELECT payload FROM runs WHERE repository=?", (self.repository,)
            ).fetchall()
            existing = [RunRecord.model_validate_json(row[0]) for row in rows]
            for item in existing:
                if item.id == record.id:
                    return item, False
            if any(item.active for item in existing):
                raise ValueError("an active or uncertain dispatch already exists; inspect it first")
            database.execute(
                "INSERT INTO runs VALUES (?, ?, ?)",
                (str(record.id), record.repository, record.model_dump_json()),
            )
        return record, True
