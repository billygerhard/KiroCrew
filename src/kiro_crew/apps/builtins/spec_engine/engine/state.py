"""SQLite state store for the spec engine.

Everything the engine remembers about a spec — the spec registry, phase
approvals, runs, the at-most-once claim ledger, the watched-item lifecycle
snapshot, the workspace ledger, and the arrival-ordered dispatch queue — lives in
one SQLite database under the app's data directory. None of it lives in a spec
directory.

That separation is the whole point of this module. ``<project>/.kiro/specs/<name>/``
is a contract shared with the Kiro IDE and CLI: it holds the native documents
plus the ``.config.kiro`` sidecar and nothing else. Engine bookkeeping written
there would be read by tools that know nothing about it, so this module refuses
to resolve any state path inside a ``.kiro/specs`` tree, and a write that cannot
land raises :class:`StatePersistenceError` rather than falling back to some
other location. A caller that cannot persist must fail its operation: recording
run state in a spec document would break interop silently and permanently,
which is worse than a loud failure.

State-changing operations on one spec serialize through that spec's lock row. A
second concurrent writer is rejected with :class:`SpecLocked`, which carries the
spec's current state, so the loser can reconcile against what actually happened
instead of retrying blind.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

#: App directory name under ``<data home>/apps/``. Working name; the final app
#: name needs sign-off, and renaming it moves the state root.
APP_NAME = "spec-engine"

#: Database file name inside the state root.
DB_FILENAME = "state.db"

#: Path segments that identify a spec directory tree, relative to a project
#: root. Engine state may never be resolved inside one.
SPEC_TREE_SEGMENTS = (".kiro", "specs")

#: How long a connection waits for another writer's transaction before giving
#: up. Long enough to absorb a competing short transaction, short enough that a
#: wedged writer surfaces as an error rather than an indefinite hang.
BUSY_TIMEOUT_S = 10.0

#: Default lifetime of a per-spec lock. A holder that crashes mid-operation
#: leaves its row behind, so the lock expires rather than wedging the spec
#: forever; operations longer than this refresh it explicitly.
DEFAULT_LOCK_TTL_S = 300.0

#: Bytes of randomness in a lock token. The token is what proves a release or a
#: guarded write belongs to the acquirer and not to whoever took the lock after
#: it expired.
LOCK_TOKEN_BYTES = 16

#: Runs included in a state snapshot, most recently updated first. A snapshot
#: exists to let a rejected caller reconcile, not to page a spec's whole history.
SNAPSHOT_RUN_LIMIT = 20

#: Length of the project fingerprint inside a spec key. 48 bits of SHA-256 over
#: the resolved project path: the key has to be one column, and the project path
#: is too long and too punctuation-heavy to concatenate safely.
PROJECT_FINGERPRINT_CHARS = 12

#: Claim kinds. One ledger serves both at-most-once needs, because both are the
#: same question ("has this exact delivery already happened?") asked of
#: different subjects.
#:
#: ``dispatch``  scope = watch source, subject = item identifier,
#:               generation = the item's lifecycle generation. A reopened item
#:               is a new generation, so it dispatches again; a re-poll of the
#:               same generation does not.
#: ``writeback`` scope = run identifier, subject = lifecycle event name. A
#:               resumed run or a retried tick cannot comment twice.
CLAIM_DISPATCH = "dispatch"
CLAIM_WRITEBACK = "writeback"

#: Schema version recorded in ``schema_meta``. Bump only alongside a migration.
SCHEMA_VERSION = 3

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # A specs row may exist before the documents do: a run takes the spec's lock
    # before creating anything, so spec_type stays NULL until it is recorded.
    # `phase` is a cache of a derivation that always reads the disk, never an
    # authority — nothing may advance a spec by writing this column.
    """
    CREATE TABLE IF NOT EXISTS specs (
        spec_key          TEXT PRIMARY KEY,
        project           TEXT NOT NULL,
        name              TEXT NOT NULL,
        spec_type         TEXT,
        phase             TEXT,
        archived          INTEGER NOT NULL DEFAULT 0,
        created_ts        TEXT NOT NULL,
        updated_ts        TEXT NOT NULL,
        lock_owner        TEXT,
        lock_token        TEXT,
        lock_acquired_ts  TEXT,
        lock_expires_epoch REAL,
        UNIQUE (project, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approvals (
        spec_key    TEXT NOT NULL,
        gate        TEXT NOT NULL,
        actor       TEXT NOT NULL,
        approved_ts TEXT NOT NULL,
        doc_hash    TEXT NOT NULL,
        stale       INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (spec_key, gate)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id       TEXT PRIMARY KEY,
        spec_key     TEXT NOT NULL,
        source       TEXT,
        item_id      TEXT,
        state        TEXT NOT NULL,
        posture      TEXT,
        cost_credits REAL NOT NULL DEFAULT 0,
        created_ts   TEXT NOT NULL,
        updated_ts   TEXT NOT NULL,
        detail       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS runs_spec_idx ON runs (spec_key, state)",
    """
    CREATE TABLE IF NOT EXISTS claims (
        kind       TEXT NOT NULL,
        scope      TEXT NOT NULL,
        subject    TEXT NOT NULL,
        generation TEXT NOT NULL DEFAULT '',
        run_id     TEXT,
        claimed_ts TEXT NOT NULL,
        PRIMARY KEY (kind, scope, subject, generation)
    )
    """,
    # The lifecycle snapshot every poll is compared against: one row per item a
    # source has ever reported, holding the generation that item is currently on.
    #
    # A row outlives the item being closed, and outlives the source stopping to
    # report it at all, which is why nothing here is ever deleted by a poll. The
    # generation is part of the dispatch claim key, so forgetting a closed item
    # would restart it at the first generation — whose claim is already held —
    # and its reopen would then be silently dropped instead of dispatched.
    #
    # `is_open` is the lifecycle authority, not `item_state`. The state text is
    # whatever the tracker printed, kept for display and diagnosis; an item the
    # source stopped reporting has no state text at all.
    #
    # `content_digest` is a hash over the fields that would change what a run was
    # given (title, body, classification), so a poll can tell an edited item from
    # an untouched one even though neither moved its lifecycle position. It is a
    # digest, not the text: the body is attacker-controlled untrusted data, and a
    # second copy of it in the state store is a second surface to leak. An empty
    # digest means "unknown" -- a row written before digests were stored, or one
    # an upgrade migrated in -- and reads as not-edited, so no pre-existing row
    # reports a spurious edit on the first poll after an upgrade.
    """
    CREATE TABLE IF NOT EXISTS watch_items (
        source         TEXT NOT NULL,
        item_id        TEXT NOT NULL,
        generation     INTEGER NOT NULL DEFAULT 1,
        item_state     TEXT NOT NULL DEFAULT '',
        is_open        INTEGER NOT NULL DEFAULT 1,
        content_digest TEXT NOT NULL DEFAULT '',
        first_seen_ts  TEXT NOT NULL,
        observed_ts    TEXT NOT NULL,
        PRIMARY KEY (source, item_id)
    )
    """,
    # One row per content element, keyed by the scope that consumed it, holding
    # the revision its class was derived from. The revision is in the row rather
    # than only the class because the question this table answers is not "what
    # class did this author have" but "is the class on file still about the text
    # we are holding" -- an edited comment keeps its id and its author.
    #
    # `kind` is part of the key, not a payload column. An element id is unique
    # only within its kind, so keying on the id alone would let a comment's row
    # overwrite its item body's and then report the survivor as unchanged.
    # `pending_reapply` outlives the observation that set it: a caller that sees a
    # changed element owes a re-application of every decision gated on the class,
    # and that obligation must survive a crash between noticing and doing it.
    """
    CREATE TABLE IF NOT EXISTS element_trust (
        scope           TEXT NOT NULL,
        kind            TEXT NOT NULL,
        element_id      TEXT NOT NULL,
        author          TEXT NOT NULL DEFAULT '',
        revision        TEXT NOT NULL,
        class_name      TEXT NOT NULL,
        evidence        TEXT NOT NULL,
        pending_reapply INTEGER NOT NULL DEFAULT 0,
        derived_ts      TEXT NOT NULL,
        PRIMARY KEY (scope, kind, element_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workspaces (
        workspace_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id       TEXT NOT NULL,
        kind         TEXT NOT NULL,
        location     TEXT NOT NULL,
        address      TEXT,
        disposable   INTEGER NOT NULL DEFAULT 1,
        cleaned      INTEGER NOT NULL DEFAULT 0,
        created_ts   TEXT NOT NULL,
        cleaned_ts   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS workspaces_run_idx ON workspaces (run_id, cleaned)",
    # seq is the arrival order: capacity frees in the order items showed up, so
    # the queue is read by ascending seq and never by timestamp (two items can
    # share a timestamp).
    """
    CREATE TABLE IF NOT EXISTS queue (
        seq         INTEGER PRIMARY KEY AUTOINCREMENT,
        source      TEXT NOT NULL,
        project     TEXT NOT NULL,
        item_id     TEXT NOT NULL,
        generation  TEXT NOT NULL DEFAULT '',
        payload     TEXT,
        enqueued_ts TEXT NOT NULL,
        dequeued_ts TEXT,
        UNIQUE (source, item_id, generation)
    )
    """,
)


class StateError(Exception):
    """Base class for state-store failures."""


class StatePersistenceError(StateError):
    """Engine state could not be persisted outside the spec documents.

    Raised instead of writing state anywhere else. Callers must fail the
    operation they were performing and report the reason.
    """


class SpecLocked(StateError):
    """A conflicting concurrent writer already holds the spec's lock.

    Carries the spec's current state so the rejected caller can reconcile
    instead of guessing what the winner did.
    """

    def __init__(self, ref: "SpecRef", holder: str | None, state: dict[str, Any]) -> None:
        super().__init__(
            f"spec {ref.name!r} in {ref.project} is locked by "
            f"{holder or 'another writer'}; state change rejected"
        )
        self.ref = ref
        self.holder = holder
        self.state = state


class LockLost(StateError):
    """The lock this operation relied on is no longer held by this caller."""


def state_root() -> Path:
    """Return the default state root: ``<data home>/apps/spec-engine/data/state``.

    Resolved through :func:`kiro_crew.config.paths.data_home` so it follows
    ``KIROCREW_HOME`` without re-running start-of-process home maintenance.
    """
    return data_home() / "apps" / APP_NAME / "data" / "state"


def reject_spec_tree_path(path: Path) -> None:
    """Raise if *path* falls inside a ``.kiro/specs`` tree.

    The engine's own state must be storable somewhere that is not the interop
    contract. A misconfigured root pointed at a spec directory is that failure,
    so it is reported as one rather than silently honoured.
    """
    parts = tuple(path.parts)
    window = len(SPEC_TREE_SEGMENTS)
    for index in range(len(parts) - window + 1):
        if parts[index : index + window] == SPEC_TREE_SEGMENTS:
            raise StatePersistenceError(
                f"refusing to store engine state inside a spec tree: {path}"
            )


def utc_now_iso() -> str:
    """Timestamp for persisted records: UTC, ISO-8601, second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class SpecRef:
    """Identity of a spec: the project that owns it plus its directory name."""

    project: str
    name: str

    @classmethod
    def of(cls, project: str | Path, name: str) -> "SpecRef":
        """Build a ref with the project path normalised to a resolved posix path.

        Normalisation happens here rather than in ``__init__`` so the dataclass
        stays a plain value that can be reconstructed from a database row without
        touching the filesystem again.
        """
        if not name or not name.strip():
            raise ValueError("spec name must not be empty")
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError(f"spec name must be a single path segment: {name!r}")
        resolved = Path(project).expanduser().resolve()
        return cls(project=resolved.as_posix(), name=name)

    @property
    def key(self) -> str:
        """Single-column identity used by every table that references a spec."""
        digest = hashlib.sha256(self.project.encode("utf-8")).hexdigest()
        return f"{digest[:PROJECT_FINGERPRINT_CHARS]}:{self.name}"

    @property
    def spec_dir(self) -> Path:
        """The native spec directory. Read and written by the IDE and CLI too."""
        return Path(self.project).joinpath(*SPEC_TREE_SEGMENTS, self.name)


@dataclass(frozen=True)
class SpecLock:
    """Proof of holding a spec's lock. Only the token can release it."""

    ref: SpecRef
    owner: str
    token: str
    acquired_ts: str
    expires_epoch: float


@dataclass(frozen=True)
class SpecRecord:
    spec_key: str
    project: str
    name: str
    spec_type: str | None
    phase: str | None
    archived: bool
    created_ts: str
    updated_ts: str
    lock_owner: str | None
    lock_acquired_ts: str | None
    lock_expires_epoch: float | None

    @property
    def ref(self) -> SpecRef:
        return SpecRef(project=self.project, name=self.name)


@dataclass(frozen=True)
class ApprovalRecord:
    spec_key: str
    gate: str
    actor: str
    approved_ts: str
    doc_hash: str
    stale: bool


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    spec_key: str
    source: str | None
    item_id: str | None
    state: str
    posture: str | None
    cost_credits: float
    created_ts: str
    updated_ts: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class ClaimRecord:
    kind: str
    scope: str
    subject: str
    generation: str
    run_id: str | None
    claimed_ts: str


@dataclass(frozen=True)
class WatchObservation:
    """What one poll observed about one item, ready to be recorded.

    Separate from :class:`WatchItemRecord` because a caller decides an item's
    lifecycle position and the store decides the timestamps: a diff that
    invented its own ``first_seen_ts`` could overwrite the real one.
    """

    item_id: str
    generation: int
    item_state: str = ""
    is_open: bool = True
    #: Hash over the fields that would change what a run was given (title, body,
    #: classification). Empty means "not computed" -- a caller that does not track
    #: content leaves it blank, and a blank recorded digest reads as unknown
    #: rather than as an edit. The digest itself is never the item text.
    content_digest: str = ""

    def __post_init__(self) -> None:
        if not self.item_id.strip():
            raise ValueError("an observation must name the item it observed")
        if self.generation < 1:
            raise ValueError("a lifecycle generation starts at one and only rises")


@dataclass(frozen=True)
class WatchItemRecord:
    """One item's recorded lifecycle position, as the last poll left it."""

    source: str
    item_id: str
    generation: int
    item_state: str
    is_open: bool
    first_seen_ts: str
    observed_ts: str
    #: The content digest recorded with this row, or the empty string for a row
    #: written before digests were stored (or migrated in by an upgrade). Empty
    #: reads as "unknown": it is never compared as though it were an edit, so an
    #: upgraded store does not report every pre-existing row as edited.
    content_digest: str = ""


@dataclass(frozen=True)
class ElementTrustRecord:
    """The trust class last derived for one content element, and from what.

    ``revision`` is the element revision the class was derived from, which is
    what makes an edit detectable: a later revision means the recorded class
    describes text that is no longer there, so it cannot authorize a decision
    about the text that replaced it.
    """

    scope: str
    element_id: str
    kind: str
    author: str
    revision: str
    class_name: str
    evidence: str
    derived_ts: str
    #: Whether a caller still owes a re-application of the decisions gated on this
    #: element's class. Set when the revision moves, cleared only by an explicit
    #: acknowledgement, so a crash between noticing and re-applying leaves the
    #: obligation outstanding instead of silently discharging it.
    pending_reapply: bool = False


@dataclass(frozen=True)
class WorkspaceRecord:
    workspace_id: int
    run_id: str
    kind: str
    location: str
    address: str | None
    disposable: bool
    cleaned: bool
    created_ts: str
    cleaned_ts: str | None


@dataclass(frozen=True)
class QueueRecord:
    seq: int
    source: str
    project: str
    item_id: str
    generation: str
    payload: dict[str, Any]
    enqueued_ts: str
    dequeued_ts: str | None


def _spec_record(row: sqlite3.Row) -> SpecRecord:
    return SpecRecord(
        spec_key=row["spec_key"],
        project=row["project"],
        name=row["name"],
        spec_type=row["spec_type"],
        phase=row["phase"],
        archived=bool(row["archived"]),
        created_ts=row["created_ts"],
        updated_ts=row["updated_ts"],
        lock_owner=row["lock_owner"],
        lock_acquired_ts=row["lock_acquired_ts"],
        lock_expires_epoch=row["lock_expires_epoch"],
    )


def _approval_record(row: sqlite3.Row) -> ApprovalRecord:
    return ApprovalRecord(
        spec_key=row["spec_key"],
        gate=row["gate"],
        actor=row["actor"],
        approved_ts=row["approved_ts"],
        doc_hash=row["doc_hash"],
        stale=bool(row["stale"]),
    )


def _decode_json_object(raw: str | None) -> dict[str, Any]:
    """Decode a stored JSON blob, tolerating a value that is not an object."""
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("discarding unparseable JSON column value")
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        spec_key=row["spec_key"],
        source=row["source"],
        item_id=row["item_id"],
        state=row["state"],
        posture=row["posture"],
        cost_credits=float(row["cost_credits"]),
        created_ts=row["created_ts"],
        updated_ts=row["updated_ts"],
        detail=_decode_json_object(row["detail"]),
    )


def _claim_record(row: sqlite3.Row) -> ClaimRecord:
    return ClaimRecord(
        kind=row["kind"],
        scope=row["scope"],
        subject=row["subject"],
        generation=row["generation"],
        run_id=row["run_id"],
        claimed_ts=row["claimed_ts"],
    )


def _watch_item_record(row: sqlite3.Row) -> WatchItemRecord:
    return WatchItemRecord(
        source=row["source"],
        item_id=row["item_id"],
        generation=int(row["generation"]),
        item_state=row["item_state"],
        is_open=bool(row["is_open"]),
        first_seen_ts=row["first_seen_ts"],
        observed_ts=row["observed_ts"],
        content_digest=row["content_digest"],
    )


def _element_trust_record(row: sqlite3.Row) -> ElementTrustRecord:
    return ElementTrustRecord(
        scope=row["scope"],
        element_id=row["element_id"],
        kind=row["kind"],
        author=row["author"],
        revision=row["revision"],
        class_name=row["class_name"],
        evidence=row["evidence"],
        derived_ts=row["derived_ts"],
        pending_reapply=bool(row["pending_reapply"]),
    )


def _workspace_record(row: sqlite3.Row) -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=int(row["workspace_id"]),
        run_id=row["run_id"],
        kind=row["kind"],
        location=row["location"],
        address=row["address"],
        disposable=bool(row["disposable"]),
        cleaned=bool(row["cleaned"]),
        created_ts=row["created_ts"],
        cleaned_ts=row["cleaned_ts"],
    )


def _queue_record(row: sqlite3.Row) -> QueueRecord:
    return QueueRecord(
        seq=int(row["seq"]),
        source=row["source"],
        project=row["project"],
        item_id=row["item_id"],
        generation=row["generation"],
        payload=_decode_json_object(row["payload"]),
        enqueued_ts=row["enqueued_ts"],
        dequeued_ts=row["dequeued_ts"],
    )


class StateStore:
    """The engine's persistent state, outside every spec directory.

    One instance per process is enough: connections are per thread, so several
    threads may share a store, and several processes may share the database
    file. Cross-process serialisation is SQLite's own (``BEGIN IMMEDIATE`` plus a
    busy timeout); cross-caller serialisation of a spec's state changes is the
    lock row, which is a domain decision and not something SQLite can express.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        resolved = Path(root) if root is not None else state_root()
        reject_spec_tree_path(resolved)
        self._root = resolved
        self._db_path = resolved / DB_FILENAME
        self._threads = threading.local()
        self._schema_lock = threading.Lock()
        self._ensure_schema()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        """Close this thread's connection. Other threads keep theirs."""
        conn = getattr(self._threads, "conn", None)
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
            self._threads.conn = None

    # ---------------------------------------------------------------- plumbing

    def _open(self) -> sqlite3.Connection:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._db_path),
                timeout=BUSY_TIMEOUT_S,
                # Autocommit: every write below opens BEGIN IMMEDIATE explicitly,
                # which is what makes a claim insert and a lock update atomic
                # against another process rather than against another statement.
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
            return conn
        except (sqlite3.Error, OSError) as exc:
            raise StatePersistenceError(
                f"could not open the engine state database at {self._db_path}: {exc}"
            ) from exc

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._threads, "conn", None)
        if conn is None:
            conn = self._open()
            self._threads.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        with self._schema_lock:
            with self._write() as conn:
                self._migrate(conn)
                for statement in _SCHEMA_STATEMENTS:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (str(SCHEMA_VERSION),),
                )

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring a pre-existing database up to the current shape.

        Runs before the ``CREATE TABLE IF NOT EXISTS`` replay, which converges an
        *additive* change on its own but cannot alter a table that already exists.
        A non-additive change, or a new column on a table that already exists,
        therefore needs a step here, or an older database keeps its old shape
        while the code assumes the new one -- and the version stamp would not
        catch it, because nothing reads the stamp to decide.

        ``element_trust`` gained ``kind`` in its primary key: keyed on the id
        alone, a comment's row overwrites its item body's. The table is dropped
        rather than copied because it is a cache of a derivation that can always
        be recomputed from the elements themselves, and carrying rows keyed the
        wrong way forward would preserve exactly the collision being fixed.

        ``watch_items`` gained ``content_digest``: an additive column, so it is
        added in place rather than dropped. The snapshot is a lifecycle ledger,
        not a recomputable cache -- dropping it would restart every open item at
        the first generation, whose dispatch claim is already held, and silently
        swallow the next reopen. A pre-existing row's digest defaults to the empty
        string, which reads as "unknown, not edited", so no upgraded row reports a
        spurious edit on the poll after the upgrade.
        """
        StateStore._drop_miskeyed_element_trust(conn)
        StateStore._add_watch_item_digest(conn)

    @staticmethod
    def _drop_miskeyed_element_trust(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'element_trust'"
        ).fetchone()
        if row is None:
            return
        keyed_on = [
            column["name"]
            for column in conn.execute("PRAGMA table_info(element_trust)").fetchall()
            if column["pk"]
        ]
        if "kind" not in keyed_on:
            conn.execute("DROP TABLE element_trust")

    @staticmethod
    def _add_watch_item_digest(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'watch_items'"
        ).fetchone()
        if row is None:
            return
        columns = [
            column["name"] for column in conn.execute("PRAGMA table_info(watch_items)").fetchall()
        ]
        if "content_digest" not in columns:
            conn.execute(
                "ALTER TABLE watch_items ADD COLUMN content_digest TEXT NOT NULL DEFAULT ''"
            )

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Run a write transaction, failing the operation if it cannot commit."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except (sqlite3.Error, OSError) as exc:
            raise StatePersistenceError(
                f"could not begin a write on {self._db_path}: {exc}"
            ) from exc
        try:
            yield conn
        except (sqlite3.Error, OSError) as exc:
            self._rollback(conn)
            raise StatePersistenceError(
                f"could not persist engine state to {self._db_path}: {exc}"
            ) from exc
        except BaseException:
            # A domain refusal (SpecLocked) or a caller error propagates as
            # itself; only the partial write is undone.
            self._rollback(conn)
            raise
        try:
            conn.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            self._rollback(conn)
            raise StatePersistenceError(
                f"could not commit engine state to {self._db_path}: {exc}"
            ) from exc

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        with contextlib.suppress(sqlite3.Error, OSError):
            conn.execute("ROLLBACK")

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        try:
            return self._conn().execute(sql, tuple(params)).fetchall()
        except (sqlite3.Error, OSError) as exc:
            raise StatePersistenceError(
                f"could not read engine state from {self._db_path}: {exc}"
            ) from exc

    def _query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------- specs

    def register_spec(
        self,
        ref: SpecRef,
        *,
        spec_type: str | None = None,
        phase: str | None = None,
    ) -> SpecRecord:
        """Insert or update the spec's registry row, preserving its lock.

        ``spec_type`` and ``phase`` are left as they are when passed as ``None``,
        so registering a spec again cannot erase a recorded type.
        """
        now = utc_now_iso()
        with self._write() as conn:
            self._insert_spec_row(conn, ref, now)
            conn.execute(
                "UPDATE specs SET spec_type = COALESCE(?, spec_type), "
                "phase = COALESCE(?, phase), updated_ts = ? WHERE spec_key = ?",
                (spec_type, phase, now, ref.key),
            )
        record = self.get_spec(ref)
        if record is None:  # pragma: no cover - the row was just written
            raise StatePersistenceError(f"spec row for {ref.name!r} vanished after write")
        return record

    @staticmethod
    def _insert_spec_row(conn: sqlite3.Connection, ref: SpecRef, now: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO specs (spec_key, project, name, created_ts, updated_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (ref.key, ref.project, ref.name, now, now),
        )

    def get_spec(self, ref: SpecRef) -> SpecRecord | None:
        row = self._query_one("SELECT * FROM specs WHERE spec_key = ?", (ref.key,))
        return _spec_record(row) if row is not None else None

    def list_specs(
        self, *, project: str | Path | None = None, include_archived: bool = False
    ) -> list[SpecRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if project is not None:
            clauses.append("project = ?")
            params.append(Path(project).expanduser().resolve().as_posix())
        if not include_archived:
            clauses.append("archived = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(f"SELECT * FROM specs{where} ORDER BY project, name", params)
        return [_spec_record(row) for row in rows]

    def record_phase(self, ref: SpecRef, phase: str | None) -> None:
        """Cache the phase last derived from disk. Never an authority on its own."""
        self._update_spec(ref, "phase = ?", (phase,))

    def set_spec_type(self, ref: SpecRef, spec_type: str) -> None:
        if not spec_type:
            raise ValueError("spec type must not be empty")
        self._update_spec(ref, "spec_type = ?", (spec_type,))

    def set_archived(self, ref: SpecRef, archived: bool) -> None:
        """Archive or unarchive a spec. Reversible in both directions."""
        self._update_spec(ref, "archived = ?", (1 if archived else 0,))

    def _update_spec(self, ref: SpecRef, assignment: str, params: Sequence[Any]) -> None:
        now = utc_now_iso()
        with self._write() as conn:
            self._insert_spec_row(conn, ref, now)
            conn.execute(
                f"UPDATE specs SET {assignment}, updated_ts = ? WHERE spec_key = ?",
                (*params, now, ref.key),
            )

    # ------------------------------------------------------------ spec locking

    def acquire_lock(
        self,
        ref: SpecRef,
        *,
        owner: str,
        ttl_s: float = DEFAULT_LOCK_TTL_S,
    ) -> SpecLock:
        """Take the spec's lock, or reject with the spec's current state.

        A live lock held by anyone — including another operation under the same
        owner name — is a conflict: two concurrent state changes on one spec are
        exactly what this serialises, and treating a repeat acquisition as
        re-entrant would let a second operation in the same session interleave
        with the first. An expired lock is taken over, because its holder is gone
        and the alternative is a spec nobody can ever write again.
        """
        if not owner:
            raise ValueError("lock owner must not be empty")
        if ttl_s <= 0:
            raise ValueError("lock ttl must be positive")
        token = secrets.token_hex(LOCK_TOKEN_BYTES)
        now_epoch = time.time()
        acquired_ts = utc_now_iso()
        expires_epoch = now_epoch + ttl_s
        with self._write() as conn:
            self._insert_spec_row(conn, ref, acquired_ts)
            cursor = conn.execute(
                "UPDATE specs SET lock_owner = ?, lock_token = ?, lock_acquired_ts = ?, "
                "lock_expires_epoch = ? WHERE spec_key = ? AND "
                "(lock_token IS NULL OR lock_expires_epoch IS NULL OR lock_expires_epoch <= ?)",
                (owner, token, acquired_ts, expires_epoch, ref.key, now_epoch),
            )
            if cursor.rowcount != 1:
                state = self._snapshot(conn, ref)
                lock_state = state.get("lock")
                holder = lock_state.get("owner") if isinstance(lock_state, dict) else None
                raise SpecLocked(ref, holder, state)
        return SpecLock(
            ref=ref,
            owner=owner,
            token=token,
            acquired_ts=acquired_ts,
            expires_epoch=expires_epoch,
        )

    def release_lock(self, lock: SpecLock) -> bool:
        """Release *lock*. False means it was already lost (expired or taken over)."""
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE specs SET lock_owner = NULL, lock_token = NULL, "
                "lock_acquired_ts = NULL, lock_expires_epoch = NULL "
                "WHERE spec_key = ? AND lock_token = ?",
                (lock.ref.key, lock.token),
            )
            return cursor.rowcount == 1

    def verify_lock(self, lock: SpecLock) -> None:
        """Raise :class:`LockLost` unless *lock* still holds the spec.

        Callers that perform several writes under one lock use this before the
        last one, so a long operation whose lock expired underneath it fails
        instead of writing state that a second writer has already superseded.
        """
        row = self._query_one(
            "SELECT lock_token, lock_expires_epoch FROM specs WHERE spec_key = ?",
            (lock.ref.key,),
        )
        if row is None or row["lock_token"] != lock.token:
            raise LockLost(f"lock on spec {lock.ref.name!r} is no longer held by {lock.owner}")
        expires = row["lock_expires_epoch"]
        if expires is not None and float(expires) <= time.time():
            raise LockLost(f"lock on spec {lock.ref.name!r} held by {lock.owner} has expired")

    @contextlib.contextmanager
    def lock(
        self,
        ref: SpecRef,
        *,
        owner: str,
        ttl_s: float = DEFAULT_LOCK_TTL_S,
    ) -> Iterator[SpecLock]:
        """Hold the spec's lock for the duration of the block."""
        handle = self.acquire_lock(ref, owner=owner, ttl_s=ttl_s)
        try:
            yield handle
        finally:
            if not self.release_lock(handle):
                # Not raised: the operation itself may well have succeeded, and
                # the caller learns about a lost lock from verify_lock.
                logger.warning(
                    "lock on spec %r was already lost when %s released it",
                    handle.ref.name,
                    handle.owner,
                )

    def current_state(self, ref: SpecRef) -> dict[str, Any]:
        """The spec's current state, as returned with a rejected state change."""
        return self._snapshot(self._conn(), ref)

    def _snapshot(self, conn: sqlite3.Connection, ref: SpecRef) -> dict[str, Any]:
        try:
            spec_row = conn.execute("SELECT * FROM specs WHERE spec_key = ?", (ref.key,)).fetchone()
            approval_rows = conn.execute(
                "SELECT * FROM approvals WHERE spec_key = ? ORDER BY gate", (ref.key,)
            ).fetchall()
            run_rows = conn.execute(
                "SELECT * FROM runs WHERE spec_key = ? ORDER BY updated_ts DESC LIMIT ?",
                (ref.key, SNAPSHOT_RUN_LIMIT),
            ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            raise StatePersistenceError(
                f"could not read the current state of spec {ref.name!r}: {exc}"
            ) from exc
        record = _spec_record(spec_row) if spec_row is not None else None
        return {
            "project": ref.project,
            "name": ref.name,
            "spec_key": ref.key,
            "registered": record is not None,
            "spec_type": record.spec_type if record else None,
            "phase": record.phase if record else None,
            "archived": record.archived if record else False,
            "updated_ts": record.updated_ts if record else None,
            "lock": self._lock_state(record),
            "approvals": [
                {
                    "gate": row["gate"],
                    "actor": row["actor"],
                    "approved_ts": row["approved_ts"],
                    "stale": bool(row["stale"]),
                }
                for row in approval_rows
            ],
            "runs": [
                {
                    "run_id": row["run_id"],
                    "state": row["state"],
                    "updated_ts": row["updated_ts"],
                }
                for row in run_rows
            ],
        }

    @staticmethod
    def _lock_state(record: SpecRecord | None) -> dict[str, Any] | None:
        if record is None or record.lock_owner is None:
            return None
        expires = record.lock_expires_epoch
        return {
            "owner": record.lock_owner,
            "acquired_ts": record.lock_acquired_ts,
            "expired": expires is not None and float(expires) <= time.time(),
            "expires_in_s": None if expires is None else round(float(expires) - time.time(), 3),
        }

    # --------------------------------------------------------------- approvals

    def record_approval(
        self,
        ref: SpecRef,
        *,
        gate: str,
        actor: str,
        doc_hash: str,
        approved_ts: str | None = None,
    ) -> ApprovalRecord:
        """Record (or replace) the approval of one gate with who approved it."""
        if not gate or not actor:
            raise ValueError("an approval needs both a gate and an actor")
        ts = approved_ts or utc_now_iso()
        with self._write() as conn:
            self._insert_spec_row(conn, ref, ts)
            conn.execute(
                "INSERT INTO approvals (spec_key, gate, actor, approved_ts, doc_hash, stale) "
                "VALUES (?, ?, ?, ?, ?, 0) "
                "ON CONFLICT (spec_key, gate) DO UPDATE SET actor = excluded.actor, "
                "approved_ts = excluded.approved_ts, doc_hash = excluded.doc_hash, stale = 0",
                (ref.key, gate, actor, ts, doc_hash),
            )
        return ApprovalRecord(
            spec_key=ref.key,
            gate=gate,
            actor=actor,
            approved_ts=ts,
            doc_hash=doc_hash,
            stale=False,
        )

    def get_approval(self, ref: SpecRef, gate: str) -> ApprovalRecord | None:
        row = self._query_one(
            "SELECT * FROM approvals WHERE spec_key = ? AND gate = ?", (ref.key, gate)
        )
        return _approval_record(row) if row is not None else None

    def list_approvals(self, ref: SpecRef) -> list[ApprovalRecord]:
        rows = self._query("SELECT * FROM approvals WHERE spec_key = ? ORDER BY gate", (ref.key,))
        return [_approval_record(row) for row in rows]

    def mark_approval_stale(self, ref: SpecRef, gate: str) -> bool:
        """Mark one gate's approval stale. False means no approval was recorded.

        A gate that was never approved is left untouched, so an edit cannot
        invent approval state for a gate nobody approved.

        Staling is one-way and there is deliberately no primitive to undo it. An
        approval goes stale because its document changed after a human approved
        it, and the only honest way back is a fresh approval of what the document
        says now. A clearing primitive would be the shortest path to an approval
        that appears live for bytes nobody agreed to -- and after a revert to the
        approved bytes, the flag is the sole surviving evidence that the document
        moved at all.
        """
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET stale = 1 WHERE spec_key = ? AND gate = ?",
                (ref.key, gate),
            )
            return cursor.rowcount == 1

    # -------------------------------------------------------------------- runs

    def create_run(
        self,
        run_id: str,
        ref: SpecRef,
        *,
        state: str,
        source: str | None = None,
        item_id: str | None = None,
        posture: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> RunRecord:
        if not run_id:
            raise ValueError("run id must not be empty")
        if not state:
            raise ValueError("run state must not be empty")
        now = utc_now_iso()
        with self._write() as conn:
            self._insert_spec_row(conn, ref, now)
            conn.execute(
                "INSERT INTO runs (run_id, spec_key, source, item_id, state, posture, "
                "cost_credits, created_ts, updated_ts, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    run_id,
                    ref.key,
                    source,
                    item_id,
                    state,
                    posture,
                    now,
                    now,
                    json.dumps(detail or {}),
                ),
            )
        record = self.get_run(run_id)
        if record is None:  # pragma: no cover - the row was just written
            raise StatePersistenceError(f"run row {run_id!r} vanished after write")
        return record

    def update_run(
        self,
        run_id: str,
        *,
        state: str | None = None,
        posture: str | None = None,
        cost_credits: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> RunRecord:
        """Update the named fields of a run. Absent arguments are left alone.

        ``detail`` is merged into the stored object rather than replacing it, so
        one writer recording a stage outcome cannot drop another's key.
        """
        assignments: list[str] = []
        params: list[Any] = []
        if state is not None:
            assignments.append("state = ?")
            params.append(state)
        if posture is not None:
            assignments.append("posture = ?")
            params.append(posture)
        if cost_credits is not None:
            assignments.append("cost_credits = ?")
            params.append(float(cost_credits))
        with self._write() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown run: {run_id!r}")
            if detail is not None:
                merged = {**_decode_json_object(row["detail"]), **detail}
                assignments.append("detail = ?")
                params.append(json.dumps(merged))
            assignments.append("updated_ts = ?")
            params.append(utc_now_iso())
            conn.execute(
                f"UPDATE runs SET {', '.join(assignments)} WHERE run_id = ?",
                (*params, run_id),
            )
        record = self.get_run(run_id)
        if record is None:  # pragma: no cover - the row was just written
            raise StatePersistenceError(f"run row {run_id!r} vanished after write")
        return record

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self._query_one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return _run_record(row) if row is not None else None

    def list_runs(
        self,
        *,
        ref: SpecRef | None = None,
        states: Sequence[str] | None = None,
    ) -> list[RunRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if ref is not None:
            clauses.append("spec_key = ?")
            params.append(ref.key)
        if states:
            placeholders = ", ".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(states)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(f"SELECT * FROM runs{where} ORDER BY created_ts, run_id", params)
        return [_run_record(row) for row in rows]

    # ------------------------------------------------------- claims (ledger)

    def claim(
        self,
        kind: str,
        scope: str,
        subject: str,
        *,
        generation: str = "",
        run_id: str | None = None,
    ) -> bool:
        """Claim one delivery. True the first time, False every time after.

        The primary key is the claim, and the insert either wins or loses inside
        one transaction, so a repeated poll, a retry, and a resumed run all read
        the same answer regardless of ordering. Callers claim BEFORE performing
        the side effect: a claim held by work that then failed is a missed
        delivery, whereas a side effect performed before the claim is a
        duplicate one, and duplicates are what this ledger exists to prevent.
        """
        if not kind or not scope or not subject:
            raise ValueError("a claim needs a kind, a scope, and a subject")
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO claims (kind, scope, subject, generation, run_id, claimed_ts) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (kind, scope, subject, generation) DO NOTHING",
                (kind, scope, subject, generation, run_id, utc_now_iso()),
            )
            return cursor.rowcount == 1

    def claim_dispatch(
        self, source: str, item_id: str, *, generation: str = "", run_id: str | None = None
    ) -> bool:
        """Claim a watched item's lifecycle generation for dispatch."""
        return self.claim(CLAIM_DISPATCH, source, item_id, generation=generation, run_id=run_id)

    def claim_writeback(self, run_id: str, event: str) -> bool:
        """Claim one lifecycle-event writeback for one run."""
        return self.claim(CLAIM_WRITEBACK, run_id, event, run_id=run_id)

    def get_claim(
        self, kind: str, scope: str, subject: str, *, generation: str = ""
    ) -> ClaimRecord | None:
        row = self._query_one(
            "SELECT * FROM claims WHERE kind = ? AND scope = ? AND subject = ? "
            "AND generation = ?",
            (kind, scope, subject, generation),
        )
        return _claim_record(row) if row is not None else None

    def release_claim(self, kind: str, scope: str, subject: str, *, generation: str = "") -> bool:
        """Drop a claim so it can be made again (the manual re-dispatch override)."""
        with self._write() as conn:
            cursor = conn.execute(
                "DELETE FROM claims WHERE kind = ? AND scope = ? AND subject = ? "
                "AND generation = ?",
                (kind, scope, subject, generation),
            )
            return cursor.rowcount == 1

    def release_claims(self, kind: str, scope: str, subject: str) -> int:
        """Drop every generation of one subject's claim, returning how many went.

        Distinct from :meth:`release_claim`, which names one generation. A caller
        releasing a subject whose generations it does not know -- a held reviewer
        comment that may have been claimed at a revision since edited -- needs all
        of them gone, because one left behind would make the release look applied
        while the next poll still read the subject as already seen.
        """
        with self._write() as conn:
            cursor = conn.execute(
                "DELETE FROM claims WHERE kind = ? AND scope = ? AND subject = ?",
                (kind, scope, subject),
            )
            return int(cursor.rowcount)

    def list_claims(
        self, *, kind: str | None = None, scope: str | None = None
    ) -> list[ClaimRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(f"SELECT * FROM claims{where} ORDER BY claimed_ts, subject", params)
        return [_claim_record(row) for row in rows]

    # ------------------------------------------------- watched-item snapshot

    def record_watch_items(self, source: str, observations: Sequence[WatchObservation]) -> None:
        """Record what one poll observed, as one transaction.

        The whole snapshot lands or none of it does. A half-applied snapshot
        would be a snapshot no poll ever saw, and the next diff would derive its
        transitions by comparing against it — inventing reopens and
        cancellations out of an interrupted write.

        Items the *observations* do not mention are left as they are. A poll that
        stopped reporting an item says so by observing it closed, never by
        omitting it, because an omission is also what a narrowed poll filter
        looks like.
        """
        if not source.strip():
            raise ValueError("a watched-item observation must name its source")
        if not observations:
            return
        now = utc_now_iso()
        with self._write() as conn:
            for observation in observations:
                conn.execute(
                    "INSERT INTO watch_items (source, item_id, generation, item_state, "
                    "is_open, content_digest, first_seen_ts, observed_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (source, item_id) DO UPDATE SET "
                    "generation = excluded.generation, item_state = excluded.item_state, "
                    "is_open = excluded.is_open, content_digest = excluded.content_digest, "
                    "observed_ts = excluded.observed_ts",
                    (
                        source,
                        observation.item_id,
                        observation.generation,
                        observation.item_state,
                        1 if observation.is_open else 0,
                        observation.content_digest,
                        now,
                        now,
                    ),
                )

    def get_watch_item(self, source: str, item_id: str) -> WatchItemRecord | None:
        row = self._query_one(
            "SELECT * FROM watch_items WHERE source = ? AND item_id = ?", (source, item_id)
        )
        return _watch_item_record(row) if row is not None else None

    def list_watch_items(self, source: str) -> list[WatchItemRecord]:
        """Every item *source* has ever reported, in identifier order.

        Identifier order rather than observation order so a diff derived from
        this snapshot reports its transitions the same way twice.
        """
        rows = self._query("SELECT * FROM watch_items WHERE source = ? ORDER BY item_id", (source,))
        return [_watch_item_record(row) for row in rows]

    def forget_watch_item(self, source: str, item_id: str) -> bool:
        """Forget one item's snapshot row, so the next poll derives it as new again.

        The one thing a poll's snapshot otherwise never gives up, and the primitive
        the manual re-dispatch override needs. A still-open item already recorded in
        ``watch_items`` derives ``unchanged`` on every later poll and is therefore
        not a dispatch candidate -- so releasing its dispatch claim alone cannot
        re-offer it, because the snapshot row, not the claim, is what suppresses it.
        Deleting the row makes the next observation of the item ``new`` at the first
        generation.

        True when a row was removed, False when the source held none for that item.
        Deliberately the default for nothing: re-offering every unchanged item each
        poll would spend credits on work nobody asked to redo, so forgetting a row
        stays a deliberate act an operator takes.
        """
        with self._write() as conn:
            cursor = conn.execute(
                "DELETE FROM watch_items WHERE source = ? AND item_id = ?", (source, item_id)
            )
            return cursor.rowcount == 1

    # ---------------------------------------------------------- element trust

    def record_element_trust(
        self,
        scope: str,
        *,
        element_id: str,
        kind: str,
        author: str,
        revision: str,
        class_name: str,
        evidence: str,
        pending_reapply: bool = False,
    ) -> ElementTrustRecord:
        """Record the class derived for one element at one revision.

        Overwrites the element's previous row rather than appending, because this
        table answers "is the class on file current" and only the latest
        revision can answer it. The audit log is where the history lives, and it
        is append-only for exactly this reason.

        ``pending_reapply`` is OR-ed with what is already stored rather than
        replacing it. A pending obligation may only be cleared by
        :meth:`acknowledge_element_trust`; if a plain re-record could clear it,
        merely looking at the element again would discharge a re-application that
        never happened.
        """
        if not scope.strip():
            raise ValueError("an element trust record must name the scope that consumed it")
        if not element_id.strip():
            raise ValueError("an element trust record must name the element")
        if not kind.strip():
            raise ValueError("an element trust record must name the element kind")
        if not revision.strip():
            raise ValueError("an element trust record must carry the revision it was derived from")
        now = utc_now_iso()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO element_trust (scope, kind, element_id, author, revision, "
                "class_name, evidence, pending_reapply, derived_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (scope, kind, element_id) DO UPDATE SET "
                "author = excluded.author, revision = excluded.revision, "
                "class_name = excluded.class_name, evidence = excluded.evidence, "
                "pending_reapply = MAX(element_trust.pending_reapply, excluded.pending_reapply), "
                "derived_ts = excluded.derived_ts",
                (
                    scope,
                    kind,
                    element_id,
                    author,
                    revision,
                    class_name,
                    evidence,
                    1 if pending_reapply else 0,
                    now,
                ),
            )
        stored = self.get_element_trust(scope, kind, element_id)
        if stored is None:  # pragma: no cover - the insert above just wrote it
            raise StatePersistenceError(
                f"element trust for {element_id!r} in scope {scope!r} did not persist"
            )
        return stored

    def acknowledge_element_trust(self, scope: str, kind: str, element_id: str) -> bool:
        """Clear one element's pending re-application. True if one was pending.

        The only way the flag goes down. A caller invokes this AFTER re-applying
        every decision gated on the element's class, so an interrupted caller
        leaves the obligation on the row and the next reconcile still reports it.
        """
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE element_trust SET pending_reapply = 0 "
                "WHERE scope = ? AND kind = ? AND element_id = ? AND pending_reapply = 1",
                (scope, kind, element_id),
            )
            return cursor.rowcount == 1

    def get_element_trust(
        self, scope: str, kind: str, element_id: str
    ) -> ElementTrustRecord | None:
        row = self._query_one(
            "SELECT * FROM element_trust WHERE scope = ? AND kind = ? AND element_id = ?",
            (scope, kind, element_id),
        )
        return _element_trust_record(row) if row is not None else None

    def list_element_trust(self, scope: str) -> list[ElementTrustRecord]:
        """Every element recorded under *scope*, in kind then element order."""
        rows = self._query(
            "SELECT * FROM element_trust WHERE scope = ? ORDER BY kind, element_id", (scope,)
        )
        return [_element_trust_record(row) for row in rows]

    # -------------------------------------------------------------- workspaces

    def record_workspace(
        self,
        run_id: str,
        *,
        kind: str,
        location: str | Path,
        address: str | None = None,
        disposable: bool = True,
    ) -> WorkspaceRecord:
        """Record a workspace or deployment so cleanup can find it later."""
        if not run_id or not kind:
            raise ValueError("a workspace record needs a run id and a kind")
        now = utc_now_iso()
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO workspaces (run_id, kind, location, address, disposable, "
                "cleaned, created_ts) VALUES (?, ?, ?, ?, ?, 0, ?)",
                (run_id, kind, str(location), address, 1 if disposable else 0, now),
            )
            workspace_id = int(cursor.lastrowid or 0)
        return WorkspaceRecord(
            workspace_id=workspace_id,
            run_id=run_id,
            kind=kind,
            location=str(location),
            address=address,
            disposable=disposable,
            cleaned=False,
            created_ts=now,
            cleaned_ts=None,
        )

    def list_workspaces(
        self, *, run_id: str | None = None, include_cleaned: bool = False
    ) -> list[WorkspaceRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if not include_cleaned:
            clauses.append("cleaned = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(f"SELECT * FROM workspaces{where} ORDER BY workspace_id", params)
        return [_workspace_record(row) for row in rows]

    def mark_workspace_cleaned(self, workspace_id: int) -> bool:
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE workspaces SET cleaned = 1, cleaned_ts = ? "
                "WHERE workspace_id = ? AND cleaned = 0",
                (utc_now_iso(), workspace_id),
            )
            return cursor.rowcount == 1

    # ------------------------------------------------------------------- queue

    def enqueue(
        self,
        *,
        source: str,
        project: str | Path,
        item_id: str,
        generation: str = "",
        payload: dict[str, Any] | None = None,
    ) -> QueueRecord | None:
        """Queue an item for dispatch. ``None`` means it was already queued.

        The uniqueness is on (source, item, generation), so a poll that runs
        again while the queue is full re-reads the same backlog instead of
        growing it.
        """
        if not source or not item_id:
            raise ValueError("a queue entry needs a source and an item id")
        now = utc_now_iso()
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO queue (source, project, item_id, generation, payload, "
                "enqueued_ts) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source, item_id, generation) DO NOTHING",
                (
                    source,
                    str(project),
                    item_id,
                    generation,
                    json.dumps(payload or {}),
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return None
            seq = int(cursor.lastrowid or 0)
        return QueueRecord(
            seq=seq,
            source=source,
            project=str(project),
            item_id=item_id,
            generation=generation,
            payload=payload or {},
            enqueued_ts=now,
            dequeued_ts=None,
        )

    def next_queued(self, *, project: str | Path | None = None) -> QueueRecord | None:
        """Take the oldest pending entry, marking it dequeued in the same write.

        Selection and marking share one transaction so two dispatchers cannot
        both take the same entry.
        """
        clauses = ["dequeued_ts IS NULL"]
        params: list[Any] = []
        if project is not None:
            clauses.append("project = ?")
            params.append(str(project))
        now = utc_now_iso()
        with self._write() as conn:
            row = conn.execute(
                f"SELECT * FROM queue WHERE {' AND '.join(clauses)} ORDER BY seq LIMIT 1",
                tuple(params),
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE queue SET dequeued_ts = ? WHERE seq = ?", (now, row["seq"]))
            record = _queue_record(row)
        return QueueRecord(
            seq=record.seq,
            source=record.source,
            project=record.project,
            item_id=record.item_id,
            generation=record.generation,
            payload=record.payload,
            enqueued_ts=record.enqueued_ts,
            dequeued_ts=now,
        )

    def list_queue(
        self, *, project: str | Path | None = None, include_dequeued: bool = False
    ) -> list[QueueRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if project is not None:
            clauses.append("project = ?")
            params.append(str(project))
        if not include_dequeued:
            clauses.append("dequeued_ts IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(f"SELECT * FROM queue{where} ORDER BY seq", params)
        return [_queue_record(row) for row in rows]

    def queue_depth(self, *, project: str | Path | None = None) -> int:
        return len(self.list_queue(project=project))
