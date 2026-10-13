"""Persistence: SQLite for the prototype; the same schema ports to Postgres.

Tables
  hires / hire_versions   canonical record (JSON) with optimistic concurrency on version
  source_refs             identity map: (system, external id) -> hire_id
  events                  ingest dedupe ledger (event_id is the idempotency key)
  applied_state           last snapshot WE wrote per (hire, target): the "base" of 3-way diff
  retries                 the outbox: targets that failed transiently and must be re-reconciled
  gates / exceptions      monitoring state
  audit                   append-only, hash-chained ledger (tamper-evident)
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from .models import EventEnvelope, ExceptionItem, GateState, HireRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS hires (hire_id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS hire_versions (hire_id TEXT, version INTEGER, data TEXT, created_at TEXT, PRIMARY KEY (hire_id, version));
CREATE TABLE IF NOT EXISTS source_refs (system TEXT, external_id TEXT, hire_id TEXT, PRIMARY KEY (system, external_id));
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, source TEXT, type TEXT, external_id TEXT,
  occurred_at TEXT, received_at TEXT, hire_id TEXT, outcome TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS applied_state (hire_id TEXT, target TEXT, snapshot TEXT, version INTEGER, applied_at TEXT,
  PRIMARY KEY (hire_id, target));
CREATE TABLE IF NOT EXISTS retries (hire_id TEXT, target TEXT, attempts INTEGER, next_attempt_at TEXT, last_error TEXT,
  PRIMARY KEY (hire_id, target));
CREATE TABLE IF NOT EXISTS gates (hire_id TEXT, gate TEXT, data TEXT, PRIMARY KEY (hire_id, gate));
CREATE TABLE IF NOT EXISTS exceptions (id TEXT PRIMARY KEY, hire_id TEXT, kind TEXT, state TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, hire_id TEXT, actor TEXT, action TEXT,
  body TEXT, prev_hash TEXT, hash TEXT);
"""


class StaleWrite(Exception):
    """Optimistic-concurrency failure: another writer saved this hire first."""


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._conn.in_transaction:          # nested use joins the outer transaction
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # --- events -------------------------------------------------------------------
    def event_seen(self, event_id: str) -> bool:
        return bool(self._q("SELECT 1 FROM events WHERE event_id=?", (event_id,)))

    def record_event(self, env: EventEnvelope, hire_id: str | None, outcome: str, now: datetime) -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                      (env.event_id, env.source, env.type, env.external_id, env.occurred_at.isoformat(),
                       now.isoformat(), hire_id, outcome, dumps(env.payload)))

    def events_for(self, hire_id: str) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT event_id, source, type, occurred_at, outcome FROM events WHERE hire_id=? ORDER BY received_at",
            (hire_id,))]

    # --- hires --------------------------------------------------------------------
    def get_hire(self, hire_id: str) -> HireRecord | None:
        rows = self._q("SELECT data FROM hires WHERE hire_id=?", (hire_id,))
        return HireRecord.model_validate_json(rows[0]["data"]) if rows else None

    def all_hires(self) -> list[HireRecord]:
        return [HireRecord.model_validate_json(r["data"]) for r in self._q("SELECT data FROM hires ORDER BY hire_id")]

    def save_hire(self, rec: HireRecord, expected_version: int | None) -> HireRecord:
        """Compare-and-set on version. expected_version=None means 'must not exist yet'."""
        with self.tx() as c:
            row = c.execute("SELECT version, data FROM hires WHERE hire_id=?", (rec.hire_id,)).fetchone()
            if expected_version is None and row is not None:
                raise StaleWrite(rec.hire_id)
            if expected_version is not None:
                if row is None or row["version"] != expected_version:
                    raise StaleWrite(rec.hire_id)
                # Source refs only ever grow, so a union can never lose a concurrent adopt.
                stored = json.loads(row["data"]).get("source_refs", {})
                rec.source_refs = {**stored, **rec.source_refs}
            data = rec.model_dump_json()
            ts = (rec.updated_at or rec.created_at or datetime.min).isoformat()
            c.execute("INSERT INTO hires VALUES (?,?,?,?) ON CONFLICT(hire_id) DO UPDATE SET "
                      "version=excluded.version, data=excluded.data, updated_at=excluded.updated_at",
                      (rec.hire_id, rec.version, data, ts))
            c.execute("INSERT OR REPLACE INTO hire_versions VALUES (?,?,?,?)", (rec.hire_id, rec.version, data, ts))
            for system, ext in rec.source_refs.items():
                c.execute("INSERT OR IGNORE INTO source_refs VALUES (?,?,?)", (system, ext, rec.hire_id))
        return rec

    def find_by_ref(self, system: str, external_id: str) -> str | None:
        rows = self._q("SELECT hire_id FROM source_refs WHERE system=? AND external_id=?", (system, external_id))
        return rows[0]["hire_id"] if rows else None

    def add_source_ref(self, hire_id: str, system: str, external_id: str) -> None:
        with self.tx() as c:
            row = c.execute("SELECT data FROM hires WHERE hire_id=?", (hire_id,)).fetchone()
            if row is None:
                return
            data = json.loads(row["data"])
            data.setdefault("source_refs", {})[system] = external_id
            c.execute("UPDATE hires SET data=? WHERE hire_id=?", (json.dumps(data), hire_id))
            # REPLACE: an explicit link (e.g. a human merging a duplicate) re-points the identity map.
            c.execute("INSERT OR REPLACE INTO source_refs VALUES (?,?,?)", (system, external_id, hire_id))

    # --- applied state (base of the three-way diff) -----------------------------------
    def get_applied(self, hire_id: str, target: str) -> dict | None:
        rows = self._q("SELECT snapshot FROM applied_state WHERE hire_id=? AND target=?", (hire_id, target))
        return json.loads(rows[0]["snapshot"]) if rows else None

    def put_applied(self, hire_id: str, target: str, snapshot: dict, version: int, now: datetime) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO applied_state VALUES (?,?,?,?,?)",
                      (hire_id, target, dumps(snapshot), version, now.isoformat()))

    # --- retries (outbox) ---------------------------------------------------------------
    def park(self, hire_id: str, target: str, error: str, next_at: datetime) -> int:
        with self.tx() as c:
            c.execute("INSERT INTO retries VALUES (?,?,1,?,?) ON CONFLICT(hire_id, target) DO UPDATE SET "
                      "attempts=attempts+1, next_attempt_at=excluded.next_attempt_at, last_error=excluded.last_error",
                      (hire_id, target, next_at.isoformat(), error))
            return c.execute("SELECT attempts FROM retries WHERE hire_id=? AND target=?",
                             (hire_id, target)).fetchone()["attempts"]

    def clear_retry(self, hire_id: str, target: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM retries WHERE hire_id=? AND target=?", (hire_id, target))

    def retries(self, hire_id: str | None = None) -> list[dict]:
        if hire_id:
            return [dict(r) for r in self._q("SELECT * FROM retries WHERE hire_id=?", (hire_id,))]
        return [dict(r) for r in self._q("SELECT * FROM retries")]

    # --- gates --------------------------------------------------------------------------
    def put_gates(self, hire_id: str, gates: list[GateState]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM gates WHERE hire_id=?", (hire_id,))
            for g in gates:
                c.execute("INSERT INTO gates VALUES (?,?,?)", (hire_id, g.gate, g.model_dump_json()))

    def gates_for(self, hire_id: str) -> list[GateState]:
        return [GateState.model_validate_json(r["data"]) for r in
                self._q("SELECT data FROM gates WHERE hire_id=? ORDER BY gate", (hire_id,))]

    # --- exceptions -----------------------------------------------------------------------
    def get_exception(self, ex_id: str) -> ExceptionItem | None:
        rows = self._q("SELECT data FROM exceptions WHERE id=?", (ex_id,))
        return ExceptionItem.model_validate_json(rows[0]["data"]) if rows else None

    def save_exception(self, item: ExceptionItem) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO exceptions VALUES (?,?,?,?,?)",
                      (item.id, item.hire_id, item.kind, item.state, item.model_dump_json()))

    def list_exceptions(self, hire_id: str | None = None, states: tuple[str, ...] | None = None) -> list[ExceptionItem]:
        sql, args = "SELECT data FROM exceptions WHERE 1=1", []
        if hire_id:
            sql += " AND hire_id=?"
            args.append(hire_id)
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            args.extend(states)
        items = [ExceptionItem.model_validate_json(r["data"]) for r in self._q(sql, tuple(args))]
        return sorted(items, key=lambda e: e.created_at)

    # --- audit (hash chain) ----------------------------------------------------------------
    def audit(self, hire_id: str | None, actor: str, action: str, details: dict, now: datetime) -> str:
        with self.tx() as c:
            prev = c.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = prev["hash"] if prev else "GENESIS"
            body = dumps({"ts": now.isoformat(), "hire_id": hire_id, "actor": actor, "action": action, "details": details})
            digest = hashlib.sha256((prev_hash + body).encode()).hexdigest()
            c.execute("INSERT INTO audit (ts, hire_id, actor, action, body, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                      (now.isoformat(), hire_id, actor, action, body, prev_hash, digest))
            return digest

    def audit_for(self, hire_id: str | None = None) -> list[dict]:
        rows = (self._q("SELECT * FROM audit WHERE hire_id=? ORDER BY seq", (hire_id,)) if hire_id
                else self._q("SELECT * FROM audit ORDER BY seq"))
        return [{"seq": r["seq"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
                 "details": json.loads(r["body"])["details"], "hash": r["hash"][:12]} for r in rows]

    def verify_audit(self) -> tuple[bool, int | None]:
        prev = "GENESIS"
        for r in self._q("SELECT seq, body, prev_hash, hash FROM audit ORDER BY seq"):
            if r["prev_hash"] != prev or hashlib.sha256((prev + r["body"]).encode()).hexdigest() != r["hash"]:
                return False, r["seq"]
            prev = r["hash"]
        return True, None
