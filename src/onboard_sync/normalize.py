"""Source payloads -> canonical fields (wire values).

Every source-specific field name lives in this file; nothing downstream knows what an
Ashby or Workday payload looks like. A key absent from the payload means "not provided";
a key present with null means "cleared".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from .config import LOCATION_CODES
from .models import Address, EventEnvelope, Location

CANCEL_TYPES = {"offer.rescinded", "offer.declined", "candidate.withdrew", "worker.rescinded"}
EMPLOYMENT = {"FullTime": "FTE", "PartTime": "FTE", "Contract": "CONTRACTOR", "Intern": "INTERN"}
WORKER_TYPE = {"Employee": "FTE", "Contingent_Worker": "CONTRACTOR", "Intern": "INTERN"}


class NormalizationError(ValueError):
    pass


@dataclass
class Normalized:
    fields: dict[str, Any]
    signal: Literal["upsert", "cancel"] = "upsert"
    hire_id_hint: str | None = None
    match_email: str | None = None
    match_name_start: tuple[str, str, str] | None = None


def _take(src: dict, key: str, dst: dict, name: str, conv: Callable[[Any], Any] = lambda v: v) -> None:
    if key in src:
        dst[name] = None if src[key] is None else conv(src[key])


def _clean(s: str) -> str:
    return s.strip()


def _email(s: str) -> str:
    return s.strip().lower()


def address(d: dict | None, style: str) -> dict | None:
    if not d:
        return None
    if style == "workday":
        d = {"line1": d.get("Address_Line_1"), "line2": d.get("Address_Line_2"), "city": d.get("City"),
             "region": d.get("Region"), "postalCode": d.get("Postal_Code"), "country": d.get("Country")}
    return Address(line1=d["line1"], line2=d.get("line2"), city=d["city"], region=d.get("region"),
                   postal_code=str(d.get("postalCode") or d.get("postal_code") or ""),
                   country=d.get("country") or d.get("countryCode")).model_dump(mode="json")


def location_from_code(code: str) -> dict:
    meta = LOCATION_CODES.get(code, {})
    return Location(label=meta.get("label", code), site_code=code, country=meta.get("country"),
                    region=meta.get("region")).model_dump(mode="json")


def normalize_ashby(env: EventEnvelope) -> Normalized:
    data = env.payload.get("data", env.payload)
    cand, offer, bgc = data.get("candidate") or {}, data.get("offer") or {}, data.get("backgroundCheck") or {}
    f: dict[str, Any] = {}
    _take(cand, "firstName", f, "legal_first_name", _clean)
    _take(cand, "lastName", f, "legal_last_name", _clean)
    _take(cand, "preferredFirstName", f, "preferred_first_name", _clean)
    if "primaryEmailAddress" in cand:
        f["personal_email"] = _email((cand["primaryEmailAddress"] or {}).get("value", ""))
    _take(cand, "homeAddress", f, "home_address", lambda a: address(a, "ashby"))
    _take(offer, "startDate", f, "start_date")
    _take(offer, "jobTitle", f, "title", _clean)
    _take(offer, "department", f, "department", _clean)
    _take(offer, "hiringManagerEmail", f, "manager_email", _email)
    _take(offer, "employmentType", f, "employment_type", lambda v: EMPLOYMENT.get(v, "FTE"))
    _take(offer, "location", f, "location", lambda l: Location(
        label=l["name"].strip(), country=l.get("countryCode"), region=l.get("region")).model_dump(mode="json"))
    _take(bgc, "status", f, "bgc_status")
    return Normalized(
        fields=f,
        signal="cancel" if env.type in CANCEL_TYPES else "upsert",
        match_email=f.get("personal_email"),
    )


def normalize_workday(env: EventEnvelope) -> Normalized:
    w = env.payload.get("worker", env.payload)
    f: dict[str, Any] = {}
    _take(w, "Legal_First_Name", f, "legal_first_name", _clean)
    _take(w, "Legal_Last_Name", f, "legal_last_name", _clean)
    _take(w, "Preferred_First_Name", f, "preferred_first_name", _clean)
    _take(w, "Personal_Email", f, "personal_email", _email)
    _take(w, "Hire_Date", f, "start_date")
    _take(w, "Job_Title", f, "title", _clean)
    _take(w, "Supervisory_Org", f, "department", _clean)
    _take(w, "Manager_Email", f, "manager_email", _email)
    _take(w, "Worker_Type", f, "employment_type", lambda v: WORKER_TYPE.get(v, "FTE"))
    _take(w, "Location_Code", f, "location", location_from_code)
    _take(w, "Home_Address", f, "home_address", lambda a: address(a, "workday"))
    cancelled = env.type in CANCEL_TYPES or w.get("Status") == "Rescinded"
    name_start = None
    if w.get("Legal_First_Name") and w.get("Legal_Last_Name") and w.get("Hire_Date"):
        name_start = (w["Legal_First_Name"], w["Legal_Last_Name"], w["Hire_Date"])
    return Normalized(fields=f, signal="cancel" if cancelled else "upsert",
                      match_email=f.get("personal_email"), match_name_start=name_start)


def normalize_it_form(env: EventEnvelope) -> Normalized:
    p = env.payload
    f: dict[str, Any] = {"hardware_form_submitted": True}
    _take(p, "equipment_choice", f, "equipment_profile")
    f["shipping_address"] = address(p.get("shipping_address"), "form") if p.get("ship_to_different_address") else None
    return Normalized(fields=f, hire_id_hint=p.get("hire_id"),
                      match_email=_email(p["personal_email"]) if p.get("personal_email") else None)


NORMALIZERS = {"ashby": normalize_ashby, "workday": normalize_workday, "it_form": normalize_it_form}


def normalize(env: EventEnvelope) -> Normalized:
    try:
        return NORMALIZERS[env.source](env)
    except (KeyError, TypeError, ValueError) as e:
        raise NormalizationError(f"{env.source}/{env.type}: {e}") from e
