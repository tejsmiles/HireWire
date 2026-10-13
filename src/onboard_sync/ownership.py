"""Field ownership and the merge step.

Three rules make change propagation safe:

1. **One owner per field at any moment.** Ashby owns HR fields until day 1, then Workday
   does; for off-cycle hires (created in Workday) Workday owns them from the start. Only the
   owner's edits change the canonical record. Anyone else's are recorded as *drift*; if that
   system is also a target, the planner's three-way diff turns the drift into a conflict.
2. **Change detection is per source.** A source "changed" a field only if its value differs
   from what *that same source* said last time. Full-snapshot webhooks repeat themselves
   constantly; a repeat is not an edit, so it can never clobber a newer decision made
   elsewhere (for example a human accepting Workday's start date).
3. **Ordering is per field.** If a source already told us something newer about a field,
   an older message about it is stale and dropped. Deltas and snapshots both work.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from .models import FieldProvenance, HireRecord

HR_FIELDS = frozenset({
    "legal_first_name", "legal_last_name", "preferred_first_name", "personal_email", "title",
    "department", "manager_email", "employment_type", "location", "start_date", "home_address",
})
FORM_FIELDS = frozenset({"shipping_address", "equipment_profile", "hardware_form_submitted"})


def hr_owner(rec: HireRecord) -> str:
    return "workday" if (rec.origin == "workday" or rec.lifecycle == "started") else "ashby"


def owner(field_name: str, rec: HireRecord) -> str:
    if field_name in FORM_FIELDS:
        return "it_form"
    if field_name == "bgc_status":
        return "ashby"          # the BGC vendor integration reports into Ashby (assumption A3)
    if field_name == "work_email":
        return "okta"
    if field_name in HR_FIELDS:
        return hr_owner(rec)
    return "engine"


def wire(value: Any) -> Any:
    """JSON-comparable form of any canonical value."""
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire(v) for v in value]
    return value


@dataclass
class FieldChange:
    field: str
    old: Any
    new: Any
    source: str

    def as_dict(self) -> dict:
        return {"field": self.field, "old": self.old, "new": self.new, "source": self.source}


@dataclass
class MergeResult:
    record: HireRecord
    changes: list[FieldChange] = field(default_factory=list)   # applied to canonical
    drift: list[FieldChange] = field(default_factory=list)     # non-owner said something different
    stale: list[str] = field(default_factory=list)             # older than what the source already told us
    fresh: int = 0                                             # fields the source actually changed


def merge(rec: HireRecord, *, source: str, occurred_at: datetime, event_id: str,
          fields: dict[str, Any]) -> MergeResult:
    rec = rec.model_copy(deep=True)
    snap = rec.source_snapshots.setdefault(source, {})
    result = MergeResult(record=rec)
    for name, raw in fields.items():
        value = wire(raw)
        prev = snap.get(name)
        if prev is not None:
            if prev["value"] == value:
                continue                       # the source repeated itself: not an edit
            if occurred_at <= datetime.fromisoformat(prev["at"]):
                # Older than (or the same instant as) what this source already told us. Ties go to
                # the first message seen, so a replayed old snapshot can never flap a field.
                result.stale.append(name)
                continue
        snap[name] = {"value": value, "at": occurred_at.isoformat()}
        result.fresh += 1
        current = wire(getattr(rec, name))
        if current == value:
            continue
        change = FieldChange(name, current, value, source)
        if owner(name, rec) == source:
            setattr(rec, name, value)
            rec.provenance[name] = FieldProvenance(source=source, at=occurred_at, event_id=event_id)
            result.changes.append(change)
        else:
            result.drift.append(change)
    return result
