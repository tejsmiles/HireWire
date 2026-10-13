"""The demo scenarios, asserted. Each test replays the same story the Loom shows."""
from conftest import calls, okta_users_for, scenario, target


def test_s01_happy_path_and_replay_storm_is_a_noop():
    h, r = scenario("S01")
    hid = r["hire_id"]
    rec = h.svc.store.get_hire(hid)
    assert rec.sync_state == "in_sync"
    assert {"ashby", "workday", "okta", "lumos", "equipment", "tracker"} <= set(rec.source_refs)
    assert h.world.writes() == r["writes_before_replay"]          # 6 re-deliveries, zero writes
    assert {g.gate: g.status for g in h.svc.store.gates_for(hid)} == {
        "workday_hire_complete": "done", "hardware_form_submitted": "done", "background_check_clear": "done",
        "equipment_shipped": "done", "okta_provisioned": "done"}
    assert (calls(h, "workday.hire"), calls(h, "okta.create_user"), calls(h, "expoit.create_order")) == (1, 1, 1)
    assert rec.work_email == "priya.raman@betterup.com"
    assert target(h, hid, "tracker")["Work Email"] == "priya.raman@betterup.com"


def test_s02_start_date_change_propagates_as_minimal_updates():
    h, r = scenario("S02")
    hid = r["hire_id"]
    assert target(h, hid, "workday")["Hire_Date"] == "2026-11-16"
    assert target(h, hid, "okta")["activationDate"] == "2026-11-16"
    order = target(h, hid, "equipment")
    assert order["deliverBy"] == "2026-11-12"                       # rescheduled, not re-ordered
    assert (calls(h, "expoit.create_order"), calls(h, "expoit.patch_order")) == (1, 1)
    row = target(h, hid, "tracker")
    assert (row["Start Date"], row["Cohort"]) == ("2026-11-16", "Cohort 2026-11-16")
    due = {g.gate: g.due_date.isoformat() for g in h.svc.store.gates_for(hid)}
    assert due["hardware_form_submitted"] == "2026-11-05"           # deadlines moved with the start date


def test_s03_login_renamed_with_alias_and_single_identity():
    h, r = scenario("S03")
    hid = r["hire_id"]
    users = okta_users_for(h, hid)
    assert len(users) == 1
    assert users[0]["profile"]["login"] == "john.okafor@betterup.com"
    assert "jhon.okafor@betterup.com" in users[0]["aliases"]
    rec = h.svc.store.get_hire(hid)
    assert rec.work_email == "john.okafor@betterup.com"
    assert "jhon.okafor@betterup.com" in rec.work_email_aliases
    assert target(h, hid, "tracker")["Work Email"] == "john.okafor@betterup.com"
    assert target(h, hid, "workday")["Legal_First_Name"] == "John"
    ex = h.svc.store.list_exceptions(hid)[0]
    assert (ex.kind, ex.state, ex.resolution["by"]) == ("email_mismatch", "resolved", "slack:U07ITOPS")
    assert ex.recommendation["recommended_option"] == "rename_login"


def test_s04_offcycle_hire_is_owned_by_workday():
    h, r = scenario("S04")
    hid = r["hire_id"]
    rec = h.svc.store.get_hire(hid)
    assert rec.origin == "workday" and "ashby" not in rec.source_refs
    assert rec.work_email == "dana.whitfield2@betterup.com"        # former employee still holds dana.whitfield@
    assert calls(h, "workday.hire") == calls(h, "workday.change") == 0   # never push to the owner
    assert target(h, hid, "okta")["activationDate"] == "2026-11-02"
    open_keys = {e.key for e in h.svc.store.list_exceptions(hid, states=("open", "awaiting_approval"))}
    assert "gate:background_check_clear" in open_keys


def test_s05_address_change_is_minimal_and_post_ship_is_an_advisory():
    h, r = scenario("S05")
    hid = r["hire_id"]
    assert r["first_move_delta"] == {"workday.change": 1, "expoit.patch_order": 1}
    assert calls(h, "expoit.patch_order") == 1                      # no doomed PATCH once shipped
    items = h.svc.store.list_exceptions(hid)
    ship = [e for e in items if e.kind == "equipment_after_ship"]
    assert len(ship) == 1 and ship[0].state == "resolved" and ship[0].resolution["by"] == "slack:U07ITOPS"
    assert not h.svc.store.list_exceptions(hid, states=("open", "awaiting_approval"))   # ack sticks
    assert target(h, hid, "workday")["Home_Address"]["City"] == "Berkeley"


def test_s06_retries_adopt_and_park_converge_without_duplicates():
    h, r = scenario("S06")
    for hid in r["hires"]:
        assert len(okta_users_for(h, hid)) == 1
        assert h.svc.store.get_hire(hid).sync_state == "in_sync"
        assert h.svc.store.retries(hid) == []
    assert calls(h, "okta.create_user") == 3                         # one committed create per hire
    assert calls(h, "sheets.append_row") == 3


def test_s07_out_of_order_and_duplicate_webhooks():
    h, r = scenario("S07")
    hid = r["hire_id"]
    outcomes = [e["outcome"] for e in h.svc.store.events_for(hid)]
    assert outcomes.count("stale") == 1
    assert str(h.svc.store.get_hire(hid).start_date) == "2026-11-16"
    assert calls(h, "workday.change") == 1


def test_s08_conflict_is_never_overwritten_and_accept_theirs_propagates():
    h, r = scenario("S08")
    hid = r["hire_id"]
    rec = h.svc.store.get_hire(hid)
    assert str(rec.start_date) == "2026-11-09"
    assert rec.provenance["start_date"].source == "slack:U07PEOPLEOPS"
    assert calls(h, "workday.change") == 0                          # the human's Workday edit was kept
    assert target(h, hid, "okta")["activationDate"] == "2026-11-09"
    assert target(h, hid, "equipment")["deliverBy"] == "2026-11-05"
    ex = [e for e in h.svc.store.list_exceptions(hid) if e.kind == "conflict"][0]
    assert ex.state == "resolved" and ex.recommendation["recommended_option"] == "accept_theirs"


def test_s09_claude_location_is_guarded_and_bad_data_does_not_cascade():
    h, r = scenario("S09")
    marcus = h.svc.store.get_hire(r["marcus"])
    assert marcus.location.site_code == "US-CA-REMOTE"
    assert marcus.provenance["location"].source == "claude"
    assert any(a["action"] == "normalize" and a["actor"] == "claude" for a in h.svc.audit(r["marcus"]))
    lena = h.svc.store.get_hire(r["lena"])
    assert lena.location.site_code == "US-OR-REMOTE" and lena.sync_state == "in_sync"
    held_event = h.svc.store.events_for(r["lena"])[0]
    assert held_event["outcome"] == "accepted"
    ex = [e for e in h.svc.store.list_exceptions(r["lena"]) if e.kind == "validation_blocked"][0]
    assert ex.state == "resolved" and ex.resolution["by"] == "engine"


def test_s10_deprovisioning_waits_for_a_human():
    h, r = scenario("S10")
    hid = r["hire_id"]
    assert target(h, hid, "okta")["status"] == "DEPROVISIONED"
    assert target(h, hid, "workday")["Status"] == "Rescinded"
    assert target(h, hid, "equipment")["status"] == "cancelled"
    assert target(h, hid, "lumos")["status"] == "cancelled"
    assert target(h, hid, "tracker")["Status"] == "Cancelled"
    actions = [(a["actor"], a["action"]) for a in h.svc.audit(hid)]
    approve = actions.index(("slack:U07PEOPLEOPS", "exception.resolve"))
    assert calls(h, "okta.deactivate") == 1 and approve < len(actions) - 1


def test_s11_gate_deadlines_move_with_the_start_date():
    h, r = scenario("S11")
    hid = r["hire_id"]
    assert not h.svc.store.list_exceptions(hid, states=("open", "awaiting_approval"))
    texts = [m["text"] for m in h.world.state["slack"]["messages"]]
    assert any("overdue" in t and "Hardware form" in t for t in texts)
    cleared = [e for e in h.svc.store.list_exceptions(hid) if e.kind == "gate" and e.resolution
               and e.resolution.get("reason") == "deadline moved with the start date"]
    assert len(cleared) >= 2
