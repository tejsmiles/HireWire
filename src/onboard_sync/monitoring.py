"""Exception desk and gate monitor: the monitoring slice, which falls out of the engine.

The desk turns conditions into exceptions:
  - Deterministic ids (hire + key), so a condition seen twice is one exception.
  - A fingerprint of the facts, so the same facts never re-triage or re-notify.
  - Claude triage recommends an option, guardrails apply, then Slack posts an approval card.
  - Conditions that clear on their own (a data fix, a moved start date) auto-resolve.
  - A human acknowledgement sticks until the facts change.

Gates are *observed* from systems, never ticked by hand. Due dates are computed from the
start date on every pass, so when the start date moves, every deadline moves with it.
"""
from __future__ import annotations

import hashlib

from .claude import Decision, apply_guardrails, fallback_decision, redact_for_llm
from .config import GATE_RULES, SLACK_CHANNELS, Settings
from .mocks import FakeSlack
from .models import ExceptionItem, GateState, HireRecord
from .ownership import wire
from .slack import approval_blocks, nudge_blocks
from .store import Store, dumps
from .timeutil import Clock, add_business_days, business_days_until

OPTIONS: dict[str, dict[str, str]] = {
    "validation_blocked": {"recheck": "Re-run validation (after the source system was fixed)"},
    "conflict": {"accept_theirs": "Adopt the value edited in the target and propagate it everywhere",
                 "keep_ours": "Re-apply the canonical value, overwriting the out-of-band edit"},
    "email_mismatch": {"rename_login": "Rename the Okta login/email to match the corrected name (old one kept as alias)",
                       "add_alias": "Keep the existing login; add the corrected address as an alias"},
    "deprovision_pending": {"approve": "Deprovision every system for this cancelled hire",
                            "reject": "Keep the accounts and reinstate the hire"},
    "equipment_after_ship": {"mark_resolved": "IT arranged a carrier redirect / return-and-reship"},
    "apply_failed": {"retry_now": "Retry the reconciliation now", "mark_resolved": "Fixed by hand in the target"},
    "possible_duplicate": {"link": "Same person: attach this record to the existing hire",
                           "keep_separate": "Different people: release the hold"},
    "gate": {"snooze_1d": "Snooze reminders for one business day", "escalate": "Escalate to the People Ops leads"},
}
DEFAULT_OWNER = {"validation_blocked": "Recruiting", "conflict": "People Ops", "email_mismatch": "IT",
                 "deprovision_pending": "People Ops", "equipment_after_ship": "IT", "apply_failed": "IT",
                 "possible_duplicate": "People Ops"}
ACK_KINDS = {"equipment_after_ship", "apply_failed"}   # a human acknowledgement sticks until the facts change
MACHINE_ACTORS = {"engine", "claude"}


def exception_id(hire_id: str, key: str) -> str:
    return "ex_" + hashlib.sha1(f"{hire_id}:{key}".encode()).hexdigest()[:10]


def fingerprint(details: dict) -> str:
    return hashlib.sha1(dumps(details).encode()).hexdigest()[:12]


class ExceptionDesk:
    def __init__(self, store: Store, advisor, slack: FakeSlack, clock: Clock, settings: Settings):
        self.store, self.advisor, self.slack, self.clock, self.settings = store, advisor, slack, clock, settings

    def raise_(self, rec: HireRecord, *, kind: str, key: str, summary: str, details: dict,
               owner: str | None = None, severity: str = "medium") -> ExceptionItem | None:
        now, ex_id, fp = self.clock.now(), exception_id(rec.hire_id, key), fingerprint(details)
        existing = self.store.get_exception(ex_id)
        active = existing is not None and existing.state in ("open", "awaiting_approval")
        if active and existing.fingerprint == fp:
            return existing                                # same facts: no re-triage, no re-notify
        if (existing and not active and kind in ACK_KINDS and existing.fingerprint == fp
                and existing.resolution and existing.resolution.get("by") not in MACHINE_ACTORS):
            return None                                    # a human already handled exactly these facts
        item = ExceptionItem(
            id=ex_id, hire_id=rec.hire_id, kind=kind, key=key, severity=severity,
            owner=owner or DEFAULT_OWNER.get(kind, "People Ops"), summary=summary, details=details,
            fingerprint=fp, options=OPTIONS[kind], created_at=existing.created_at if active else now,
            updated_at=now, snoozed_until=existing.snoozed_until if active else None)
        decision = self._triage(item, rec)
        item.recommendation = decision.model_dump()
        if decision.action != "auto_resolve":
            item.state = "awaiting_approval"
            self.slack.post(SLACK_CHANNELS[item.owner], f"[{kind}] {rec.display_name}: {summary}",
                            approval_blocks(item, rec, decision))
            item.notified_at = now
        self.store.save_exception(item)
        self.store.audit(rec.hire_id, "engine", "exception.raise",
                         {"exception_id": ex_id, "kind": kind, "summary": summary,
                          "recommendation": {k: item.recommendation[k] for k in
                                             ("recommended_option", "action", "confidence", "source")}}, now)
        return item

    def _triage(self, item: ExceptionItem, rec: HireRecord) -> Decision:
        today = self.clock.today()
        ctx = {"hire": redact_for_llm(rec), "today": today.isoformat(),
               "business_days_to_start": business_days_until(today, rec.start_date) if rec.start_date else None,
               "recent_activity": [a["action"] for a in self.store.audit_for(rec.hire_id)[-6:]]}
        exc = {"kind": item.kind, "summary": item.summary, "details": item.details}
        try:
            d = self.advisor.triage(exc, ctx, item.options)
        except Exception as e:                             # LLM outage never blocks the pipeline
            d = fallback_decision(f"{type(e).__name__}: {e}")
        return apply_guardrails(d, item.kind, item.options, self.settings)

    def raise_gate(self, rec: HireRecord, g: GateState) -> ExceptionItem:
        now, today = self.clock.now(), self.clock.today()
        details = {"gate": g.gate, "due_date": g.due_date.isoformat(), "status": g.status, "owner": g.owner,
                   "start_date": wire(rec.start_date)}
        ex_id, fp = exception_id(rec.hire_id, f"gate:{g.gate}"), fingerprint(details)
        existing = self.store.get_exception(ex_id)
        active = existing is not None and existing.state in ("open", "awaiting_approval")
        item = existing if active and existing.fingerprint == fp else ExceptionItem(
            id=ex_id, hire_id=rec.hire_id, kind="gate", key=f"gate:{g.gate}", owner=g.owner,
            severity="high" if g.status == "overdue" else "medium",
            summary=f"{g.gate.replace('_', ' ')} is {g.status.replace('_', ' ')} (due {g.due_date})",
            details=details, fingerprint=fp, options=OPTIONS["gate"],
            created_at=existing.created_at if active else now, updated_at=now,
            snoozed_until=existing.snoozed_until if active else None, notified_at=existing.notified_at if active else None)
        due_for_nudge = item.notified_at is None or (g.status == "overdue" and item.notified_at.date() < today) \
            or item is not existing
        snoozed = item.snoozed_until is not None and today <= item.snoozed_until
        if due_for_nudge and not snoozed:
            ctx = {"name": rec.display_name, "start_date": wire(rec.start_date), "hire_id": rec.hire_id}
            try:
                text = self.advisor.draft_nudge(details, ctx)
            except Exception:
                text = f"{g.gate} for {rec.display_name} is {g.status} (due {g.due_date})."
            self.slack.post(SLACK_CHANNELS[g.owner], text, nudge_blocks(item, text))
            item.notified_at = now
        if item is not existing or due_for_nudge:
            self.store.save_exception(item)
        return item

    def clear(self, hire_id: str, key: str, reason: str) -> None:
        item = self.store.get_exception(exception_id(hire_id, key))
        if item and item.state in ("open", "awaiting_approval"):
            now = self.clock.now()
            item.state, item.updated_at = "resolved", now
            item.resolution = {"option": "auto_cleared", "by": "engine", "reason": reason, "at": now.isoformat()}
            self.store.save_exception(item)
            self.store.audit(hire_id, "engine", "exception.cleared", {"exception_id": item.id, "kind": item.kind,
                                                                      "reason": reason}, now)

    def clear_absent(self, hire_id: str, present_keys: set[str], kinds: set[str]) -> None:
        for item in self.store.list_exceptions(hire_id, states=("open", "awaiting_approval")):
            if item.kind in kinds and item.key not in present_keys:
                self.clear(hire_id, item.key, "condition no longer present")


GATE_CHECKS = {
    "workday_hire_complete": lambda rec, obs: bool(rec.source_refs.get("workday")),
    "hardware_form_submitted": lambda rec, obs: rec.hardware_form_submitted,
    "background_check_clear": lambda rec, obs: rec.bgc_status == "clear",
    "equipment_shipped": lambda rec, obs: (obs.get("equipment") or {}).get("status") in ("shipped", "delivered"),
    "okta_provisioned": lambda rec, obs: bool(obs.get("okta"))
    and obs["okta"].get("activationDate") == wire(rec.start_date),
}


class GateMonitor:
    def __init__(self, store: Store, targets: dict, clock: Clock, settings: Settings, desk: ExceptionDesk):
        self.store, self.targets, self.clock, self.settings, self.desk = store, targets, clock, settings, desk

    def recompute(self, rec: HireRecord) -> list[GateState]:
        if rec.lifecycle != "pre_start" or not rec.start_date:
            self.store.put_gates(rec.hire_id, [])
            for gate in GATE_RULES:
                self.desk.clear(rec.hire_id, f"gate:{gate}", f"hire is {rec.lifecycle}")
            return []
        observed = {name: self.targets[name].fetch(rec.source_refs[name])
                    for name in ("okta", "equipment") if rec.source_refs.get(name)}
        today, states = self.clock.today(), []
        for gate, (offset, owner) in GATE_RULES.items():
            due = add_business_days(rec.start_date, -offset)
            if GATE_CHECKS[gate](rec, observed):
                status = "done"
            elif today > due:
                status = "overdue"
            elif today >= add_business_days(due, -self.settings.gate_warn_business_days):
                status = "at_risk"
            else:
                status = "pending"
            states.append(GateState(hire_id=rec.hire_id, gate=gate, owner=owner, due_date=due, status=status))
        self.store.put_gates(rec.hire_id, states)
        for g in states:
            if g.status in ("at_risk", "overdue"):
                self.desk.raise_gate(rec, g)
            else:
                self.desk.clear(rec.hire_id, f"gate:{g.gate}",
                                "gate done" if g.status == "done" else "deadline moved with the start date")
        return states
