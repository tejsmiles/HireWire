"""The orchestrator: ingest -> merge -> validate -> (save) -> reconcile -> monitor.

The engine is level-triggered. An event only updates the desired state, in one fast
transaction. Converging the targets is the reconciler's job: it runs straight away for
latency and again from the sweep for correctness. A crash or a missed webhook between the
two loses nothing, because the next sweep diffs desired against observed state and finishes
the job.
"""
from __future__ import annotations

import threading
from datetime import timedelta

from .config import Settings
from .matching import Unmatched, resolve_hire
from .mocks import FakeWorld
from .models import EventEnvelope, FieldProvenance, HireRecord, OpResult
from .monitoring import ExceptionDesk, GateMonitor
from .normalize import NormalizationError, normalize
from .ownership import hr_owner, merge
from .reconcile import Executor, Planner
from .store import StaleWrite, Store
from .targets import Target
from .timeutil import Clock, add_business_days
from .validation import Validator

RECONCILE_KINDS = {"conflict", "email_mismatch", "equipment_after_ship", "apply_failed", "deprovision_pending"}


class Engine:
    def __init__(self, store: Store, world: FakeWorld, targets: dict[str, Target], advisor, clock: Clock,
                 settings: Settings, sleep=None):
        self.store, self.world, self.targets, self.advisor = store, world, targets, advisor
        self.clock, self.settings = clock, settings
        self.validator = Validator(world.workday.manager_exists, advisor, clock, settings)
        self.planner = Planner(targets, store)
        self.executor = Executor(targets, store, settings, clock, **({"sleep": sleep} if sleep else {}))
        self.desk = ExceptionDesk(store, advisor, world.slack, clock, settings)
        self.gates = GateMonitor(store, targets, clock, settings, self.desk)
        self._lock = threading.RLock()     # prototype: serial per process. Production: per-hire advisory lock.
        self._auto: list[tuple[str, str]] = []

    def _audit(self, hire_id: str | None, actor: str, action: str, details: dict) -> None:
        self.store.audit(hire_id, actor, action, details, self.clock.now())

    # ------------------------------------------------------------------ ingest
    def handle_event(self, env: EventEnvelope) -> dict:
        with self._lock:
            for _ in range(3):
                try:
                    result = self._ingest(env)
                    break
                except StaleWrite:          # someone saved this hire first: re-read and re-merge
                    continue
            else:
                raise RuntimeError(f"could not apply {env.event_id}: persistent write contention")
            self._drain_auto()
            return result

    def _ingest(self, env: EventEnvelope) -> dict:
        now = self.clock.now()
        if self.store.event_seen(env.event_id):
            return {"status": "duplicate", "event_id": env.event_id}
        try:
            norm = normalize(env)
            rec, how, dup_of = resolve_hire(self.store, env, norm, now)
        except (NormalizationError, Unmatched) as e:
            self.store.record_event(env, None, "rejected", now)
            self._audit(None, f"source:{env.source}", "ingest.rejected", {"event_id": env.event_id, "error": str(e)})
            return {"status": "rejected", "event_id": env.event_id, "error": str(e)}

        is_new = how in ("new", "possible_duplicate")
        expected = None if is_new else rec.version
        m = merge(rec, source=env.source, occurred_at=env.occurred_at, event_id=env.event_id, fields=norm.fields)
        rec = m.record
        lifecycle = None
        if norm.signal == "cancel" and hr_owner(rec) == env.source and rec.lifecycle != "cancelled":
            lifecycle = {"from": rec.lifecycle, "to": "cancelled"}
            rec.lifecycle = "cancelled"
        changed = bool(m.changes or lifecycle or is_new)
        if not changed and not m.fresh and how == "source_ref":
            outcome = "stale" if m.stale else "no_change"
            self.store.record_event(env, rec.hire_id, outcome, now)
            self._audit(rec.hire_id, f"source:{env.source}", f"ingest.{outcome}",
                        {"event_id": env.event_id, "type": env.type, "stale_fields": m.stale})
            return {"status": outcome, "hire_id": rec.hire_id, "event_id": env.event_id, "stale_fields": m.stale}

        verdict = self.validator.validate(rec)
        rec = verdict.record
        if changed or verdict.normalizations:
            rec.version += 1
            rec.updated_at = now
        dup_hold = how == "possible_duplicate" or self._is_open(rec.hire_id, "possible_duplicate")
        if dup_hold or verdict.blocks:
            rec.sync_state = "held"
        elif rec.sync_state in ("new", "held"):
            rec.sync_state = "pending"
        self.store.save_hire(rec, expected)
        self.store.record_event(env, rec.hire_id, "accepted", now)
        out = {"status": "accepted", "hire_id": rec.hire_id, "event_id": env.event_id, "matched_by": how,
               "version": rec.version, "changes": [c.as_dict() for c in m.changes],
               "drift": [c.as_dict() for c in m.drift], "stale_fields": m.stale, "lifecycle": lifecycle,
               "findings": [f.as_dict() for f in verdict.findings], "normalizations": verdict.normalizations}
        self._audit(rec.hire_id, f"source:{env.source}", "ingest", {k: v for k, v in out.items() if k != "status"})
        for n in verdict.normalizations:
            self._audit(rec.hire_id, "claude" if str(n.get("via", "")).startswith("claude") else "engine",
                        "normalize", n)

        if dup_hold:
            if how == "possible_duplicate":
                self._raise(rec, kind="possible_duplicate", key="possible_duplicate",
                            summary=f"{rec.display_name} from {env.source} looks like existing hire {dup_of}",
                            details={"duplicate_of": dup_of, "source": env.source, "external_id": env.external_id})
            return {**out, "status": "held"}
        if verdict.blocks:
            self._raise(rec, kind="validation_blocked", key="validation", owner="Recruiting", severity="high",
                        summary="; ".join(f.message for f in verdict.blocks),
                        details={"findings": [f.as_dict() for f in verdict.blocks]})
            return {**out, "status": "held"}
        self.desk.clear(rec.hire_id, "validation", "validation passed")
        out["reconcile"] = self._reconcile(rec.hire_id)
        return out

    # ------------------------------------------------------------------ reconcile
    def reconcile(self, hire_id: str, approved: frozenset[str] = frozenset()) -> dict:
        with self._lock:
            result = self._reconcile(hire_id, approved)
            self._drain_auto()
            return result

    def _reconcile(self, hire_id: str, approved: frozenset[str] = frozenset()) -> dict:
        rec = self.store.get_hire(hire_id)
        if rec is None or rec.sync_state == "held":
            return {"status": "held"}
        now, expected = self.clock.now(), rec.version
        passes, latest, learned_all = [], {}, {}
        # Converge to a fixpoint: a create can unlock dependents (Okta -> Lumos) or teach us a
        # fact another target displays (the Okta login -> tracker). Bounded to 3 passes.
        for n in range(1, 4):
            plan = self.planner.plan(rec)
            backoff = frozenset(t for t, r in latest.items() if r.status in ("parked", "failed"))
            results, rec, learned = self.executor.execute(plan, rec, approved, backoff)
            learned_all.update(learned)
            for r in results:
                if r.status != "noop" or r.target not in latest:
                    latest[r.target] = r
            passes.append({"pass": n, "plan": plan.summary(), "results": [
                _fmt(r) for r in results if r.status not in ("noop", "skipped")
                and not (n > 1 and r.status in ("held", "blocked"))]})
            if not any(r.status in ("ok", "adopted") for r in results):
                break
        if learned_all:
            for f in learned_all:
                rec.provenance[f] = FieldProvenance(source="okta", at=now)
            rec.version += 1
            rec.updated_at = now

        present: set[str] = set()
        for op in plan.ops:
            if op.kind == "conflict":
                key = f"conflict:{op.target}"
                present.add(key)
                self._raise(rec, kind="conflict", key=key, severity="high",
                            summary=f"{op.target} was edited outside the engine ({', '.join(op.fields)})",
                            details={"target": op.target, "fields": op.fields, "ours": op.desired,
                                     "theirs": op.observed, "base": op.base})
        for a in plan.advisories:
            present.add(a.key)
            self._raise(rec, kind=a.kind, key=a.key, summary=a.summary, details=a.details, owner=a.owner,
                        severity=a.severity)
        held = [t for t, r in latest.items() if r.status == "held"]
        if held:
            present.add("deprovision")
            self._raise(rec, kind="deprovision_pending", key="deprovision", severity="high",
                        summary=f"Offer cancelled: approve deprovisioning {', '.join(held)}",
                        details={"targets": held})
        for target, r in latest.items():
            key = f"apply_failed:{target}"
            if r.status == "failed":
                present.add(key)
                self._raise(rec, kind="apply_failed", key=key, owner="IT", severity="high",
                            summary=f"{target} rejected the change: {r.error}",
                            details={"target": target, "op": r.kind, "error": r.error, "code": r.code})
            elif r.status == "parked":
                attempts = self.store.park(rec.hire_id, target, r.error or "",
                                           now + timedelta(minutes=self.settings.retry_park_minutes))
                if attempts >= self.settings.retry_escalate_after:
                    present.add(key)
                    self._raise(rec, kind="apply_failed", key=key, owner="IT", severity="high",
                                summary=f"{target} still failing after {attempts} reconcile passes: {r.error}",
                                details={"target": target, "op": r.kind, "error": r.error, "code": r.code,
                                         "attempts": attempts})
            elif r.status in ("ok", "adopted", "noop"):
                self.store.clear_retry(rec.hire_id, target)
        self.desk.clear_absent(rec.hire_id, present, RECONCILE_KINDS)

        gates = self.gates.recompute(rec)
        open_items = self.store.list_exceptions(rec.hire_id, states=("open", "awaiting_approval"))
        statuses = {r.status for r in latest.values()}
        if statuses & {"conflict", "failed", "held"} or any(e.kind != "gate" for e in open_items):
            rec.sync_state = "attention"
        elif statuses & {"parked", "blocked"}:
            rec.sync_state = "pending"
        else:
            rec.sync_state = "in_sync"
        self.store.save_hire(rec, expected)
        summary = {"passes": passes, "sync_state": rec.sync_state, "learned": learned_all,
                   "gates": {g.gate: g.status for g in gates},
                   "open_exceptions": [f"{e.kind}:{e.key}" for e in open_items]}
        self._audit(rec.hire_id, "engine", "reconcile", summary)
        return summary

    def diff(self, hire_id: str) -> dict:
        """Dry run: what the engine would do right now (no writes)."""
        rec = self.store.get_hire(hire_id)
        verdict = self.validator.validate(rec)
        plan = self.planner.plan(verdict.record)
        return {"hire_id": hire_id, "version": rec.version, "sync_state": rec.sync_state,
                "validation": [f.as_dict() for f in verdict.findings],
                "plan": [op.model_dump(exclude_none=True, exclude_defaults=True) for op in plan.ops],
                "advisories": [a.model_dump() for a in plan.advisories]}

    # ------------------------------------------------------------------ sweep
    def sweep(self) -> dict:
        """Anti-entropy pass (n8n cron). Flips lifecycle on day 1, re-reconciles every active
        hire (catching out-of-band drift and retrying parked targets), and refreshes gates."""
        with self._lock:
            today, report = self.clock.today(), {"started": [], "reconciled": {}}
            for rec in self.store.all_hires():
                if rec.sync_state == "held":
                    continue
                if rec.lifecycle == "pre_start" and rec.start_date and rec.start_date <= today:
                    expected = rec.version
                    rec.lifecycle, rec.version, rec.updated_at = "started", rec.version + 1, self.clock.now()
                    self.store.save_hire(rec, expected)
                    self._audit(rec.hire_id, "engine", "lifecycle.started",
                                {"note": "day 1: HR fields are now owned by Workday"})
                    report["started"].append(rec.hire_id)
                if rec.lifecycle == "started" and rec.start_date and today > rec.start_date + timedelta(days=30):
                    continue
                report["reconciled"][rec.hire_id] = self._reconcile(rec.hire_id)
            self._audit(None, "engine", "sweep", {"started": report["started"], "reconciled": {
                h: r.get("sync_state") for h, r in report["reconciled"].items()}})
            self._drain_auto()
            return report

    # ------------------------------------------------------------------ exceptions
    def _raise(self, rec: HireRecord, **kw) -> None:
        item = self.desk.raise_(rec, **kw)
        if item and item.recommendation and item.recommendation.get("action") == "auto_resolve":
            self._auto.append((item.id, item.recommendation["recommended_option"]))

    def _drain_auto(self) -> None:
        for _ in range(10):
            if not self._auto:
                return
            ex_id, option = self._auto.pop(0)
            self.resolve_exception(ex_id, option, actor="claude")

    def resolve_exception(self, ex_id: str, option: str, actor: str) -> dict:
        with self._lock:
            ex = self.store.get_exception(ex_id)
            if ex is None:
                raise KeyError(ex_id)
            if ex.state not in ("open", "awaiting_approval"):
                return {"status": f"already_{ex.state}", "exception_id": ex_id}   # double clicks are harmless
            if option not in ex.options:
                raise ValueError(f"'{option}' is not an option for {ex.kind}: {sorted(ex.options)}")
            now = self.clock.now()
            ex.state, ex.updated_at = "resolved", now
            ex.resolution = {"option": option, "by": actor, "at": now.isoformat()}
            self.store.save_exception(ex)
            self._audit(ex.hire_id, actor, "exception.resolve",
                        {"exception_id": ex_id, "kind": ex.kind, "option": option,
                         "recommended": (ex.recommendation or {}).get("recommended_option")})
            try:
                outcome = getattr(self, f"_resolve_{ex.kind}")(ex, option, actor)
            except Exception as e:      # the chosen action failed: reopen so it isn't lost
                ex.state, ex.updated_at = "awaiting_approval", self.clock.now()
                ex.resolution = {"option": option, "by": actor, "error": str(e)}
                self.store.save_exception(ex)
                self._audit(ex.hire_id, actor, "exception.resolve_failed", {"exception_id": ex_id, "error": str(e)})
                return {"status": "failed", "exception_id": ex_id, "option": option, "error": str(e)}
            self._drain_auto()
            status = "resolved" if ex.kind != "gate" else {"snooze_1d": "snoozed", "escalate": "escalated"}[option]
            return {"status": status, "exception_id": ex_id, "option": option, "outcome": outcome}

    def _is_open(self, hire_id: str, key: str) -> bool:
        from .monitoring import exception_id
        item = self.store.get_exception(exception_id(hire_id, key))
        return bool(item and item.state in ("open", "awaiting_approval"))

    def _commit(self, rec: HireRecord, expected: int, actor: str) -> dict:
        """Save a human-driven canonical change through the same admission gate as events."""
        verdict = self.validator.validate(rec)
        rec = verdict.record
        rec.version, rec.updated_at = rec.version + 1, self.clock.now()
        rec.sync_state = "held" if verdict.blocks else "pending"
        self.store.save_hire(rec, expected)
        if verdict.blocks:
            self._raise(rec, kind="validation_blocked", key="validation", owner="Recruiting", severity="high",
                        summary="; ".join(f.message for f in verdict.blocks),
                        details={"findings": [f.as_dict() for f in verdict.blocks]})
            return {"status": "held"}
        return self._reconcile(rec.hire_id)

    def _resolve_conflict(self, ex, option, actor):
        target, theirs = ex.details["target"], ex.details["theirs"]
        rec = self.store.get_hire(ex.hire_id)
        base = self.store.get_applied(rec.hire_id, target) or {}
        # Either way, the out-of-band value is acknowledged as the new base for that target.
        self.store.put_applied(rec.hire_id, target, {**base, **theirs}, rec.version, self.clock.now())
        if option == "keep_ours":
            return self._reconcile(rec.hire_id)            # observed == base now -> safe update with ours
        expected = rec.version
        for f, v in theirs.items():
            cf, cv = self.targets[target].to_canonical(f, v)
            setattr(rec, cf, cv)
            rec.provenance[cf] = FieldProvenance(source=actor, at=self.clock.now())
        return self._commit(rec, expected, actor)

    def _resolve_email_mismatch(self, ex, option, actor):
        rec, okta = self.store.get_hire(ex.hire_id), self.targets["okta"]
        expected, uid = rec.version, rec.source_refs["okta"]
        if option == "rename_login":
            new = okta.free_login(ex.details["expected_login"], rec.hire_id)
            okta.rename_login(uid, new)
            rec.work_email_aliases = [*rec.work_email_aliases, ex.details["current_login"]]
            rec.work_email = new
        else:
            okta.add_alias(uid, ex.details["expected_login"])
            rec.work_email_aliases = [*rec.work_email_aliases, ex.details["expected_login"]]
        rec.provenance["work_email"] = FieldProvenance(source=actor, at=self.clock.now())
        return self._commit(rec, expected, actor)

    def _resolve_deprovision_pending(self, ex, option, actor):
        if option == "approve":
            return self._reconcile(ex.hire_id, approved=frozenset({"deprovision"}))
        rec = self.store.get_hire(ex.hire_id)
        expected = rec.version
        rec.lifecycle = "pre_start"
        return self._commit(rec, expected, actor)

    def _resolve_equipment_after_ship(self, ex, option, actor):
        return {"note": "acknowledged: IT is handling the redirect with ExpoIT"}

    def _resolve_apply_failed(self, ex, option, actor):
        self.store.clear_retry(ex.hire_id, ex.details["target"])
        return self._reconcile(ex.hire_id) if option == "retry_now" else {"note": "fixed by hand"}

    def _resolve_validation_blocked(self, ex, option, actor):
        rec = self.store.get_hire(ex.hire_id)
        return self._commit(rec, rec.version, actor)

    def _resolve_possible_duplicate(self, ex, option, actor):
        rec = self.store.get_hire(ex.hire_id)
        expected = rec.version
        if option == "keep_separate":
            return self._commit(rec, expected, actor)
        orig = self.store.get_hire(ex.details["duplicate_of"])
        src, ext = ex.details["source"], ex.details["external_id"]
        if src in orig.source_refs:
            note = f"{orig.hire_id} already has {src} {orig.source_refs[src]}: rescind duplicate {ext} in {src}"
        else:
            self.store.add_source_ref(orig.hire_id, src, ext)
            note = f"attached {src} {ext} to {orig.hire_id}"
        rec.lifecycle, rec.sync_state, rec.version = "cancelled", "held", rec.version + 1
        self.store.save_hire(rec, expected)
        return {"linked_to": orig.hire_id, "note": note}

    def _resolve_gate(self, ex, option, actor):
        ex.state, ex.resolution = "open", {"option": option, "by": actor, "at": self.clock.now().isoformat()}
        if option == "snooze_1d":
            ex.snoozed_until = add_business_days(self.clock.today(), 1)
        else:
            ex.severity = "high"
            self.world.slack.post("#people-ops-leads", f"Escalated by {actor}: {ex.summary} (hire {ex.hire_id})")
        self.store.save_exception(ex)
        return {"snoozed_until": str(ex.snoozed_until)} if option == "snooze_1d" else {"escalated": True}


def _fmt(r: OpResult) -> str:
    s = f"{r.target}:{r.kind}->{r.status}"
    if r.external_id and r.kind == "create":
        s += f" {r.external_id}"
    if r.attempts > 1:
        s += f" after {r.attempts} attempts"
    if r.error and r.status not in ("ok", "adopted"):
        s += f" ({r.error})"
    return s
