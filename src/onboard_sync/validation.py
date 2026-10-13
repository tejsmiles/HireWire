"""Admission gate: nothing reaches a target system until the record passes.

Deterministic rules run first (cheap, explainable, testable). Claude is used for one
long-tail job, mapping a free-text location to the Workday location master, and its
answer is applied only if a deterministic guard agrees (country and home state must be
consistent). If Claude errors or is unsure, the gate fails closed and blocks.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from .config import LOCATION_ALIASES, LOCATION_CODES, Settings
from .models import FieldProvenance, HireRecord, Location
from .ownership import wire
from .timeutil import Clock, business_days_until, is_business_day

REQUIRED = ("legal_first_name", "legal_last_name", "personal_email", "start_date", "title",
            "department", "manager_email", "location")
PLACEHOLDERS = {"test", "tbd", "tba", "n/a", "na", "unknown", "candidate", "xxx", "asdf", "none"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


@dataclass
class Finding:
    rule: str
    severity: str          # block | warn | info
    message: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "severity": self.severity, "message": self.message}


@dataclass
class Verdict:
    record: HireRecord
    findings: list[Finding] = field(default_factory=list)
    normalizations: list[dict] = field(default_factory=list)

    @property
    def blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block"]

    @property
    def warns(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]


def name_findings(field_name: str, value: str) -> list[Finding]:
    """Deliberately permissive: mononyms, short names, apostrophes, hyphens and
    non-Latin scripts are all valid. Only block what is clearly not a name."""
    out = []
    if any(c.isdigit() for c in value):
        out.append(Finding("name_format", "block", f"{field_name} '{value}' contains digits"))
    if value.strip().casefold() in PLACEHOLDERS:
        out.append(Finding("name_format", "block", f"{field_name} '{value}' is a placeholder"))
    if any(unicodedata.category(c) == "Cc" for c in value):
        out.append(Finding("name_format", "block", f"{field_name} contains control characters"))
    if "  " in value:
        out.append(Finding("name_format", "warn", f"{field_name} '{value}' has repeated spaces"))
    if len(value) > 3 and value.isupper():
        out.append(Finding("name_format", "warn", f"{field_name} '{value}' is ALL CAPS"))
    return out


def location_guard(code: str, rec: HireRecord) -> str | None:
    """Deterministic check any proposed site code must pass. Returns a problem or None."""
    meta = LOCATION_CODES.get(code)
    if not meta:
        return f"{code} is not in the Workday location master"
    if rec.location and rec.location.country and rec.location.country != meta["country"]:
        return f"{code} is in {meta['country']} but the offer location is in {rec.location.country}"
    home = rec.home_address
    if meta["remote"] and meta["region"] and home and home.region and home.region != meta["region"]:
        return (f"{code} is a remote {meta['region']} code but the home address is in "
                f"{home.region} (payroll/tax state)")
    return None


class Validator:
    def __init__(self, manager_exists: Callable[[str], bool], advisor, clock: Clock, settings: Settings):
        self.manager_exists = manager_exists
        self.advisor = advisor
        self.clock = clock
        self.settings = settings

    def validate(self, rec: HireRecord) -> Verdict:
        rec = rec.model_copy(deep=True)
        v = Verdict(record=rec)
        if rec.lifecycle == "cancelled":
            return v
        missing = [f for f in REQUIRED if getattr(rec, f) in (None, "")]
        if missing:
            v.findings.append(Finding("required_fields", "block", "Missing " + ", ".join(missing)))
        for f in ("legal_first_name", "legal_last_name", "preferred_first_name"):
            if getattr(rec, f):
                v.findings += name_findings(f, getattr(rec, f))
        if rec.personal_email and not EMAIL_RE.match(rec.personal_email):
            v.findings.append(Finding("email_format", "block", f"personal email '{rec.personal_email}' is invalid"))
        v.findings += self._dates(rec)
        if rec.location:
            self._location(v)
        v.findings += self._consistency(v.record)
        if rec.manager_email and not self.manager_exists(rec.manager_email):
            v.findings.append(Finding("manager_unknown", "block",
                                      f"manager {rec.manager_email} is not an active worker in Workday"))
        return v

    def _dates(self, rec: HireRecord) -> list[Finding]:
        if not rec.start_date or rec.lifecycle != "pre_start":
            return []
        today, out = self.clock.today(), []
        if rec.start_date < today:
            return [Finding("start_date", "block", f"start date {rec.start_date} is in the past")]
        if not is_business_day(rec.start_date):
            out.append(Finding("start_date", "block", f"start date {rec.start_date} is a weekend or company holiday"))
        elif rec.start_date.weekday() != self.settings.cohort_weekday:
            out.append(Finding("start_date", "warn", f"start date {rec.start_date} is not a cohort start day"))
        lead = business_days_until(today, rec.start_date)
        if lead < self.settings.min_lead_business_days:
            out.append(Finding("lead_time", "warn",
                               f"only {lead} business days until start: hardware and access gates are at risk"))
        return out

    def _location(self, v: Verdict) -> None:
        rec, loc = v.record, v.record.location
        code, via, rationale = None, None, None
        if loc.site_code in LOCATION_CODES:
            code = loc.site_code
        elif LOCATION_ALIASES.get(loc.label.casefold().strip()):
            code, via = LOCATION_ALIASES[loc.label.casefold().strip()], "alias table"
        else:
            from .claude import redact_for_llm
            try:
                p = self.advisor.judge_location(redact_for_llm(rec), LOCATION_CODES)
            except Exception as e:                      # the judge can never crash the pipeline
                v.findings.append(Finding("location_unresolved", "block",
                                          f"'{loc.label}' is not a Workday location and auto-mapping failed ({e})"))
                return
            if not p.site_code or p.confidence < self.settings.judge_min_confidence:
                v.findings.append(Finding("location_unresolved", "block",
                                          f"'{loc.label}' is not a Workday location; Claude was not confident "
                                          f"({p.confidence:.2f}): {p.rationale}"))
                return
            code, via, rationale = p.site_code, f"claude:{p.source} ({p.confidence:.2f})", p.rationale
        problem = location_guard(code, rec)
        if problem:
            hint = f" (suggested by {via})" if via else ""
            v.findings.append(Finding("location_inconsistent", "block", f"'{loc.label}' -> {code}{hint}: {problem}"))
            return
        if code != loc.site_code:
            meta = LOCATION_CODES[code]
            new = Location(label=meta["label"], site_code=code, country=meta["country"], region=meta["region"])
            v.normalizations.append({"field": "location", "from": wire(loc), "to": wire(new),
                                     "via": via, "rationale": rationale})
            rec.location = new
            rec.provenance["location"] = FieldProvenance(
                source="claude" if via and via.startswith("claude") else "engine", at=self.clock.now())
            v.findings.append(Finding("location_normalized", "info", f"'{loc.label}' -> {code} via {via}"))

    def _consistency(self, rec: HireRecord) -> list[Finding]:
        ship_to = rec.shipping_address or rec.home_address
        if ship_to and rec.location and rec.location.country and ship_to.country != rec.location.country:
            return [Finding("ship_to_country", "block",
                            f"equipment would ship to {ship_to.country} but the work location is in "
                            f"{rec.location.country}")]
        return []
