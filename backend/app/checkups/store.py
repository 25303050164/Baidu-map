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
from typing import Callable
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
CREATE TABLE IF NOT EXISTS facility_extensions (
    extension_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    categories TEXT NOT NULL,
    budget INTEGER NOT NULL,
    status TEXT NOT NULL,
    stage TEXT,
    requests INTEGER NOT NULL DEFAULT 0,
    network_requests INTEGER NOT NULL DEFAULT 0,
    document TEXT,
    counts_by_category TEXT,
    facilities_status TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL,
    intent TEXT,
    stop_reason TEXT,
    initial_plan TEXT,
    UNIQUE (task_id, client_request_id)
);
"""
#: Columns added after the first release, with their declarations. A store
#: created before them is migrated in place; existing rows read them as NULL,
#: which the view reports as "not recorded" rather than inventing a time.
ADDED_COLUMNS = (("stage_started_at", "REAL"), ("activity_at", "REAL"), ("progress", "TEXT"))
#: 补查表同理：早于它的库按原样读回，不编造缺的字段。``intent`` 记的是客户端当时声明的
#: 请求身份（类别与是否显式给了预算）；旧行没有它，按旧版的解析结果判定，见
#: :func:`extension_matches`。``stop_reason``/``initial_plan`` 是定稿时冻结的诊断：
#: 界面读状态就能看到停在哪、缓存帮了多少，不必再取结果文件。
EXTENSION_ADDED_COLUMNS = (("facilities_status", "TEXT"), ("intent", "TEXT"),
                           ("stop_reason", "TEXT"), ("initial_plan", "TEXT"),
                           ("kind", "TEXT"))
#: 这一行是哪一种工作。``extension``：按需补查，**不**改父任务的报告与修订。
#: ``retry``：§5 B2 决策 1 的重试，它**要**为同一次体检发布新修订。
#: 两者共用同一张表是有意的：幂等键、409 冲突、串行队列、取消与终态语义
#: 是同一套规则，各写一遍才是真正会分叉的地方。区别只在定稿之后做什么。
#: 旧行为 NULL，按 ``extension`` 读 —— 那正是它们被创建时的语义。
EXTENSION_KINDS = frozenset({"extension", "retry"})
#: What ``update`` may write. ``progress`` is the worker's in-stage step (a JSON
#: object); ``stage_started_at`` and ``activity_at`` are maintained here, never
#: passed in.
UPDATABLE = frozenset({"status", "stage", "business_status", "requests", "network_requests",
                       "started_at", "finished_at", "cancel_requested", "error", "progress"})
#: 补查行可以改的字段。``document`` 是结果文件的相对路径，写入即意味着这次补查定稿。
EXTENSION_UPDATABLE = frozenset({"status", "stage", "requests", "network_requests",
                                 "document", "counts_by_category", "facilities_status",
                                 "error", "finished_at", "stop_reason", "initial_plan"})


class RequestIdConflict(Exception):
    """Same client request id, different inputs."""


class TaskNotFound(LookupError):
    """Unknown or pruned task id."""


class ExtensionNotFound(LookupError):
    """Unknown补查 id，或它不属于这个任务。"""


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


@dataclass(frozen=True)
class ExtensionRecord:
    """一次按需补查的行：它自己的预算、状态和结果文件。"""
    extension_id: str
    task_id: str
    client_request_id: str
    fingerprint: str
    base_revision: int
    categories: list[str]
    budget: int
    status: str
    stage: str | None
    requests: int
    network_requests: int
    document_path: str | None
    counts_by_category: dict
    facilities_status: str | None
    error: str | None
    created_at: float
    updated_at: float
    finished_at: float | None
    #: 客户端当时声明的身份（``categories`` 与 ``maxPoiRequests``）。旧行没有它，
    #: 读作 None —— 那意味着"这一行只冻了当时解析出来的预算"。
    intent: dict | None = None
    #: 定稿时冻结的停止原因与首轮估算：它们是读这次补查时必须知道的事。
    stop_reason: str | None = None
    initial_plan: dict | None = None
    #: ``extension``（补查，不动父任务报告）或 ``retry``（重试，为父任务发布新修订）。
    #: 旧行读作 ``extension``。
    kind: str = "extension"

    @property
    def declared_max_poi_requests(self) -> int | None:
        """客户端显式给出的预算，或 None：它当时把预算留给了部署默认值。"""
        return None if self.intent is None else self.intent.get('maxPoiRequests')


def _extension_record(row) -> ExtensionRecord:
    return ExtensionRecord(
        extension_id=row["extension_id"], task_id=row["task_id"],
        client_request_id=row["client_request_id"], fingerprint=row["fingerprint"],
        base_revision=row["base_revision"], categories=json.loads(row["categories"]),
        budget=row["budget"], status=row["status"], stage=row["stage"],
        requests=row["requests"], network_requests=row["network_requests"],
        document_path=row["document"], error=row["error"], created_at=row["created_at"],
        updated_at=row["updated_at"], finished_at=row["finished_at"],
        facilities_status=row["facilities_status"],
        intent=(None if row["intent"] is None else json.loads(row["intent"])),
        stop_reason=row["stop_reason"],
        initial_plan=(None if row["initial_plan"] is None else json.loads(row["initial_plan"])),
        # 旧行没有这一列，读作 ``extension``：那正是它们被创建时的语义，"补查不改父任务报告"。
        kind=row["kind"] or "extension",
        counts_by_category=({} if not row["counts_by_category"]
                            else json.loads(row["counts_by_category"])))


def extension_identity(*, categories, max_poi_requests) -> dict:
    """What the client asked for, as an identity nothing else may widen.

    It is deliberately the *declared* request and not what the deployment resolved
    it to: a retry that leaves the budget out must stay the same request after the
    default, the boundary geometry or the day's balance has moved. Only the two
    things that change what is being asked for take part.
    """
    return {"categories": sorted(set(categories)), "maxPoiRequests": max_poi_requests}


def extension_matches(record: ExtensionRecord, *, identity: dict,
                      resolved_budget: int | Callable[[], int]) -> bool:
    """Whether ``identity`` is the request ``record`` was created from.

    The stored row keeps the client's own declaration, so a repeat matches on it
    directly. Two equivalences are preserved rather than invented:

    * A request that left the budget out resolves to the deployment default, so it
      is the same request as one that named exactly that number — in either
      direction, which is what the older release compared.
    * A row written before ``intent`` existed froze only its resolved budget, so it
      is judged the way it was created: by the number this request resolves to now.

    ``resolved_budget`` is therefore only ever consulted for those two cases, and
    never for two rows that both declared what they wanted. It may be a callable so a
    caller can pass something expensive — the deployment's current default, which needs
    the parent's boundary — without reading it to answer a question that does not use it.
    """
    def resolved() -> int:
        return resolved_budget() if callable(resolved_budget) else resolved_budget

    if sorted(set(identity["categories"])) != sorted(set(record.categories)):
        return False
    declared_now = identity["maxPoiRequests"]
    if record.intent is None:
        return resolved() == record.budget
    declared_then = record.declared_max_poi_requests
    if declared_now is None:
        # Omitted: it resolves to the deployment default, so it is the same request
        # as one that named exactly that number — and only that number. A row that
        # declared its own budget is not matched just because the omission happens
        # to resolve to the same figure it froze.
        return declared_then is None or resolved() == declared_then
    if declared_then is None:
        # The row left the budget out, so it froze the default it applied: naming
        # that number is the same request, naming another one is not.
        return declared_now == record.budget
    return declared_now == declared_then


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
        """Create the tables and add any later column. Changes no task's state."""
        with self._connection() as connection:
            connection.executescript(SCHEMA)
            present = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            for name, declaration in ADDED_COLUMNS:
                if name not in present:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
            extension_columns = {row["name"] for row in
                                 connection.execute("PRAGMA table_info(facility_extensions)")}
            for name, declaration in EXTENSION_ADDED_COLUMNS:
                if name not in extension_columns:
                    connection.execute(
                        f"ALTER TABLE facility_extensions ADD COLUMN {name} {declaration}")

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
            # 补查走同一条重启语义：没跑完的补查不会自己续上，也不会重放任何已付费的请求。
            # 它不动原任务，所以原体检的报告仍然可以照常查看。
            connection.execute(
                "UPDATE facility_extensions SET status='failed', stage=NULL, finished_at=?,"
                " error='interrupted_by_restart', updated_at=?"
                " WHERE status IN ('queued','running')", (time.time(), time.time()))
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

    # -- 按需补查 -----------------------------------------------------------

    def create_extension(self, *, extension_id: str, task_id: str, client_request_id: str,
                         identity: dict, fingerprint: str, base_revision: int, categories, budget: int,
                         kind: str = "extension"):
        """Insert once per (task, client request id); identical repeats return the same row.

        幂等键按任务隔离：两个任务里的同一个客户端请求标识是两次不同的补查。

        The check happens inside the same ``BEGIN IMMEDIATE`` transaction as the
        insert, so two callers racing on one request id cannot both create a row:
        the loser reads the winner's row back and is told it did not create
        anything, and only a creator is enqueued. ``fingerprint`` is kept as the
        audit value of the declared identity; the decision itself is
        :func:`extension_matches`, which is the one rule both callers share.
        """
        now = time.time()
        encoded = json.dumps(identity, sort_keys=True, ensure_ascii=False)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM facility_extensions WHERE task_id=? AND client_request_id=?",
                (task_id, client_request_id)).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                # ``budget`` here is the resolution the caller already computed for this
                # request; the comparison consults it only when the stored row cannot
                # answer the question on its own declaration alone.
                if not extension_matches(_extension_record(existing), identity=identity,
                                         resolved_budget=budget):
                    raise RequestIdConflict(client_request_id)
                return _extension_record(existing), False
            connection.execute(
                "INSERT INTO facility_extensions (extension_id, task_id, client_request_id,"
                " fingerprint, base_revision, categories, budget, status, created_at, updated_at,"
                " intent, kind) VALUES (?,?,?,?,?,?,?,'queued',?,?,?,?)",
                (extension_id, task_id, client_request_id, fingerprint, base_revision,
                 json.dumps(list(categories), ensure_ascii=False), budget, now, now, encoded, kind))
            connection.execute("COMMIT")
            return self._extension(extension_id), True

    def find_extension(self, task_id: str, client_request_id: str) -> ExtensionRecord | None:
        """The row this request id already names, if any. Read-only, no side effects.

        A lookup and not a create: answering a repeat must not enqueue, reserve or
        run anything, and it must not need the parent's boundary or the day's
        balance to have been consulted first.
        """
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM facility_extensions WHERE task_id=? AND client_request_id=?",
                (task_id, client_request_id)).fetchone()
        return None if row is None else _extension_record(row)

    def _extension(self, extension_id: str) -> ExtensionRecord:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM facility_extensions WHERE extension_id=?",
                                     (extension_id,)).fetchone()
        if row is None:
            raise ExtensionNotFound(extension_id)
        return _extension_record(row)

    def extension(self, extension_id: str) -> ExtensionRecord:
        return self._extension(extension_id)

    def extensions_of(self, task_id: str) -> list[ExtensionRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM facility_extensions WHERE task_id=? ORDER BY created_at,"
                " extension_id", (task_id,)).fetchall()
        return [_extension_record(row) for row in rows]

    def claim_extension(self, extension_id: str) -> bool:
        """queued -> running 恰好一次，所以两个 worker 不会同时开始同一次补查。"""
        now = time.time()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE facility_extensions SET status='running', stage='poi', updated_at=?"
                " WHERE extension_id=? AND status='queued'", (now, extension_id))
            return cursor.rowcount == 1

    def update_extension(self, extension_id: str, **fields) -> ExtensionRecord:
        unknown = set(fields) - EXTENSION_UPDATABLE
        if unknown:
            raise ValueError(f"unknown extension fields: {sorted(unknown)}")
        if not fields:
            return self._extension(extension_id)
        if "counts_by_category" in fields and fields["counts_by_category"] is not None:
            fields["counts_by_category"] = json.dumps(fields["counts_by_category"],
                                                      ensure_ascii=False, sort_keys=True)
        assignments = [f"{name}=?" for name in fields]
        with self._connection() as connection:
            connection.execute(
                f"UPDATE facility_extensions SET {', '.join(assignments)}, updated_at=?"
                " WHERE extension_id=?", (*fields.values(), time.time(), extension_id))
        return self._extension(extension_id)

    def finish_extension(self, extension_id: str, *, status: str, document_path: str | None,
                         counts_by_category: dict | None = None, requests: int = 0,
                         network_requests: int = 0, facilities_status: str | None = None,
                         error: str | None = None, stop_reason: str | None = None,
                         initial_plan: dict | None = None) -> ExtensionRecord:
        """定稿一次补查。``document_path`` 在行上出现就意味着结果已经落到磁盘。"""
        return self.update_extension(
            extension_id, status=status, stage="ready" if document_path else None,
            document=document_path, counts_by_category=counts_by_category,
            requests=requests, network_requests=network_requests,
            facilities_status=facilities_status, error=error, finished_at=time.time(),
            stop_reason=stop_reason,
            initial_plan=(None if initial_plan is None
                          else json.dumps(initial_plan, ensure_ascii=False, sort_keys=True)))

    def extension_document(self, extension_id: str) -> dict | None:
        record = self._extension(extension_id)
        if record.document_path is None:
            return None
        return json.loads((self.root / record.document_path).read_text("utf-8"))

    def publish_extension_document(self, extension_id: str, task_id: str, document: dict) -> str:
        """先把结果写到磁盘，再由调用方把它记进行里 —— 与修订同序，半写不入索引。"""
        relative = Path("tasks") / task_id / f"extension-{extension_id}.json"
        atomic_dump(self.root / relative, document)
        return relative.as_posix()
