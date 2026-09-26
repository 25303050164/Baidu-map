"""Durable checkup task metadata and the immutable revision index.

Metadata, idempotency keys and consumption counters live in SQLite; published
revisions are atomic JSON files, so a partially written revision can never
replace a frozen one. A restart marks unfinished tasks interrupted and keeps
every revision they already published; it never resumes paid requests.
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..persistence import atomic_dump

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    client_request_id TEXT NOT NULL UNIQUE,
    engine TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT,
    business_status TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    budget INTEGER NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    network_requests INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS revisions (
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    stage TEXT NOT NULL,
    created_at REAL NOT NULL,
    result_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (task_id, revision)
);
"""


class RequestIdConflict(Exception):
    """Same client request id, different inputs."""


class TaskNotFound(LookupError):
    """Unknown or pruned task id."""


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    client_request_id: str
    engine: str
    fingerprint: str
    payload: dict
    status: str
    stage: str | None
    business_status: str | None
    revision: int
    budget: int
    requests: int
    network_requests: int
    created_at: float
    updated_at: float
    started_at: float | None
    finished_at: float | None
    cancel_requested: bool
    error: str | None


def _record(row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"], client_request_id=row["client_request_id"], engine=row["engine"],
        fingerprint=row["fingerprint"], payload=json.loads(row["payload"]), status=row["status"],
        stage=row["stage"], business_status=row["business_status"], revision=row["revision"],
        budget=row["budget"], requests=row["requests"], network_requests=row["network_requests"],
        created_at=row["created_at"], updated_at=row["updated_at"], started_at=row["started_at"],
        finished_at=row["finished_at"], cancel_requested=bool(row["cancel_requested"]),
        error=row["error"])


class CheckupStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "checkups.sqlite3"

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    def initialize(self) -> int:
        """The whole of a restart: create the schema, then sweep unfinished tasks.

        Returns the number of tasks marked interrupted.
        """
        self.create_schema()
        return self.interrupt_unfinished()

    def create_schema(self) -> None:
        """Create the tables. Runs anywhere, changes no task's state."""
        with self._connection() as connection:
            connection.executescript(SCHEMA)

    def interrupt_unfinished(self) -> int:
        """Fail every queued/running/cancelling task as ``interrupted_by_restart``.

        Returns how many were marked. Cancelled or completed tasks are left
        untouched, and no paid request is ever replayed.

        This is a **serving** startup step, not an import-time one: importing the
        app object is not an event in the deployment's history. Anything that
        imports ``app.main`` for its own reasons -- ``tools/export_contract.py``
        builds the OpenAPI out of it -- would otherwise reach into the live store
        and kill whatever checkup is running at that moment.
        """
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status='failed', stage=NULL, finished_at=?,"
                " error='interrupted_by_restart', updated_at=?"
                " WHERE status IN ('queued','running','cancelling')",
                (time.time(), time.time()))
            return cursor.rowcount

    def artifact_dir(self, task_id: str) -> Path:
        directory = self.root / "tasks" / task_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def create(self, *, task_id: str, client_request_id: str, engine: str,
               fingerprint: str, payload: dict, budget: int) -> tuple[TaskRecord, bool]:
        """Insert once per client request id; identical repeats return the same task."""
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM tasks WHERE client_request_id=?", (client_request_id,)).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                if existing["fingerprint"] != fingerprint:
                    raise RequestIdConflict(client_request_id)
                return _record(existing), False
            connection.execute(
                "INSERT INTO tasks (task_id, client_request_id, engine, fingerprint, payload,"
                " status, stage, revision, budget, created_at, updated_at)"
                " VALUES (?,?,?,?,?,'queued',NULL,0,?,?,?)",
                (task_id, client_request_id, engine, fingerprint,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), budget, now, now))
            connection.execute("COMMIT")
            return self._get(task_id), True

    def _get(self, task_id: str) -> TaskRecord:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return _record(row)

    def get(self, task_id: str) -> TaskRecord:
        return self._get(task_id)

    def by_request(self, client_request_id: str) -> TaskRecord:
        with self._connection() as connection:
            row = connection.execute("SELECT task_id FROM tasks WHERE client_request_id=?",
                                     (client_request_id,)).fetchone()
        if row is None:
            raise TaskNotFound(client_request_id)
        return self._get(row["task_id"])

    def next_queued(self) -> TaskRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT task_id FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
        return self._get(row["task_id"]) if row else None

    def claim(self, task_id: str) -> bool:
        """Move queued -> running exactly once, so two workers cannot both start it."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status='running', stage='isochrone', started_at=?, updated_at=?"
                " WHERE task_id=? AND status='queued'", (time.time(), time.time(), task_id))
            return cursor.rowcount == 1

    def update(self, task_id: str, **fields) -> TaskRecord:
        allowed = {"status", "stage", "business_status", "requests", "network_requests",
                   "started_at", "finished_at", "cancel_requested", "error"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")
        if not fields:
            return self._get(task_id)
        assignments = ", ".join(f"{name}=?" for name in fields)
        values = [int(value) if isinstance(value, bool) else value for value in fields.values()]
        with self._connection() as connection:
            connection.execute(
                f"UPDATE tasks SET {assignments}, updated_at=? WHERE task_id=?",
                (*values, time.time(), task_id))
        return self._get(task_id)

    def publish(self, task_id: str, *, stage: str, snapshot: dict, result_hash: str) -> int:
        """Freeze one revision. The payload is durable before its index row exists."""
        record = self._get(task_id)
        revision = record.revision + 1
        relative = Path("tasks") / task_id / f"revision-{revision:04d}-{stage}.json"
        atomic_dump(self.root / relative, snapshot)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO revisions (task_id, revision, stage, created_at, result_hash, payload)"
                " VALUES (?,?,?,?,?,?)",
                (task_id, revision, stage, time.time(), result_hash, str(relative.as_posix())))
            connection.execute("UPDATE tasks SET revision=?, stage=?, updated_at=? WHERE task_id=?",
                               (revision, stage, time.time(), task_id))
            connection.execute("COMMIT")
        return revision

    def revisions(self, task_id: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT revision, stage, created_at, result_hash, payload FROM revisions"
                " WHERE task_id=? ORDER BY revision", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def revision(self, task_id: str, revision: int | None = None) -> dict | None:
        """Latest revision by default. Raises for an unknown task id."""
        self._get(task_id)
        with self._connection() as connection:
            if revision is None:
                row = connection.execute(
                    "SELECT revision, stage, created_at, result_hash, payload FROM revisions"
                    " WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
            else:
                row = connection.execute(
                    "SELECT revision, stage, created_at, result_hash, payload FROM revisions"
                    " WHERE task_id=? AND revision=?", (task_id, revision)).fetchone()
        if row is None:
            return None
        return {**dict(row), "snapshot": json.loads((self.root / row["payload"]).read_text("utf-8"))}
