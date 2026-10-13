"""Identity resolution: which hire does an incoming event belong to?

Order of evidence, strongest first:
  1. a source ref we already know (system, external id)
  2. an explicit hire_id (the hardware-form link is pre-filled with it)
  3. the same personal email -> adopt this source record into the existing hire
  4. same normalized name + start date but no email match -> *possible duplicate*:
     create the record but hold it for a human (never auto-merge on a fuzzy match)
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime

from .models import EventEnvelope, HireRecord
from .normalize import Normalized
from .store import Store


class Unmatched(Exception):
    pass


def new_hire_id(source: str, external_id: str) -> str:
    return "h_" + hashlib.sha1(f"{source}:{external_id}".encode()).hexdigest()[:8]


def name_key(first: str | None, last: str | None, start: str | None) -> str:
    def fold(s: str | None) -> str:
        s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().casefold()
        return re.sub(r"[^a-z]", "", s)
    return f"{fold(first)}|{fold(last)}|{start or ''}"


def resolve_hire(store: Store, env: EventEnvelope, norm: Normalized, now: datetime
                 ) -> tuple[HireRecord, str, str | None]:
    """Return (record, how_matched, possible_duplicate_of)."""
    hid = store.find_by_ref(env.source, env.external_id)
    if hid:
        return store.get_hire(hid), "source_ref", None

    if norm.hire_id_hint:
        rec = store.get_hire(norm.hire_id_hint)
        if rec:
            rec.source_refs[env.source] = env.external_id
            return rec, "hire_id", None

    if norm.match_email:
        for rec in store.all_hires():
            if rec.lifecycle == "cancelled" or (rec.personal_email or "").lower() != norm.match_email:
                continue
            existing = rec.source_refs.get(env.source)
            if existing and existing != env.external_id:
                # Same person, second record in the same system (e.g. HR keyed a duplicate
                # worker by hand). Never silently re-point the ref: hold for a human.
                return _new(env, now), "possible_duplicate", rec.hire_id
            rec.source_refs[env.source] = env.external_id
            return rec, "personal_email", None

    if env.source == "it_form":
        raise Unmatched(f"hardware form {env.external_id} matches no hire")

    rec = _new(env, now)
    if norm.match_name_start:
        key = name_key(*norm.match_name_start)
        for other in store.all_hires():
            if other.lifecycle != "cancelled" and name_key(
                    other.legal_first_name, other.legal_last_name,
                    other.start_date.isoformat() if other.start_date else None) == key:
                return rec, "possible_duplicate", other.hire_id
    return rec, "new", None


def _new(env: EventEnvelope, now: datetime) -> HireRecord:
    return HireRecord(hire_id=new_hire_id(env.source, env.external_id), origin=env.source,
                      source_refs={env.source: env.external_id}, created_at=now, updated_at=now)
