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
from .rounds import ROUND_SCHEMA, RoundStore

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
    error TEXT,
    stage_started_at REAL,
    activity_at REAL,
    progress TEXT
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
#: Columns added after the first release, with their declarations. A store
#: created before them is migrated in place; existing rows read them as NULL,
#: which the view reports as "not recorded" rather than inventing a time.
ADDED_COLUMNS = (("stage_started_at", "REAL"), ("activity_at", "REAL"), ("progress", "TEXT"))
#: What ``update`` may write. ``progress`` is the worker's in-stage step (a JSON
#: object); ``stage_started_at`` and ``activity_at`` are maintained here, never
#: passed in.
UPDATABLE = frozenset({"status", "stage", "business_status", "requests", "network_requests",
                       "started_at", "finished_at", "cancel_requested", "error", "progress"})


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
    #: When the current stage began, on the same wall clock as ``started_at``.
    stage_started_at: float | None = None
    #: The last time the worker wrote anything about this task: a counter, a step,
    #: a revision or a state change. A client's cancel request is not activity.
    activity_at: float | None = None
    #: The worker's current in-stage step, as written; None before the first one.
    progress: dict | None = None


def _record(row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"], client_request_id=row["client_request_id"], engine=row["engine"],
        fingerprint=row["fingerprint"], payload=json.loads(row["payload"]), status=row["status"],
        stage=row["stage"], business_status=row["business_status"], revision=row["revision"],
        budget=row["budget"], requests=row["requests"], network_requests=row["network_requests"],
        created_at=row["created_at"], updated_at=row["updated_at"], started_at=row["started_at"],
        finished_at=row["finished_at"], cancel_requested=bool(row["cancel_requested"]),
        error=row["error"], stage_started_at=row["stage_started_at"],
        activity_at=row["activity_at"],
        progress=None if row["progress"] is None else json.loads(row["progress"]))


class CheckupStore(RoundStore):
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "checkups.sqlite3"
        self._keeper = None

    def close(self):
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
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
        """Create the tables and add any later column. Changes no task's state."""
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            # Keep WAL alive between short per-operation connections. Closing the
            # last connection otherwise checkpoints and removes WAL on every page.
            if self._keeper is None:
                self._keeper = sqlite3.connect(self.path, check_same_thread=False,
                                               isolation_level=None)
            connection.executescript(SCHEMA)
            connection.executescript(ROUND_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            round_columns = {row["name"] for row in connection.execute("PRAGMA table_info(checkup_rounds)")}
            if "poi_limit" not in round_columns:
                connection.execute("ALTER TABLE checkup_rounds ADD COLUMN poi_limit INTEGER NOT NULL DEFAULT 60")
                # Old continuation rounds always used 60. Initial rounds could
                # explicitly lower it; missing request fields meant 60 then.
                for row in connection.execute("SELECT r.task_id,t.payload FROM checkup_rounds r "
                                              "JOIN tasks t ON t.task_id=r.task_id WHERE r.number=1").fetchall():
                    facilities = json.loads(row["payload"]).get("facilities", {})
                    limit = facilities.get("max_poi_requests", facilities.get("maxPoiRequests", 60))
                    connection.execute("UPDATE checkup_rounds SET poi_limit=? WHERE task_id=? AND number=1",
                                       (limit, row["task_id"]))
            connection.execute("COMMIT")
            present = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            for name, declaration in ADDED_COLUMNS:
                if name not in present:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")

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
            if connection.execute('SELECT 1 FROM checkup_rounds WHERE request_id=?',
                                  (client_request_id,)).fetchone():
                raise RequestIdConflict(client_request_id)
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
            continuation = self.round_request(client_request_id)
            if continuation is not None:
                return self._get(continuation['task_id'])
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
        now = time.time()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status='running', stage='isochrone', started_at=?,"
                " stage_started_at=?, activity_at=?, progress=NULL, updated_at=?"
                " WHERE task_id=? AND status='queued'", (now, now, now, now, task_id))
            return cursor.rowcount == 1

    def update(self, task_id: str, *, activity: bool = True, **fields) -> TaskRecord:
        """Write the named fields; a stage that changes restarts its clock and step.

        ``activity`` is False for writes that are not the worker's doing -- a
        client asking to cancel -- so "the server last did something at" stays
        a statement about the computation, not about who called in.
        """
        unknown = set(fields) - UPDATABLE
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")
        if not fields:
            return self._get(task_id)
        now = time.time()
        if "progress" in fields and fields["progress"] is not None:
            fields["progress"] = json.dumps(fields["progress"], ensure_ascii=False, sort_keys=True)
        assignments = [f"{name}=?" for name in fields]
        values = [int(value) if isinstance(value, bool) else value for value in fields.values()]
        if "stage" in fields:
            # SQLite evaluates every right-hand side against the old row, so the
            # comparison below sees the stage being left, not the one written.
            assignments.append("stage_started_at=CASE WHEN stage IS ? THEN stage_started_at ELSE ? END")
            values += [fields["stage"], now]
            if "progress" not in fields:
                assignments.append("progress=CASE WHEN stage IS ? THEN progress ELSE NULL END")
                values.append(fields["stage"])
        if activity:
            assignments.append("activity_at=?")
            values.append(now)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE tasks SET {', '.join(assignments)}, updated_at=? WHERE task_id=?",
                (*values, now, task_id))
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
            now = time.time()
            connection.execute(
                "UPDATE tasks SET revision=?, stage=?, updated_at=?, activity_at=?,"
                " stage_started_at=CASE WHEN stage IS ? THEN stage_started_at ELSE ? END"
                " WHERE task_id=?", (revision, stage, now, now, stage, now, task_id))
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
