"""Canonical data model.

HireRecord is the single desired state for one hire. Every target system is a
projection of it; every source system is an input to it.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Source = Literal["ashby", "workday", "it_form"]
Lifecycle = Literal["pre_start", "started", "cancelled"]
SyncState = Literal["new", "held", "pending", "in_sync", "attention"]
OpKind = Literal["create", "update", "noop", "skip", "conflict", "deprovision"]


class Address(BaseModel):
    model_config = ConfigDict(frozen=True)
    line1: str
    line2: str | None = None
    city: str
    region: str | None = None
    postal_code: str
    country: str


class Location(BaseModel):
    model_config = ConfigDict(frozen=True)
    label: str
    site_code: str | None = None
    country: str | None = None
    region: str | None = None


class FieldProvenance(BaseModel):
    source: str               # ashby | workday | it_form | okta | claude | human:<id>
    at: datetime
    event_id: str | None = None


# Fields that sources may write (merge operates on exactly these).
MERGEABLE_FIELDS = (
    "legal_first_name", "legal_last_name", "preferred_first_name", "personal_email",
    "title", "department", "manager_email", "employment_type", "location", "start_date",
    "home_address", "shipping_address", "equipment_profile", "hardware_form_submitted",
    "bgc_status",
)


class HireRecord(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    hire_id: str
    version: int = 0
    origin: Source
    lifecycle: Lifecycle = "pre_start"
    sync_state: SyncState = "new"
    source_refs: dict[str, str] = Field(default_factory=dict)

    legal_first_name: str | None = None
    legal_last_name: str | None = None
    preferred_first_name: str | None = None
    personal_email: str | None = None
    work_email: str | None = None
    work_email_aliases: list[str] = Field(default_factory=list)

    title: str | None = None
    department: str | None = None
    manager_email: str | None = None
    employment_type: Literal["FTE", "CONTRACTOR", "INTERN"] | None = None
    location: Location | None = None
    start_date: date | None = None

    home_address: Address | None = None
    shipping_address: Address | None = None       # None => ship to home address
    equipment_profile: str | None = None
    hardware_form_submitted: bool = False
    bgc_status: Literal["not_started", "in_progress", "clear", "consider"] | None = None

    provenance: dict[str, FieldProvenance] = Field(default_factory=dict)
    # source -> field -> {"value": <wire value>, "at": iso}: what each source last said.
    source_snapshots: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def first_name(self) -> str:
        return self.preferred_first_name or self.legal_first_name or ""

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.legal_last_name or ''}".strip()


class EventEnvelope(BaseModel):
    """What n8n posts to /events. event_id is the ingest idempotency key."""
    event_id: str
    source: Source
    type: str
    external_id: str
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class PlanOp(BaseModel):
    target: str
    kind: OpKind
    desired: dict[str, Any] = Field(default_factory=dict)
    observed: dict[str, Any] | None = None
    base: dict[str, Any] | None = None
    fields: list[str] = Field(default_factory=list)
    reason: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    requires_approval: bool = False


class Advisory(BaseModel):
    """A condition a target adapter wants a human to look at (not a write)."""
    kind: str
    key: str
    summary: str
    owner: str
    severity: Literal["low", "medium", "high"] = "medium"
    details: dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    hire_id: str
    version: int
    ops: list[PlanOp]
    advisories: list[Advisory] = Field(default_factory=list)

    def summary(self) -> list[str]:
        out = []
        for op in self.ops:
            s = f"{op.target}:{op.kind.upper()}"
            if op.fields:
                s += f"({','.join(op.fields)})"
            if op.reason and op.kind in ("skip", "deprovision"):
                s += f" [{op.reason}]"
            out.append(s)
        return out


class OpResult(BaseModel):
    target: str
    kind: OpKind
    status: Literal["ok", "adopted", "noop", "skipped", "blocked", "held", "conflict", "failed", "parked"]
    external_id: str | None = None
    attempts: int = 0
    error: str | None = None
    code: str | None = None


class GateState(BaseModel):
    hire_id: str
    gate: str
    owner: str
    due_date: date
    status: Literal["done", "pending", "at_risk", "overdue"]


class ExceptionItem(BaseModel):
    id: str
    hire_id: str
    kind: str
    key: str
    severity: Literal["low", "medium", "high"] = "medium"
    state: Literal["open", "awaiting_approval", "resolved", "dismissed"] = "open"
    owner: str = "People Ops"
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str = ""
    options: dict[str, str] = Field(default_factory=dict)
    recommendation: dict[str, Any] | None = None
    resolution: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    notified_at: datetime | None = None
    snoozed_until: date | None = None
