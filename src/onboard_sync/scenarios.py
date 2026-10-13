"""Demo scenarios on sample data.

Each scenario is a short story told through real events (Ashby webhooks, Workday polls,
hardware-form submissions, Slack clicks, clock advances). The tests assert on these same
runs, so the demo *is* the test suite.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .models import EventEnvelope
from .service import Service
from .slack import action_body, parse_action
from .timeutil import Clock

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
T0 = datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc)      # Tue 13 Oct 2026, 09:00 Pacific


def set_path(obj: dict, dotted: str, value) -> None:
    *head, last = dotted.split(".")
    for k in head:
        obj = obj.setdefault(k, {})
    obj[last] = value


class Harness:
    def __init__(self, svc: Service | None = None, verbose: bool = True, out: Callable[[str], None] = print):
        self.svc = svc or Service(clock=Clock(T0), settings=Settings(claude_mode="stub", retry_base_seconds=0))
        self.world = self.svc.world
        self.out = out if verbose else (lambda *_: None)
        self.sent: list[EventEnvelope] = []
        self._ashby: dict[str, dict] = {}          # Ashby's current state per candidate (webhooks send snapshots)
        self._wd_seen: dict[str, str] = {}         # what the n8n Workday poller has already forwarded
        self._slack_seen = len(self.world.state["slack"]["messages"])

    # ------------------------------------------------------------------ narration
    def step(self, title: str) -> None:
        self.out(f"\n== {title}")

    def _new_slack(self) -> None:
        msgs = self.world.state["slack"]["messages"]
        for m in msgs[self._slack_seen:]:
            self.out(f"     slack {m['channel']}: {m['text']}")
            ctx = [b for b in m["blocks"] if b.get("type") == "context"]
            if ctx:
                self.out(f"       {ctx[0]['elements'][0]['text']}")
            sec = [b for b in m["blocks"] if b.get("type") == "section"]
            if ctx and sec:
                why = sec[0]["text"]["text"].split("\n", 1)[-1]
                self.out(f"       why: {why}")
        self._slack_seen = len(msgs)

    def _narrate(self, r: dict) -> None:
        st = r["status"]
        if st in ("duplicate", "stale", "no_change", "rejected"):
            extra = r.get("error") or (f"stale fields: {', '.join(r['stale_fields'])}" if r.get("stale_fields") else "")
            self.out(f"     {st.upper()} {extra}".rstrip())
            return
        ch = r.get("changes", [])
        names = ", ".join(c["field"] for c in ch) if 0 < len(ch) <= 4 else f"{len(ch)} fields"
        self.out(f"     {st} | matched by {r['matched_by']} | v{r['version']} | changes: {names or 'none'}")
        if r.get("lifecycle"):
            self.out(f"     lifecycle: {r['lifecycle']['from']} -> {r['lifecycle']['to']}")
        if r.get("drift"):
            self.out(f"     drift (non-owner edit, not applied): {', '.join(d['field'] for d in r['drift'])}")
        for f in r.get("findings", []):
            self.out(f"     validation {f['severity']}: {f['message']}")
        self._narrate_reconcile(r.get("reconcile"))
        self._new_slack()

    def _narrate_reconcile(self, rec: dict | None) -> None:
        if not rec or "passes" not in rec:
            return
        for p in rec["passes"]:
            if p["results"] or p["pass"] == 1:
                self.out(f"     plan[{p['pass']}]: {'  '.join(p['plan'])}")
                for res in p["results"]:
                    self.out(f"        {res}")
        gates = "  ".join(f"{g}={s}" for g, s in rec.get("gates", {}).items())
        self.out(f"     sync_state={rec['sync_state']}" + (f" | gates: {gates}" if gates else ""))

    def show(self, hire_id: str) -> None:
        v = self.svc.hire(hire_id)
        r = v["record"]
        self.out(f"\n   [{hire_id}] {r['preferred_first_name'] or r['legal_first_name']} {r['legal_last_name']} | "
                 f"{r['lifecycle']}/{r['sync_state']} | start {r['start_date']} | {r['work_email']} | "
                 f"refs {r['source_refs']}")
        open_ex = [f"{e['kind']}({e['state']})" for e in v["exceptions"] if e["state"] in ("open", "awaiting_approval")]
        self.out(f"   open exceptions: {', '.join(open_ex) or 'none'}")

    # ------------------------------------------------------------------ time
    def at(self, day: str) -> None:
        self.svc.clock.set(datetime.fromisoformat(day).replace(hour=16, tzinfo=timezone.utc))
        self.out(f"   clock -> {self.svc.clock.now():%a %d %b %Y}")

    def advance(self, hours: int = 0, days: int = 0) -> None:
        self.svc.clock.advance(days=days, hours=hours)

    # ------------------------------------------------------------------ sources
    def _eid(self, *parts) -> str:
        return parts[0] + ":" + hashlib.sha1(":".join(map(str, parts)).encode()).hexdigest()[:12]

    def ashby(self, sample: str, type: str = "candidate.updated", changes: dict | None = None,
              at: datetime | None = None, send: bool = True):
        payload = self._ashby.setdefault(sample, json.loads((SAMPLES / "ashby" / f"{sample}.json").read_text()))
        for path, value in (changes or {}).items():
            set_path(payload["data"], path, value)
        cand = payload["data"]["candidate"]["id"]
        if at is None:
            self.svc.clock.advance(minutes=5)      # real events never share an instant
        occurred = at or self.svc.clock.now()
        env = EventEnvelope(event_id=self._eid("ashby", cand, type, occurred.isoformat(), json.dumps(payload)),
                            source="ashby", type=type, external_id=cand, occurred_at=occurred,
                            payload=copy.deepcopy(payload))
        return self.deliver(env) if send else env

    def deliver(self, env: EventEnvelope) -> dict:
        self.out(f"  -> {env.source} {env.type} [{env.event_id}] occurred {env.occurred_at:%d %b %H:%M}")
        self.sent.append(env)
        r = self.svc.ingest(env)
        self._narrate(r)
        return r

    def workday_poll(self) -> list[dict]:
        """What the n8n poller does: each worker whose Last_Updated moved becomes one event."""
        out = []
        for wid, wk in list(self.world.state["workday"]["workers"].items()):
            if self._wd_seen.get(wid) == wk["Last_Updated"]:
                continue
            self._wd_seen[wid] = wk["Last_Updated"]
            env = EventEnvelope(event_id=f"workday:{wid}:{wk['Last_Updated']}", source="workday",
                                type="worker.rescinded" if wk.get("Status") == "Rescinded" else "worker.changed",
                                external_id=wid, occurred_at=datetime.fromisoformat(wk["Last_Updated"]),
                                payload={"worker": copy.deepcopy(wk)})
            out.append(self.deliver(env))
        return out

    def hardware_form(self, hire_id: str, personal_email: str, **fields) -> dict:
        payload = {"submission_id": f"form_{hire_id}", "hire_id": hire_id, "personal_email": personal_email,
                   "equipment_choice": None, "ship_to_different_address": False, **fields}
        payload = {k: v for k, v in payload.items() if v is not None}
        self.svc.clock.advance(minutes=5)
        env = EventEnvelope(event_id=self._eid("it_form", hire_id, json.dumps(payload, sort_keys=True)),
                            source="it_form", type="hardware_form.submitted", external_id=payload["submission_id"],
                            occurred_at=self.svc.clock.now(), payload=payload)
        return self.deliver(env)

    def replay_all(self, new_ids: bool = False) -> list[dict]:
        out = []
        for env in list(self.sent):
            again = env.model_copy(update={"event_id": env.event_id + ":redelivery"}) if new_ids else env
            r = self.svc.ingest(again)
            out.append(r)
        statuses = {}
        for r in out:
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        self.out(f"     replayed {len(out)} events -> {statuses}")
        return out

    # ------------------------------------------------------------------ other actors
    def chaos(self, service: str, op: str, times: int = 1, status: int = 500, after_commit: bool = False) -> None:
        self.world.fail_next(service, op, times, status, after_commit)
        what = "commit then time out" if after_commit else f"return {status}"
        self.out(f"   chaos: {service}.{op} will {what} x{times}")

    def vendor_ships(self, hire_id: str) -> None:
        oid = self.svc.store.get_hire(hire_id).source_refs["equipment"]
        self.world.expoit.vendor_ships(oid)
        self.out(f"   ExpoIT ships order {oid}")

    def sweep(self) -> dict:
        self.out("  -> n8n cron: POST /sweep")
        r = self.svc.sweep()
        for hid, summary in r["reconciled"].items():
            self.out(f"     {hid}: {summary.get('sync_state')}")
            for p in summary.get("passes", []):
                for res in p["results"]:
                    self.out(f"        {res}")
        self._new_slack()
        return r

    def workday_edit(self, wid: str, changes: dict) -> None:
        self.svc.clock.advance(minutes=5)
        self.world.workday.human_edit(wid, changes)
        self.out(f"   Workday {wid} edited by hand: {changes}")

    def open_exception(self, kind: str, hire_id: str | None = None, key: str | None = None):
        for e in self.svc.store.list_exceptions(hire_id, states=("open", "awaiting_approval")):
            if e.kind == kind and (key is None or e.key == key):
                return e
        raise LookupError(f"no open {kind} exception")

    def click(self, kind: str, option: str, user: str = "U07PEOPLEOPS", hire_id: str | None = None,
              key: str | None = None) -> dict:
        ex = self.open_exception(kind, hire_id, key)
        ex_id, opt, uid = parse_action(action_body(ex.id, option, user))   # exactly what Slack POSTs
        self.out(f"  -> slack: {uid} clicks [{opt}] on the {kind} card ({ex_id})")
        r = self.svc.resolve(ex_id, opt, actor=f"slack:{uid}")
        self.out(f"     {r['status']}")
        out = r.get("outcome")
        if isinstance(out, dict):
            self._narrate_reconcile(out)
        self._new_slack()
        return r


# ---------------------------------------------------------------------- scenarios
PRIYA_EMAIL = "priya.raman.home@gmail.com"
NEW_ADDR = {"line1": "1550 Bryant St", "line2": None, "city": "San Francisco", "region": "CA",
            "postalCode": "94103", "country": "US"}
NEWER_ADDR = {"line1": "2120 Milvia St", "line2": "Unit 9", "city": "Berkeley", "region": "CA",
              "postalCode": "94704", "country": "US"}


def s01_happy_path(h: Harness) -> dict:
    h.step("Offer accepted in Ashby")
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    h.step("The hire submits the IT hardware form (pre-filled link carries the hire id)")
    h.hardware_form(hid, PRIYA_EMAIL)
    h.step("Background check clears (vendor reports into Ashby)")
    h.ashby("priya_raman", changes={"backgroundCheck.status": "clear"})
    h.step("ExpoIT ships the laptop; next morning's sweep observes it")
    h.vendor_ships(hid)
    h.advance(days=1)
    h.sweep()
    h.step("Replay storm: every webhook re-delivered, twice (same ids, then new ids)")
    before = h.world.writes()
    h.replay_all()
    h.replay_all(new_ids=True)
    h.out(f"     target writes during replay: {h.world.writes() - before}")
    h.show(hid)
    return {"hire_id": hid, "writes_before_replay": before}


def s02_start_date_moves(h: Harness) -> dict:
    h.step("Offer accepted; the hire submits the hardware form (laptop ordered)")
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    h.hardware_form(hid, PRIYA_EMAIL)
    h.step("Recruiter pushes the start date by two weeks in Ashby")
    h.ashby("priya_raman", changes={"offer.startDate": "2026-11-16"})
    h.show(hid)
    return {"hire_id": hid}


def s03_name_fix_after_okta(h: Harness) -> dict:
    h.step("Offer accepted with a typo in the first name ('Jhon')")
    hid = h.ashby("jhon_okafor", type="offer.accepted")["hire_id"]
    h.step("Recruiter fixes the typo in Ashby after Okta was provisioned")
    h.ashby("jhon_okafor", changes={"candidate.firstName": "John"})
    h.step("IT clicks the recommended option on the Slack card")
    h.click("email_mismatch", "rename_login", user="U07ITOPS")
    h.show(hid)
    return {"hire_id": hid}


def s04_offcycle_hire(h: Harness) -> dict:
    h.step("People Ops creates an off-cycle hire directly in Workday (no Ashby record)")
    wid = h.world.workday.human_create(json.loads((SAMPLES / "workday" / "dana_whitfield.json").read_text()))
    h.out(f"   Workday worker {wid} created by hand")
    h.step("n8n's Workday poll picks it up")
    hid = h.workday_poll()[0]["hire_id"]
    h.out("   note: dana.whitfield@ is still held by a deprovisioned former employee, so Okta allocated "
          "dana.whitfield2@ (adapter-level collision handling)")
    h.step("HR moves the start date in Workday (Workday owns off-cycle hires, so it propagates)")
    h.workday_edit(wid, {"Hire_Date": "2026-11-02"})
    h.workday_poll()
    h.step("Sweep on Thu 22 Oct: no background check on file for this off-cycle hire")
    h.at("2026-10-22")
    h.sweep()
    h.show(hid)
    return {"hire_id": hid, "worker_id": wid}


def s05_address_change(h: Harness) -> dict:
    h.step("Offer accepted; hardware form submitted (laptop ordered)")
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    h.hardware_form(hid, PRIYA_EMAIL)
    h.step("The hire moves before the laptop ships: minimal diff")
    before = dict(h.world.state["calls"])
    h.ashby("priya_raman", changes={"candidate.homeAddress": NEW_ADDR})
    delta = {k: v - before.get(k, 0) for k, v in h.world.state["calls"].items() if v != before.get(k, 0)}
    h.out(f"     writes caused by the address change: {delta}")
    h.step("ExpoIT ships, then the hire moves again")
    h.vendor_ships(hid)
    h.ashby("priya_raman", changes={"candidate.homeAddress": NEWER_ADDR})
    h.step("IT arranges a carrier redirect and acknowledges")
    h.click("equipment_after_ship", "mark_resolved", user="U07ITOPS")
    h.step("Next sweep: acknowledged facts don't re-alert")
    h.advance(days=1)
    h.sweep()
    h.show(hid)
    return {"hire_id": hid, "first_move_delta": delta}


def s06_retries(h: Harness) -> dict:
    h.step("Okta returns 500 twice and Sheets rate-limits once during provisioning")
    h.chaos("okta", "create_user", times=2, status=500)
    h.chaos("sheets", "append_row", times=1, status=429)
    a = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    h.step("Lost response: Okta commits the user, then the response times out")
    h.chaos("okta", "create_user", times=1, after_commit=True)
    b = h.ashby("jhon_okafor", type="offer.accepted")["hire_id"]
    h.step("Okta hard down: in-request retries exhausted, so the hire is parked (Lumos waits)")
    h.chaos("okta", "create_user", times=6, status=503)
    c = h.ashby("marcus_bell", type="offer.accepted")["hire_id"]
    h.step("Next sweep re-runs the reconciliation (not the stale operation) and converges")
    h.advance(hours=1)
    h.sweep()
    h.show(c)
    return {"hires": [a, b, c]}


def s07_out_of_order(h: Harness) -> dict:
    h.step("Offer accepted")
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    h.advance(hours=3)
    now = h.svc.clock.now()
    older = h.ashby("priya_raman", changes={"offer.startDate": "2026-11-09"}, at=now - timedelta(hours=2), send=False)
    newer = h.ashby("priya_raman", changes={"offer.startDate": "2026-11-16"}, at=now - timedelta(hours=1), send=False)
    h.step("Two start-date edits; the webhooks arrive newest first")
    h.deliver(newer)
    h.deliver(older)
    h.step("The newer webhook is delivered again")
    h.deliver(newer)
    h.show(hid)
    return {"hire_id": hid}


def s08_workday_conflict(h: Harness) -> dict:
    h.step("Offer accepted; hardware form submitted")
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    h.hardware_form(hid, PRIYA_EMAIL)
    wid = h.svc.store.get_hire(hid).source_refs["workday"]
    h.step("People Ops edits the hire date directly in Workday, outside the flow")
    h.workday_edit(wid, {"Hire_Date": "2026-11-09"})
    h.step("n8n's Workday poll: three-way diff spots an out-of-band edit, so a conflict is raised (not overwritten)")
    h.workday_poll()
    h.step("People Ops accepts Workday's date on the Slack card; it propagates everywhere else")
    h.click("conflict", "accept_theirs")
    h.show(hid)
    return {"hire_id": hid}


def s09_fuzzy_location(h: Harness) -> dict:
    h.step("Ashby location 'Remote - Bay Area' isn't in the Workday location master")
    m = h.ashby("marcus_bell", type="offer.accepted")["hire_id"]
    h.step("Same label, but this hire's home address is in Portland, OR")
    lena = h.ashby("lena_ortiz", type="offer.accepted")["hire_id"]
    h.step("Recruiter corrects the location in Ashby; the hold releases by itself")
    h.ashby("lena_ortiz", changes={"offer.location": {"name": "Remote - Oregon", "countryCode": "US", "region": "OR"}})
    h.show(m)
    h.show(lena)
    return {"marcus": m, "lena": lena}


def s10_offer_reneged(h: Harness) -> dict:
    h.step("Offer accepted; hardware form submitted (accounts + laptop in flight)")
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    h.hardware_form(hid, PRIYA_EMAIL)
    h.step("Candidate withdraws in Ashby")
    h.ashby("priya_raman", type="candidate.withdrew")
    h.step("People Ops approves deprovisioning on the Slack card")
    h.click("deprovision_pending", "approve")
    h.show(hid)
    return {"hire_id": hid}


def s11_gates(h: Harness) -> dict:
    h.step("Offer accepted (start Mon 2 Nov); no hardware form yet, background check in progress")
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    h.step("Tue 20 Oct sweep: hardware form (due T-7 = Thu 22 Oct) is at risk")
    h.at("2026-10-20")
    h.sweep()
    h.step("IT snoozes the reminder for a day")
    h.click("gate", "snooze_1d", user="U07ITOPS", key="gate:hardware_form_submitted")
    h.step("Fri 23 Oct sweep: form overdue; background check and shipping now at risk")
    h.at("2026-10-23")
    h.sweep()
    h.step("Recruiter pushes the start date two weeks: every deadline moves and open gates clear")
    h.ashby("priya_raman", changes={"offer.startDate": "2026-11-16"})
    h.show(hid)
    return {"hire_id": hid}


SCENARIOS: dict[str, tuple[str, Callable[[Harness], dict]]] = {
    "S01": ("Happy path, then a webhook replay storm changes nothing", s01_happy_path),
    "S02": ("Start date moves after the laptop is ordered", s02_start_date_moves),
    "S03": ("Name typo fixed after Okta was provisioned", s03_name_fix_after_okta),
    "S04": ("Off-cycle hire created directly in Workday", s04_offcycle_hire),
    "S05": ("Address change: minimal diff, then after the laptop shipped", s05_address_change),
    "S06": ("Transient errors, lost responses and a hard outage", s06_retries),
    "S07": ("Out-of-order and duplicate webhooks", s07_out_of_order),
    "S08": ("Someone edits Workday directly: conflict, Claude triage, Slack approval", s08_workday_conflict),
    "S09": ("Free-text location: Claude proposes, a deterministic guard decides", s09_fuzzy_location),
    "S10": ("Offer reneged: deprovisioning waits for a human", s10_offer_reneged),
    "S11": ("Gate deadlines: nudges, snooze, overdue, and deadlines that move", s11_gates),
}


def run(sid: str, svc: Service | None = None, verbose: bool = True,
        out: Callable[[str], None] = print) -> tuple[Harness, dict]:
    title, fn = SCENARIOS[sid]
    h = Harness(svc, verbose=verbose, out=out)
    h.out(f"\n######## {sid}: {title}")
    return h, fn(h)
