"""Engine semantics: ordering, ownership, idempotency, failure handling, identity resolution."""
import json
from datetime import date, datetime, timedelta, timezone

from conftest import calls, okta_users_for
from onboard_sync.models import EventEnvelope, HireRecord
from onboard_sync.ownership import merge
from onboard_sync.reconcile import idempotency_key
from onboard_sync.scenarios import SAMPLES, T0
from onboard_sync.timeutil import add_business_days, business_days_until

T = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def _rec(**kw) -> HireRecord:
    return HireRecord(hire_id="h_x", origin="ashby", source_refs={"ashby": "c1"}, **kw)


# --- merge / ownership --------------------------------------------------------------------
def test_field_level_ordering_and_ties():
    rec = merge(_rec(), source="ashby", occurred_at=T, event_id="e1", fields={"title": "A", "department": "X"}).record
    newer = merge(rec, source="ashby", occurred_at=T + timedelta(hours=2), event_id="e3", fields={"title": "C"}).record
    late = merge(newer, source="ashby", occurred_at=T + timedelta(hours=1), event_id="e2",
                 fields={"title": "B", "department": "Y"})
    assert late.record.title == "C"                       # older message about title: stale
    assert late.record.department == "Y"                  # but it is the newest word on department
    assert late.stale == ["title"]
    tie = merge(late.record, source="ashby", occurred_at=T + timedelta(hours=2), event_id="e4", fields={"title": "Z"})
    assert tie.record.title == "C" and tie.stale == ["title"]   # same instant: first seen wins


def test_repeats_are_not_edits_so_snapshots_cannot_clobber_a_human_decision():
    rec = merge(_rec(), source="ashby", occurred_at=T, event_id="e1", fields={"start_date": "2026-11-02"}).record
    rec.start_date = date(2026, 11, 9)                    # e.g. a human accepted Workday's date
    again = merge(rec, source="ashby", occurred_at=T + timedelta(days=1), event_id="e2",
                  fields={"start_date": "2026-11-02", "title": "New"})
    assert again.record.start_date == date(2026, 11, 9)   # Ashby repeated itself: not an edit
    assert again.record.title == "New"


def test_non_owner_edits_are_drift_not_changes():
    rec = _rec(start_date=date(2026, 11, 2))
    m = merge(rec, source="workday", occurred_at=T, event_id="w1", fields={"start_date": "2026-11-09"})
    assert m.record.start_date == date(2026, 11, 2) and [d.field for d in m.drift] == ["start_date"]


def test_ownership_flips_to_workday_on_day_one(h):
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    h.at("2026-11-02")
    h.sweep()
    assert h.svc.store.get_hire(hid).lifecycle == "started"
    h.ashby("priya_raman", changes={"offer.jobTitle": "Coach Success Lead"})
    assert h.svc.store.get_hire(hid).title == "Senior Coach Success Manager"     # Ashby no longer owns it
    wid = h.svc.store.get_hire(hid).source_refs["workday"]
    h.workday_edit(wid, {"Job_Title": "Coach Success Lead"})
    h.workday_poll()
    rec = h.svc.store.get_hire(hid)
    assert rec.title == "Coach Success Lead"                                      # Workday does
    assert h.svc.targets["okta"].fetch(rec.source_refs["okta"])["title"] == "Coach Success Lead"


# --- idempotency ---------------------------------------------------------------------------
def test_idempotency_keys():
    assert idempotency_key("h", "okta", "create", 1) == idempotency_key("h", "okta", "create", 9)
    assert idempotency_key("h", "okta", "update", 1) != idempotency_key("h", "okta", "update", 2)


def test_crash_between_ingest_and_reconcile_is_healed_by_the_sweep(h, monkeypatch):
    real = h.svc.engine._reconcile
    monkeypatch.setattr(h.svc.engine, "_reconcile", lambda *a, **k: {"status": "crashed"})
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    assert h.world.writes() == 0                                                  # desired state saved, nothing pushed
    monkeypatch.setattr(h.svc.engine, "_reconcile", real)
    h.sweep()
    assert h.svc.store.get_hire(hid).sync_state == "in_sync" and calls(h, "workday.hire") == 1


def test_adopts_a_worker_hr_created_by_hand_instead_of_duplicating(h):
    worker = json.loads((SAMPLES / "workday" / "dana_whitfield.json").read_text())
    payload = json.loads((SAMPLES / "ashby" / "priya_raman.json").read_text())
    worker.update(Personal_Email=payload["data"]["candidate"]["primaryEmailAddress"]["value"],
                  Legal_First_Name="Priya", Legal_Last_Name="Raman")
    h.world.workday.human_create(worker)                                          # keyed by hand, not polled yet
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    assert calls(h, "workday.hire") == 0                                          # lookup-before-create adopted it
    w = h.svc.targets["workday"].fetch(h.svc.store.get_hire(hid).source_refs["workday"])
    assert w["Hire_Date"] == "2026-11-02" and w["Job_Title"] == "Senior Coach Success Manager"   # then converged


# --- failure handling ------------------------------------------------------------------------
def test_permanent_error_fails_fast_and_raises_exception(h):
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    h.chaos("okta", "update_profile", times=1, status=400)
    h.ashby("priya_raman", changes={"offer.jobTitle": "Lead"})
    items = {e.kind: e for e in h.svc.store.list_exceptions(hid, states=("awaiting_approval",))}
    assert "apply_failed" in items and items["apply_failed"].details["code"] == "INJECTED"
    assert h.svc.store.retries(hid) == []                                         # 4xx is never parked/retried
    h.click("apply_failed", "retry_now", user="U07ITOPS")
    assert h.svc.targets["okta"].fetch(h.svc.store.get_hire(hid).source_refs["okta"])["title"] == "Lead"


def test_parked_target_escalates_after_repeated_sweeps(h):
    h.chaos("okta", "create_user", times=40, status=503)
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    for _ in range(2):
        h.advance(hours=1)
        h.sweep()
    open_kinds = [e.kind for e in h.svc.store.list_exceptions(hid, states=("awaiting_approval",))]
    assert "apply_failed" in open_kinds and h.svc.store.retries(hid)[0]["attempts"] == 3
    assert h.svc.store.get_hire(hid).source_refs.get("tracker")                  # the tracker still shows the hire


# --- identity resolution ---------------------------------------------------------------------
def test_possible_duplicate_is_held_then_linked(h):
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    wid = h.svc.store.get_hire(hid).source_refs["workday"]
    h.workday_poll()                                                              # echo of our own create
    worker = dict(h.world.workday.get(wid))
    worker.pop("Worker_ID")
    worker["Personal_Email"] = "different@example.com"                            # same name + start, new email
    dup_wid = h.world.workday.human_create(worker)
    h.svc.clock.advance(minutes=5)
    res = h.workday_poll()
    assert res[0]["status"] == "held"
    dup = res[0]["hire_id"]
    assert okta_users_for(h, dup) == []                                           # nothing provisioned for it
    h.click("possible_duplicate", "link", hire_id=dup)
    assert h.svc.store.get_hire(dup).lifecycle == "cancelled"
    assert h.svc.store.find_by_ref("workday", dup_wid) == dup                     # original already has a worker


def test_unmatched_hardware_form_is_rejected(svc):
    r = svc.ingest(EventEnvelope(event_id="f1", source="it_form", type="hardware_form.submitted",
                                 external_id="form_1", occurred_at=T0, payload={"personal_email": "x@y.com"}))
    assert r["status"] == "rejected"


# --- business days -----------------------------------------------------------------------------
def test_business_days_respect_weekends_and_holidays():
    assert add_business_days(date(2026, 11, 25), 1) == date(2026, 11, 30)        # Thanksgiving + Friday off
    assert add_business_days(date(2026, 11, 2), -7) == date(2026, 10, 22)
    assert business_days_until(date(2026, 10, 13), date(2026, 11, 2)) == 13
