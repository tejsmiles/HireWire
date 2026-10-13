"""Target adapters: the canonical record expressed in each system's own shape.

Every adapter answers the same questions for the planner and the executor:
  skip_reason(rec)            should this target be touched yet?
  project(rec, observed)      desired state, in the target's field names (managed fields only)
  fetch(ext_id)               observed state
  find_existing(rec)          natural-key lookup, run before every create and after a 409
  create / update / deprovision
  advisories(rec, observed)   conditions a human must handle that are not writes
  to_canonical(field, value)  conflict-detecting targets only: map a field back to canonical

Only Workday detects conflicts. It is the one target people edit directly as a system of
record. Okta, Lumos, ExpoIT and the tracker are projections of the canonical record, so
out-of-band edits to the fields the engine manages get overwritten, and the audit log
records it.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from .config import (DEFAULT_BUNDLE, DEFAULT_EQUIPMENT_PROFILE, EQUIPMENT_PROFILES, LUMOS_BUNDLES,
                     Settings)
from .errors import AlreadyExists, PermanentError
from .mocks import FakeWorld
from .models import Address, Advisory, HireRecord
from .normalize import WORKER_TYPE, address, location_from_code
from .ownership import owner, wire
from .timeutil import add_business_days, cohort_label


class Target:
    name = ""
    depends_on: tuple[str, ...] = ()
    detect_conflicts = False

    def __init__(self, world: FakeWorld, settings: Settings):
        self.world, self.settings = world, settings

    def skip_reason(self, rec: HireRecord) -> str | None:
        return None

    def project(self, rec: HireRecord, observed: dict | None) -> dict[str, Any]:
        raise NotImplementedError

    def fetch(self, ext_id: str) -> dict | None:
        raise NotImplementedError

    def find_existing(self, rec: HireRecord) -> str | None:
        return None

    def create(self, rec: HireRecord, desired: dict, key: str) -> str:
        raise NotImplementedError

    def update(self, ext_id: str, changes: dict, key: str) -> None:
        raise NotImplementedError

    def deprovision(self, ext_id: str, key: str) -> None:
        raise NotImplementedError

    def advisories(self, rec: HireRecord, observed: dict | None) -> list[Advisory]:
        return []

    def after_create(self, rec: HireRecord, ext_id: str) -> dict[str, Any]:
        """Canonical facts learned from the target at creation (e.g. the Okta login)."""
        return {}

    def to_canonical(self, field: str, value: Any) -> tuple[str, Any]:
        raise KeyError(field)


# --- Workday ------------------------------------------------------------------------------
WD_FIELDS = {
    "Legal_First_Name": "legal_first_name", "Legal_Last_Name": "legal_last_name",
    "Preferred_First_Name": "preferred_first_name", "Personal_Email": "personal_email",
    "Hire_Date": "start_date", "Job_Title": "title", "Supervisory_Org": "department",
    "Manager_Email": "manager_email", "Location_Code": "location", "Home_Address": "home_address",
    "Worker_Type": "employment_type",
}
WORKER_TYPE_OUT = {"FTE": "Employee", "INTERN": "Employee", "CONTRACTOR": "Contingent_Worker"}


def _wd_address(a: Address | None) -> dict | None:
    return None if a is None else {"Address_Line_1": a.line1, "Address_Line_2": a.line2, "City": a.city,
                                   "Region": a.region, "Postal_Code": a.postal_code, "Country": a.country}


class WorkdayTarget(Target):
    name = "workday"
    detect_conflicts = True

    def project(self, rec, observed):
        full = {
            "Legal_First_Name": rec.legal_first_name, "Legal_Last_Name": rec.legal_last_name,
            "Preferred_First_Name": rec.preferred_first_name, "Personal_Email": rec.personal_email,
            "Hire_Date": wire(rec.start_date), "Job_Title": rec.title, "Supervisory_Org": rec.department,
            "Manager_Email": rec.manager_email,
            "Location_Code": rec.location.site_code if rec.location else None,
            "Home_Address": _wd_address(rec.home_address),
            "Worker_Type": WORKER_TYPE_OUT.get(rec.employment_type or "FTE"),
        }
        # Never push a field to the system that owns it (post-start, or off-cycle hires).
        return {k: v for k, v in full.items() if owner(WD_FIELDS[k], rec) != "workday"}

    def fetch(self, ext_id):
        return self.world.workday.get(ext_id)

    def find_existing(self, rec):
        return self.world.workday.find(candidate_id=rec.source_refs.get("ashby"), personal_email=rec.personal_email)

    def create(self, rec, desired, key):
        return self.world.workday.hire({**desired, "External_Candidate_ID": rec.source_refs.get("ashby"),
                                        "Hire_ID": rec.hire_id}, idempotency_key=key)

    def update(self, ext_id, changes, key):
        self.world.workday.change(ext_id, changes, idempotency_key=key)

    def deprovision(self, ext_id, key):
        self.world.workday.rescind(ext_id)

    def to_canonical(self, field, value):
        cf = WD_FIELDS[field]
        if value is None:
            return cf, None
        if cf == "location":
            return cf, location_from_code(value)
        if cf == "home_address":
            return cf, address(value, "workday")
        if cf == "employment_type":
            return cf, WORKER_TYPE.get(value, "FTE")
        return cf, value


# --- Okta ---------------------------------------------------------------------------------
def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def derive_login(rec: HireRecord, domain: str) -> str:
    return f"{slug(rec.first_name)}.{slug(rec.legal_last_name or '')}@{domain}"


def base_local(login: str) -> str:
    """'john.okafor2@x' -> 'john.okafor' (collision suffixes don't count as a name change)."""
    return re.sub(r"\d+$", "", login.split("@")[0])


class OktaTarget(Target):
    name = "okta"
    depends_on = ("workday",)   # an identity must trace back to an HR record

    def project(self, rec, observed):
        # The login is immutable once created: desired == observed, so it never diffs.
        login = observed["login"] if observed else (rec.work_email or derive_login(rec, self.settings.email_domain))
        return {
            "login": login, "email": login, "firstName": rec.first_name, "lastName": rec.legal_last_name,
            "displayName": rec.display_name, "title": rec.title, "department": rec.department,
            "managerEmail": rec.manager_email, "countryCode": rec.location.country if rec.location else None,
            "employeeNumber": rec.source_refs.get("workday"), "hireId": rec.hire_id,
            "activationDate": wire(rec.start_date),
        }

    def fetch(self, ext_id):
        u = self.world.okta.get(ext_id)
        return None if u is None else {**u["profile"], "status": u["status"], "aliases": u["aliases"]}

    def find_existing(self, rec):
        u = self.world.okta.find_by_hire_id(rec.hire_id)
        return u["id"] if u else None

    def free_login(self, wanted: str, hire_id: str) -> str:
        local, domain = wanted.split("@")
        for n in range(1, 10):
            login = wanted if n == 1 else f"{local}{n}@{domain}"
            holder = self.world.okta.find_by_login(login)
            if holder is None or holder["profile"].get("hireId") == hire_id:
                return login
        raise PermanentError(409, "LOGIN_EXHAUSTED", f"no free login for {wanted}")

    def create(self, rec, desired, key):
        local, domain = desired["login"].split("@")
        for n in range(1, 10):
            login = desired["login"] if n == 1 else f"{local}{n}@{domain}"
            try:
                return self.world.okta.create_user({**desired, "login": login, "email": login})
            except AlreadyExists:
                holder = self.world.okta.find_by_login(login)
                if holder and holder["profile"].get("hireId") == rec.hire_id:
                    return holder["id"]        # our own earlier attempt whose response was lost
                # a namesake or former employee holds it (even deprovisioned): next suffix
        raise PermanentError(409, "LOGIN_EXHAUSTED", f"no free login for {desired['login']}")

    def update(self, ext_id, changes, key):
        self.world.okta.update_profile(ext_id, changes)

    def deprovision(self, ext_id, key):
        self.world.okta.deactivate(ext_id)

    def rename_login(self, ext_id: str, new_login: str) -> None:
        self.world.okta.rename_login(ext_id, new_login)

    def add_alias(self, ext_id: str, alias: str) -> None:
        self.world.okta.add_alias(ext_id, alias)

    def after_create(self, rec, ext_id):
        return {"work_email": self.world.okta.get(ext_id)["profile"]["login"]}

    def advisories(self, rec, observed):
        if not observed or rec.lifecycle == "cancelled":
            return []
        expected = derive_login(rec, self.settings.email_domain)
        known = {base_local(observed["login"])} | {base_local(a) for a in observed.get("aliases", [])} \
            | {base_local(a) for a in rec.work_email_aliases}
        if base_local(expected) in known:
            return []
        return [Advisory(kind="email_mismatch", key="email_mismatch:okta", owner="IT", severity="medium",
                         summary=f"Name changed after Okta was provisioned: login is {observed['login']}, "
                                 f"the corrected name implies {expected}",
                         details={"current_login": observed["login"], "expected_login": expected})]


# --- Lumos --------------------------------------------------------------------------------
class LumosTarget(Target):
    name = "lumos"
    depends_on = ("okta",)

    def project(self, rec, observed):
        return {"hireRef": rec.hire_id, "oktaUserId": rec.source_refs.get("okta"),
                "bundle": LUMOS_BUNDLES.get(rec.department or "", DEFAULT_BUNDLE), "neededBy": wire(rec.start_date)}

    def fetch(self, ext_id):
        return self.world.lumos.get(ext_id)

    def find_existing(self, rec):
        return self.world.lumos.find(rec.hire_id)

    def create(self, rec, desired, key):
        return self.world.lumos.create_request(desired, idempotency_key=key)

    def update(self, ext_id, changes, key):
        self.world.lumos.update_request(ext_id, changes)

    def deprovision(self, ext_id, key):
        self.world.lumos.cancel_request(ext_id)


# --- Equipment (IT hardware form -> ExpoIT order) -----------------------------------------
class EquipmentTarget(Target):
    name = "equipment"

    def skip_reason(self, rec):
        return None if rec.hardware_form_submitted else "awaiting IT hardware form"

    def _full(self, rec: HireRecord) -> dict:
        return {
            "hireRef": rec.hire_id, "recipient": rec.display_name,
            "shipTo": wire(rec.shipping_address or rec.home_address),
            "deliverBy": wire(add_business_days(rec.start_date, -2)) if rec.start_date else None,
            "profile": rec.equipment_profile or EQUIPMENT_PROFILES.get(rec.department or "", DEFAULT_EQUIPMENT_PROFILE),
        }

    def project(self, rec, observed):
        if observed and observed.get("status") != "ordered":
            # A shipped order can't be edited. Don't attempt a doomed PATCH; advisories() surfaces it.
            return {"hireRef": rec.hire_id}
        return self._full(rec)

    def fetch(self, ext_id):
        return self.world.expoit.get(ext_id)

    def find_existing(self, rec):
        return self.world.expoit.find(rec.hire_id)

    def create(self, rec, desired, key):
        return self.world.expoit.create_order(desired, idempotency_key=key)

    def update(self, ext_id, changes, key):
        self.world.expoit.patch_order(ext_id, changes, idempotency_key=key)

    def deprovision(self, ext_id, key):
        self.world.expoit.cancel_order(ext_id)

    def advisories(self, rec, observed):
        if not observed or observed.get("status") in ("ordered", "cancelled") or rec.lifecycle == "cancelled":
            return []
        full = self._full(rec)
        stale = [k for k in ("shipTo", "recipient") if observed.get(k) != full[k]]
        if not stale:
            return []
        return [Advisory(kind="equipment_after_ship", key="equipment_after_ship:equipment", owner="IT", severity="high",
                         summary=f"Laptop already {observed['status']} with an outdated {' and '.join(stale)}",
                         details={"fields": stale, "status": observed["status"], "tracking": observed.get("tracking"),
                                  "shipped_to": observed.get("shipTo"), "should_ship_to": full["shipTo"],
                                  "desired": {k: full[k] for k in stale}})]


# --- Cohort tracker (Google Sheet) --------------------------------------------------------
class TrackerTarget(Target):
    name = "tracker"

    def project(self, rec, observed):
        loc = (rec.location.site_code or rec.location.label) if rec.location else None
        return {
            "Hire ID": rec.hire_id, "Name": rec.display_name, "Start Date": wire(rec.start_date),
            "Cohort": cohort_label(rec.start_date), "Team": rec.department, "Title": rec.title,
            "Manager": rec.manager_email, "Location": loc, "Work Email": rec.work_email or "",
            "Workday ID": rec.source_refs.get("workday", ""),
            "Status": {"pre_start": "Pre-start", "started": "Started", "cancelled": "Cancelled"}[rec.lifecycle],
        }

    def fetch(self, ext_id):
        return self.world.sheets.get_row(ext_id)

    def find_existing(self, rec):
        return self.world.sheets.find_row(rec.hire_id)

    def create(self, rec, desired, key):
        return self.world.sheets.append_row(desired)

    def update(self, ext_id, changes, key):
        self.world.sheets.update_row(ext_id, changes)

    def deprovision(self, ext_id, key):
        self.world.sheets.update_row(ext_id, {"Status": "Cancelled"})


def build_targets(world: FakeWorld, settings: Settings) -> dict[str, Target]:
    return {t.name: t for t in (WorkdayTarget(world, settings), OktaTarget(world, settings),
                                LumosTarget(world, settings), EquipmentTarget(world, settings),
                                TrackerTarget(world, settings))}
