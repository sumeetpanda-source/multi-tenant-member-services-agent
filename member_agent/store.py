import json
import hashlib
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .domain import Scope
from .errors import PolicyDenied


class Store:
    """Local trusted state. Connections are short-lived and transactions explicit."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, member TEXT NOT NULL,
                    created REAL NOT NULL, steps INTEGER NOT NULL DEFAULT 0,
                    model_tokens INTEGER NOT NULL DEFAULT 0, cost TEXT NOT NULL DEFAULT '0',
                    active_seconds REAL NOT NULL DEFAULT 0, turn_owner TEXT, lease_until REAL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    body TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, approved REAL, result TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_operation ON operations(session_id)
                    WHERE status IN ('DRAFT','APPROVED','IN_FLIGHT');
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id), timestamp REAL NOT NULL,
                    event TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_session ON events(session_id, seq);
                CREATE INDEX IF NOT EXISTS operations_by_session ON operations(session_id, created DESC);
                CREATE TABLE IF NOT EXISTS read_cache (
                    session_id TEXT NOT NULL REFERENCES sessions(id), resource TEXT NOT NULL,
                    captured REAL NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(session_id, resource)
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(operations)")}
            if "approved_hash" not in columns:
                db.execute("ALTER TABLE operations ADD COLUMN approved_hash TEXT")

    @contextmanager
    def connection(self, immediate=False):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        try:
            if immediate:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def create_session(self, tenant: str, member_id: str) -> Scope:
        if not re.fullmatch(r"M\d{5,20}", member_id):
            raise PolicyDenied("invalid_member_id")
        scope = Scope(uuid.uuid4().hex, tenant, member_id)
        with self.connection() as db:
            db.execute("INSERT INTO sessions(id,tenant,member,created) VALUES(?,?,?,?)",
                       (scope.session_id, tenant, member_id, time.time()))
        return scope

    @staticmethod
    def _check_scope(db, scope):
        row = db.execute("SELECT * FROM sessions WHERE id=?", (scope.session_id,)).fetchone()
        if row is None or row["tenant"] != scope.tenant or row["member"] != scope.member_id:
            raise PolicyDenied("session_scope_mismatch")
        return row

    def session(self, session_id: str, tenant: str, member_id: str) -> Scope:
        scope = Scope(session_id, tenant, member_id)
        self.usage(scope)
        return scope

    def usage(self, scope: Scope) -> dict:
        with self.connection() as db:
            row = self._check_scope(db, scope)
        return {key: row[key] for key in ("steps", "model_tokens", "cost", "active_seconds")}

    @contextmanager
    def turn(self, scope: Scope, lease_seconds: float):
        owner = uuid.uuid4().hex
        with self.connection(immediate=True) as db:
            row = self._check_scope(db, scope)
            if row["turn_owner"] and (row["lease_until"] or 0) > time.time():
                raise PolicyDenied("conversation_busy")
            db.execute("UPDATE sessions SET turn_owner=?,lease_until=? WHERE id=?",
                       (owner, time.time() + lease_seconds, scope.session_id))
        try:
            yield owner
        finally:
            with self.connection() as db:
                db.execute("UPDATE sessions SET turn_owner=NULL,lease_until=NULL WHERE id=? AND turn_owner=?",
                           (scope.session_id, owner))

    def save_usage(self, scope, usage):
        with self.connection(immediate=True) as db:
            self._check_scope(db, scope)
            db.execute("UPDATE sessions SET steps=?,model_tokens=?,cost=?,active_seconds=? WHERE id=?",
                       (usage["steps"], usage["model_tokens"], str(usage["cost"]),
                        usage["active_seconds"], scope.session_id))

    def event(self, scope: Scope, event: str, **data):
        # No free-text fields: both keys and string values have fixed safe shapes.
        allowed = {"tool", "code", "decision", "status", "attempt", "elapsed_ms",
                   "input_tokens", "output_tokens", "cost_usd", "mode", "count"}
        if not re.fullmatch(r"[a-z_.]{1,48}", event) or set(data) - allowed:
            raise ValueError("invalid_audit_schema")
        for value in data.values():
            if isinstance(value, str) and not re.fullmatch(r"[a-z0-9_.-]{1,80}", value):
                raise ValueError("invalid_audit_value")
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise ValueError("invalid_audit_value")
        for key in ("tool", "code", "decision", "mode", "status"):
            if key in data and isinstance(data[key], str) and not re.fullmatch(r"[a-z][a-z_-]{0,79}", data[key]):
                raise ValueError("invalid_audit_value")
        with self.connection() as db:
            self._check_scope(db, scope)
            db.execute("INSERT INTO events(session_id,timestamp,event,data) VALUES(?,?,?,?)",
                       (scope.session_id, time.time(), event, json.dumps(data, sort_keys=True)))

    def events(self, scope):
        with self.connection() as db:
            self._check_scope(db, scope)
            rows = db.execute("SELECT seq,timestamp,event,data FROM events WHERE session_id=? ORDER BY seq",
                              (scope.session_id,)).fetchall()
        return [{"seq": r["seq"], "timestamp": r["timestamp"], "event": r["event"],
                 **json.loads(r["data"])} for r in rows]

    def operation(self, scope, operation_id=None):
        with self.connection() as db:
            self._check_scope(db, scope)
            if operation_id:
                row = db.execute("SELECT * FROM operations WHERE id=? AND session_id=?",
                                 (operation_id, scope.session_id)).fetchone()
            else:
                row = db.execute("SELECT * FROM operations WHERE session_id=? ORDER BY created DESC LIMIT 1",
                                 (scope.session_id,)).fetchone()
        if operation_id and row is None:
            raise PolicyDenied("operation_scope_mismatch")
        return dict(row) if row else None

    def draft(self, scope, body):
        serialised = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        with self.connection(immediate=True) as db:
            self._check_scope(db, scope)
            open_row = db.execute("SELECT id FROM operations WHERE session_id=? "
                                  "AND status IN ('DRAFT','APPROVED','IN_FLIGHT')", (scope.session_id,)).fetchone()
            if open_row:
                raise PolicyDenied("pending_operation_exists")
            previous = db.execute("SELECT id FROM operations WHERE session_id=? AND body=? "
                                  "AND status='COMPLETED' ORDER BY created DESC LIMIT 1",
                                  (scope.session_id, serialised)).fetchone()
            if previous:
                return self.operation(scope, previous["id"])
            operation_id = uuid.uuid4().hex
            db.execute("INSERT INTO operations(id,session_id,body,status,created) VALUES(?,?,?,'DRAFT',?)",
                       (operation_id, scope.session_id, serialised, time.time()))
        return self.operation(scope, operation_id)

    def approve(self, scope, operation_id, ttl):
        with self.connection(immediate=True) as db:
            self._check_scope(db, scope)
            row = db.execute("SELECT * FROM operations WHERE id=? AND session_id=?",
                             (operation_id, scope.session_id)).fetchone()
            if row is None:
                raise PolicyDenied("operation_scope_mismatch")
            if row["status"] == "DRAFT":
                if time.time() - row["created"] > ttl:
                    raise PolicyDenied("approval_expired")
                digest = hashlib.sha256(row["body"].encode("utf-8")).hexdigest()
                db.execute("UPDATE operations SET status='APPROVED',approved=?,approved_hash=? WHERE id=?",
                           (time.time(), digest, operation_id))
            elif row["status"] not in {"APPROVED", "IN_FLIGHT", "COMPLETED"}:
                raise PolicyDenied("no_active_draft")

    def cancel(self, scope, operation_id):
        with self.connection(immediate=True) as db:
            self._check_scope(db, scope)
            row = db.execute("SELECT status FROM operations WHERE id=? AND session_id=?",
                             (operation_id, scope.session_id)).fetchone()
            if row is None or row["status"] != "DRAFT":
                raise PolicyDenied("cannot_cancel_submitted_operation")
            db.execute("UPDATE operations SET status='CANCELLED' WHERE id=?", (operation_id,))

    def mark_in_flight(self, scope, operation_id):
        with self.connection(immediate=True) as db:
            self._check_scope(db, scope)
            row = db.execute("SELECT status FROM operations WHERE id=? AND session_id=?",
                             (operation_id, scope.session_id)).fetchone()
            if row is None or row["status"] not in {"APPROVED", "IN_FLIGHT"}:
                raise PolicyDenied("confirmation_required")
            db.execute("UPDATE operations SET status='IN_FLIGHT' WHERE id=?", (operation_id,))

    def complete(self, scope, operation_id, result):
        with self.connection(immediate=True) as db:
            self._check_scope(db, scope)
            updated = db.execute("UPDATE operations SET status='COMPLETED',result=? "
                                 "WHERE id=? AND session_id=? AND status IN ('IN_FLIGHT','COMPLETED')",
                                 (json.dumps(result, sort_keys=True), operation_id, scope.session_id))
            if updated.rowcount != 1:
                raise PolicyDenied("operation_scope_mismatch")

    def cached(self, scope, resource):
        with self.connection(immediate=True) as db:
            self._check_scope(db, scope)
            now = time.time()
            db.execute("DELETE FROM read_cache WHERE session_id=? AND expires<=?", (scope.session_id, now))
            row = db.execute("SELECT payload,captured FROM read_cache WHERE session_id=? AND resource=? AND expires>?",
                             (scope.session_id, resource, now)).fetchone()
        return (json.loads(row["payload"]), max(0, now - row["captured"])) if row else None

    def cache(self, scope, resource, payload, captured, ttl):
        if ttl <= 0 or time.time() - captured >= ttl:
            return
        with self.connection(immediate=True) as db:
            self._check_scope(db, scope)
            db.execute("INSERT OR REPLACE INTO read_cache(session_id,resource,captured,expires,payload) VALUES(?,?,?,?,?)",
                       (scope.session_id, resource, captured, captured + ttl, json.dumps(payload)))
