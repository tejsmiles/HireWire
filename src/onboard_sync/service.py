"""Composition root and facade. The API, CLI, MCP server and tests all go through Service."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .claude import make_advisor
from .config import Settings
from .engine import Engine
from .mocks import FakeWorld
from .models import EventEnvelope
from .store import Store
from .targets import build_targets
from .timeutil import Clock


class Service:
    def __init__(self, db_path: str = ":memory:", world_path: str | Path | None = None, clock: Clock | None = None,
                 settings: Settings | None = None, advisor=None, sleep=None):
        self.settings = settings or Settings()
        self.clock = clock or Clock()
        self.world = FakeWorld(self.clock, world_path)
        if clock is None and self.world.state.get("clock"):
            self.clock.set(datetime.fromisoformat(self.world.state["clock"]))   # resume scenario time
        self.store = Store(db_path)
        self.targets = build_targets(self.world, self.settings)
        self.advisor = advisor or make_advisor(self.settings)
        self.engine = Engine(self.store, self.world, self.targets, self.advisor, self.clock, self.settings, sleep)

    @classmethod
    def from_env(cls) -> "Service":
        var = Path(os.getenv("ONBOARD_SYNC_HOME", "var"))
        var.mkdir(parents=True, exist_ok=True)
        return cls(os.getenv("ONBOARD_SYNC_DB", str(var / "onboard.db")),
                   os.getenv("ONBOARD_SYNC_WORLD", str(var / "world.json")))

    # --- commands ---------------------------------------------------------------------
    def ingest(self, env: EventEnvelope | dict) -> dict:
        return self.engine.handle_event(env if isinstance(env, EventEnvelope) else EventEnvelope.model_validate(env))

    def sweep(self) -> dict:
        return self.engine.sweep()

    def resolve(self, exception_id: str, option: str, actor: str) -> dict:
        return self.engine.resolve_exception(exception_id, option, actor)

    # --- queries ----------------------------------------------------------------------
    def find_hire(self, query: str) -> str | None:
        if self.store.get_hire(query):
            return query
        q = query.casefold()
        for rec in self.store.all_hires():
            if q in rec.display_name.casefold() or q in (rec.legal_first_name or "").casefold():
                return rec.hire_id
        return None

    def hire(self, hire_id: str) -> dict:
        rec = self.store.get_hire(hire_id)
        if rec is None:
            raise KeyError(hire_id)
        return {
            "record": rec.model_dump(mode="json", exclude={"source_snapshots", "provenance"}),
            "provenance": {k: f"{v.source} @ {v.at.isoformat()}" for k, v in rec.provenance.items()},
            "gates": [g.model_dump(mode="json") for g in self.store.gates_for(hire_id)],
            "exceptions": [self.exception_view(e.id) for e in self.store.list_exceptions(hire_id)],
            "retries": self.store.retries(hire_id),
            "events": self.store.events_for(hire_id),
        }

    def hires(self) -> list[dict]:
        out = []
        for r in self.store.all_hires():
            open_ex = self.store.list_exceptions(r.hire_id, states=("open", "awaiting_approval"))
            out.append({"hire_id": r.hire_id, "name": r.display_name, "start_date": str(r.start_date),
                        "lifecycle": r.lifecycle, "sync_state": r.sync_state, "origin": r.origin,
                        "systems": sorted(r.source_refs), "open_exceptions": len(open_ex)})
        return out

    def exception_view(self, ex_id: str) -> dict:
        e = self.store.get_exception(ex_id)
        if e is None:
            raise KeyError(ex_id)
        return {"id": e.id, "hire_id": e.hire_id, "kind": e.kind, "state": e.state, "severity": e.severity,
                "owner": e.owner, "summary": e.summary, "details": e.details, "options": e.options,
                "recommendation": e.recommendation, "resolution": e.resolution,
                "snoozed_until": str(e.snoozed_until) if e.snoozed_until else None}

    def exceptions(self, include_closed: bool = False) -> list[dict]:
        states = None if include_closed else ("open", "awaiting_approval")
        return [self.exception_view(e.id) for e in self.store.list_exceptions(states=states)]

    def diff(self, hire_id: str) -> dict:
        return self.engine.diff(hire_id)

    def audit(self, hire_id: str | None = None) -> list[dict]:
        return self.store.audit_for(hire_id)

    def verify_audit(self) -> dict:
        ok, bad = self.store.verify_audit()
        return {"ok": ok, "first_bad_seq": bad, "entries": len(self.store.audit_for())}
