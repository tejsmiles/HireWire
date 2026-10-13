"""Plan and execute: the heart of change propagation.

Planner: a three-way diff per target.
    desired  = target.project(canonical)
    observed = target.fetch(external id)
    base     = the last snapshot WE wrote to that target (applied_state)
  For each field where desired != observed:
    observed == base  -> only the canonical record moved      -> safe UPDATE
    observed != base  -> someone edited the target out of band -> CONFLICT (never overwrite blindly)

Executor: applies the plan in dependency order. Idempotency is enforced in three layers:
    ingest  event_id dedupe (engine)
    state   version compare-and-set + per-field ordering (store / ownership)
    write   lookup-by-natural-key before every create, adopt-on-409, and idempotency keys
            (one create key per (hire, target) forever; update keys scoped to the version)
Transient errors retry with backoff and jitter. If they're exhausted, the target is parked,
and the sweep later re-runs the *reconciliation* rather than replaying the stale operation.
"""
from __future__ import annotations

import hashlib
import random
import time
from typing import Callable

from .config import SAFE_TARGETS, TARGET_ORDER, Settings
from .errors import AlreadyExists, PermanentError, TransientError
from .models import HireRecord, OpResult, Plan, PlanOp
from .store import Store
from .targets import Target
from .timeutil import Clock


class Planner:
    def __init__(self, targets: dict[str, Target], store: Store):
        self.targets, self.store = targets, store

    def plan(self, rec: HireRecord) -> Plan:
        ops: list[PlanOp] = []
        advisories = []
        for name in TARGET_ORDER:
            t, ext = self.targets[name], rec.source_refs.get(name)
            if rec.lifecycle == "cancelled":
                applied = self.store.get_applied(rec.hire_id, name) or {}
                if ext and not applied.get("_deprovisioned"):
                    ops.append(PlanOp(target=name, kind="deprovision", reason="offer cancelled",
                                      requires_approval=name not in SAFE_TARGETS))
                else:
                    ops.append(PlanOp(target=name, kind="noop"))
                continue
            reason = t.skip_reason(rec)
            if reason and not ext:
                ops.append(PlanOp(target=name, kind="skip", reason=reason))
                continue
            observed = t.fetch(ext) if ext else None
            desired = t.project(rec, observed)
            if observed is None:
                ops.append(PlanOp(target=name, kind="create", desired=desired,
                                  depends_on=[d for d in t.depends_on if not rec.source_refs.get(d)]))
            else:
                base = self.store.get_applied(rec.hire_id, name)
                drift = sorted(k for k, v in desired.items() if observed.get(k) != v)
                oob = [k for k in drift if t.detect_conflicts and base is not None and k in base
                       and observed.get(k) != base[k]]
                safe = [k for k in drift if k not in oob]
                if safe:
                    ops.append(PlanOp(target=name, kind="update", fields=safe,
                                      desired={k: desired[k] for k in safe},
                                      observed={k: observed.get(k) for k in safe}))
                if oob:
                    ops.append(PlanOp(target=name, kind="conflict", fields=oob,
                                      desired={k: desired[k] for k in oob},
                                      observed={k: observed.get(k) for k in oob},
                                      base={k: base.get(k) for k in oob}))
                if not drift:
                    ops.append(PlanOp(target=name, kind="noop"))
            advisories += t.advisories(rec, observed)
        return Plan(hire_id=rec.hire_id, version=rec.version, ops=ops, advisories=advisories)


def idempotency_key(hire_id: str, target: str, kind: str, version: int) -> str:
    # A hire gets exactly one create per target, ever, so that key is version-independent.
    # Updates are scoped to the desired-state version so a later correction is never deduped away.
    scope = "create" if kind == "create" else f"{kind}:v{version}"
    return hashlib.sha256(f"{hire_id}:{target}:{scope}".encode()).hexdigest()[:24]


class Executor:
    def __init__(self, targets: dict[str, Target], store: Store, settings: Settings, clock: Clock,
                 sleep: Callable[[float], None] = time.sleep):
        self.targets, self.store, self.settings, self.clock, self.sleep = targets, store, settings, clock, sleep

    def execute(self, plan: Plan, rec: HireRecord, approved: frozenset[str] = frozenset(),
                backoff: frozenset[str] = frozenset()) -> tuple[list[OpResult], HireRecord, dict]:
        """Returns (results, record with new refs/learned facts, learned canonical updates).
        Targets in `backoff` already failed earlier in this run and are not retried again."""
        rec = rec.model_copy(deep=True)
        learned: dict = {}
        results: list[OpResult] = []
        for op in plan.ops:
            t = self.targets[op.target]
            if op.target in backoff and op.kind in ("create", "update", "deprovision"):
                continue
            if op.kind in ("noop", "skip", "conflict"):
                status = {"noop": "noop", "skip": "skipped", "conflict": "conflict"}[op.kind]
                results.append(OpResult(target=op.target, kind=op.kind, status=status, error=op.reason))
            elif op.requires_approval and op.kind not in approved:
                results.append(OpResult(target=op.target, kind=op.kind, status="held", error="awaiting human approval"))
            elif op.kind == "create" and (missing := [d for d in t.depends_on if not rec.source_refs.get(d)]):
                results.append(OpResult(target=op.target, kind=op.kind, status="blocked",
                                        error=f"waiting on {', '.join(missing)}"))
            else:
                results.append(self._apply(op, t, rec, learned))
        return results, rec, learned

    def _backoff(self, attempt: int) -> float:
        return min(30.0, self.settings.retry_base_seconds * 2 ** (attempt - 1)) * (0.5 + random.random() / 2)

    def _apply(self, op: PlanOp, t: Target, rec: HireRecord, learned: dict) -> OpResult:
        key = idempotency_key(rec.hire_id, op.target, op.kind, rec.version)
        last: Exception | None = None
        for attempt in range(1, self.settings.retry_max_attempts + 1):
            try:
                if op.kind == "create":
                    return self._create(t, rec, key, attempt, learned)
                ext = rec.source_refs[op.target]
                if op.kind == "update":
                    t.update(ext, op.desired, key)
                    base = self.store.get_applied(rec.hire_id, op.target) or {}
                    self.store.put_applied(rec.hire_id, op.target, {**base, **op.desired}, rec.version, self.clock.now())
                else:  # deprovision
                    t.deprovision(ext, key)
                    self.store.put_applied(rec.hire_id, op.target, {"_deprovisioned": True}, rec.version,
                                           self.clock.now())
                return OpResult(target=op.target, kind=op.kind, status="ok", external_id=ext, attempts=attempt)
            except TransientError as e:
                last = e
                if attempt < self.settings.retry_max_attempts:
                    self.sleep(self._backoff(attempt))
            except (PermanentError, AlreadyExists) as e:
                return OpResult(target=op.target, kind=op.kind, status="failed", error=str(e), code=e.code,
                                attempts=attempt)
        return OpResult(target=op.target, kind=op.kind, status="parked", error=str(last),
                        code=getattr(last, "code", None), attempts=self.settings.retry_max_attempts)

    def _create(self, t: Target, rec: HireRecord, key: str, attempt: int, learned: dict) -> OpResult:
        desired = t.project(rec, None)          # late-bind refs created earlier in this pass
        ext = t.find_existing(rec)              # lookup-before-create on every attempt
        status = "adopted" if ext else "ok"
        if not ext:
            try:
                ext = t.create(rec, desired, key)
            except AlreadyExists:
                ext = t.find_existing(rec)
                if not ext:
                    raise PermanentError(409, "COLLISION", f"{t.name}: a record exists but is not this hire's")
                status = "adopted"
        rec.source_refs[t.name] = ext
        self.store.add_source_ref(rec.hire_id, t.name, ext)
        # Base for the next diff: what we wrote, or (when adopting) what was already there.
        if status == "ok":
            snapshot = desired
        else:
            observed = t.fetch(ext) or {}
            snapshot = {k: observed.get(k) for k in desired}
        self.store.put_applied(rec.hire_id, t.name, snapshot, rec.version, self.clock.now())
        for f, v in t.after_create(rec, ext).items():
            if getattr(rec, f) != v:
                setattr(rec, f, v)
                learned[f] = v
        return OpResult(target=t.name, kind="create", status=status, external_id=ext, attempts=attempt)
