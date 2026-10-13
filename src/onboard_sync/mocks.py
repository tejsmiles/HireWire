"""Fake services that misbehave the way the real APIs do.

They're here to exercise failure handling, not to look realistic. Okta returns 409 on a
duplicate login, ExpoIT refuses edits once an order ships, and any call can be made to
fail N times or to fail *after committing*. That last case is the lost response that
breaks naive retries. State can persist to a JSON file, so the CLI, the API and the MCP
server all see the same world. Only engine writes are counted, which is how the
idempotency tests prove that a replay touches nothing.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .errors import TransientError, http_error
from .timeutil import Clock

MANAGERS = ["maria.lopez@betterup.com", "sam.chen@betterup.com", "alex.rivera@betterup.com",
            "jordan.kim@betterup.com", "nina.patel@betterup.com"]


def _initial_state() -> dict[str, Any]:
    okta_users = {}
    for i, email in enumerate(MANAGERS):
        first, last = email.split("@")[0].split(".")
        okta_users[f"00u_mgr{i}"] = {"id": f"00u_mgr{i}", "status": "ACTIVE", "aliases": [], "profile": {
            "login": email, "email": email, "firstName": first.title(), "lastName": last.title()}}
    # A former employee: their login still exists (deprovisioned), so a namesake hire collides.
    okta_users["00u_former"] = {"id": "00u_former", "status": "DEPROVISIONED", "aliases": [], "profile": {
        "login": "dana.whitfield@betterup.com", "email": "dana.whitfield@betterup.com",
        "firstName": "Dana", "lastName": "Whitfield", "hireId": "h_former01"}}
    return {"seq": 1000, "calls": {}, "chaos": [], "clock": None,
            "workday": {"workers": {}, "directory": list(MANAGERS)},
            "okta": {"users": okta_users},
            "lumos": {"requests": {}, "idempotency": {}},
            "expoit": {"orders": {}, "idempotency": {}},
            "sheets": {"rows": {}},
            "slack": {"messages": []}}


class FakeWorld:
    def __init__(self, clock: Clock, path: str | Path | None = None):
        self.clock = clock
        self.path = Path(path) if path else None
        self.state = json.loads(self.path.read_text()) if self.path and self.path.exists() else _initial_state()
        self.workday, self.okta, self.lumos = FakeWorkday(self), FakeOkta(self), FakeLumos(self)
        self.expoit, self.sheets, self.slack = FakeExpoIT(self), FakeSheets(self), FakeSlack(self)

    def now(self) -> str:
        return self.clock.now().isoformat()

    def save(self) -> None:
        if self.path:
            self.state["clock"] = self.now()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.state, indent=1, default=str))

    def next_id(self, prefix: str) -> str:
        self.state["seq"] += 1
        return f"{prefix}{self.state['seq']}"

    def writes(self, service: str | None = None) -> int:
        return sum(n for k, n in self.state["calls"].items() if service is None or k.startswith(service + "."))

    # --- chaos -----------------------------------------------------------------------
    def fail_next(self, service: str, op: str, times: int = 1, status: int = 500, after_commit: bool = False) -> None:
        self.state["chaos"].append({"service": service, "op": op, "times": times, "status": status,
                                    "after_commit": after_commit})
        self.save()

    def _chaos(self, service: str, op: str, after: bool) -> None:
        for rule in self.state["chaos"]:
            if (rule["service"], rule["op"], rule["after_commit"]) == (service, op, after) and rule["times"] > 0:
                rule["times"] -= 1
                self.save()
                if after:
                    raise TransientError(504, "TIMEOUT", f"{service}.{op}: gateway timeout (request was committed)")
                raise http_error(rule["status"], "INJECTED", f"{service}.{op}: injected {rule['status']}")

    def before(self, service: str, op: str) -> None:
        self._chaos(service, op, after=False)

    def committed(self, service: str, op: str) -> None:
        key = f"{service}.{op}"
        self.state["calls"][key] = self.state["calls"].get(key, 0) + 1
        self.save()
        self._chaos(service, op, after=True)


class _Fake:
    service = ""

    def __init__(self, world: FakeWorld):
        self.w = world

    @property
    def s(self) -> dict:
        return self.w.state[self.service]


class FakeWorkday(_Fake):
    service = "workday"

    def manager_exists(self, email: str) -> bool:
        return email.lower() in self.s["directory"]

    def get(self, wid: str) -> dict | None:
        return copy.deepcopy(self.s["workers"].get(wid))

    def find(self, candidate_id: str | None = None, personal_email: str | None = None) -> str | None:
        for wid, wk in self.s["workers"].items():
            if wk.get("Status") == "Rescinded":
                continue
            if (candidate_id and wk.get("External_Candidate_ID") == candidate_id) or \
               (personal_email and wk.get("Personal_Email") == personal_email):
                return wid
        return None

    def hire(self, fields: dict, idempotency_key: str | None = None) -> str:
        """The Hire business process (async in real Workday; synchronous here)."""
        self.w.before("workday", "hire")
        wid = self.w.next_id("W-")
        self.s["workers"][wid] = {"Worker_ID": wid, "Status": "Pre-Hire", **copy.deepcopy(fields),
                                  "Last_Updated": self.w.now()}
        self.w.committed("workday", "hire")
        return wid

    def change(self, wid: str, changes: dict, idempotency_key: str | None = None) -> None:
        self.w.before("workday", "change")
        if wid not in self.s["workers"]:
            raise http_error(404, "NOT_FOUND", f"worker {wid}")
        self.s["workers"][wid].update(copy.deepcopy(changes))
        self.s["workers"][wid]["Last_Updated"] = self.w.now()
        self.w.committed("workday", "change")

    def rescind(self, wid: str) -> None:
        self.w.before("workday", "rescind")
        self.s["workers"][wid]["Status"] = "Rescinded"
        self.s["workers"][wid]["Last_Updated"] = self.w.now()
        self.w.committed("workday", "rescind")

    # --- what humans do directly in Workday (not engine writes) ---------------------------
    def human_create(self, fields: dict) -> str:
        wid = self.w.next_id("W-")
        self.s["workers"][wid] = {"Worker_ID": wid, "Status": "Pre-Hire", **copy.deepcopy(fields),
                                  "Last_Updated": self.w.now()}
        self.w.save()
        return wid

    def human_edit(self, wid: str, changes: dict) -> None:
        self.s["workers"][wid].update(copy.deepcopy(changes))
        self.s["workers"][wid]["Last_Updated"] = self.w.now()
        self.w.save()


class FakeOkta(_Fake):
    service = "okta"

    def _taken(self, login: str) -> dict | None:
        login = login.lower()
        for u in self.s["users"].values():
            if u["profile"]["login"].lower() == login or login in u["aliases"]:
                return u
        return None

    def get(self, uid: str) -> dict | None:
        return copy.deepcopy(self.s["users"].get(uid))

    def find_by_login(self, login: str) -> dict | None:
        return copy.deepcopy(self._taken(login))

    def find_by_hire_id(self, hire_id: str) -> dict | None:
        for u in self.s["users"].values():
            if u["profile"].get("hireId") == hire_id and u["status"] != "DEPROVISIONED":
                return copy.deepcopy(u)
        return None

    def create_user(self, profile: dict) -> str:
        self.w.before("okta", "create_user")
        if self._taken(profile["login"]):
            raise http_error(409, "E0000001", "login: An object with this field already exists")
        uid = self.w.next_id("00u")
        self.s["users"][uid] = {"id": uid, "status": "STAGED", "aliases": [], "profile": copy.deepcopy(profile)}
        self.w.committed("okta", "create_user")
        return uid

    def update_profile(self, uid: str, changes: dict) -> None:
        self.w.before("okta", "update_profile")
        if {"login", "email"} & set(changes):
            raise http_error(400, "E0000001", "login/email change must go through rename_login")
        self.s["users"][uid]["profile"].update(copy.deepcopy(changes))
        self.w.committed("okta", "update_profile")

    def rename_login(self, uid: str, new_login: str) -> None:
        self.w.before("okta", "rename_login")
        if self._taken(new_login):
            raise http_error(409, "E0000001", "login: An object with this field already exists")
        u = self.s["users"][uid]
        u["aliases"].append(u["profile"]["login"])
        u["profile"]["login"] = u["profile"]["email"] = new_login
        self.w.committed("okta", "rename_login")

    def add_alias(self, uid: str, alias: str) -> None:
        self.w.before("okta", "add_alias")
        self.s["users"][uid]["aliases"].append(alias)
        self.w.committed("okta", "add_alias")

    def deactivate(self, uid: str) -> None:
        self.w.before("okta", "deactivate")
        self.s["users"][uid]["status"] = "DEPROVISIONED"
        self.w.committed("okta", "deactivate")


class FakeLumos(_Fake):
    service = "lumos"

    def get(self, rid: str) -> dict | None:
        return copy.deepcopy(self.s["requests"].get(rid))

    def find(self, hire_ref: str) -> str | None:
        return next((r["id"] for r in self.s["requests"].values()
                     if r["hireRef"] == hire_ref and r["status"] != "cancelled"), None)

    def create_request(self, payload: dict, idempotency_key: str) -> str:
        self.w.before("lumos", "create_request")
        if idempotency_key in self.s["idempotency"]:
            return self.s["idempotency"][idempotency_key]
        rid = self.w.next_id("lr_")
        self.s["requests"][rid] = {"id": rid, "status": "pending", **copy.deepcopy(payload)}
        self.s["idempotency"][idempotency_key] = rid
        self.w.committed("lumos", "create_request")
        return rid

    def update_request(self, rid: str, changes: dict) -> None:
        self.w.before("lumos", "update_request")
        self.s["requests"][rid].update(copy.deepcopy(changes))
        self.w.committed("lumos", "update_request")

    def cancel_request(self, rid: str) -> None:
        self.w.before("lumos", "cancel_request")
        self.s["requests"][rid]["status"] = "cancelled"
        self.w.committed("lumos", "cancel_request")


class FakeExpoIT(_Fake):
    """Equipment vendor: honours Idempotency-Key; orders are editable only while 'ordered'."""
    service = "expoit"

    def get(self, oid: str) -> dict | None:
        return copy.deepcopy(self.s["orders"].get(oid))

    def find(self, hire_ref: str) -> str | None:
        return next((o["id"] for o in self.s["orders"].values()
                     if o["hireRef"] == hire_ref and o["status"] != "cancelled"), None)

    def create_order(self, payload: dict, idempotency_key: str) -> str:
        self.w.before("expoit", "create_order")
        if idempotency_key in self.s["idempotency"]:
            return self.s["idempotency"][idempotency_key]
        oid = self.w.next_id("EXP-")
        self.s["orders"][oid] = {"id": oid, "status": "ordered", "tracking": None, **copy.deepcopy(payload)}
        self.s["idempotency"][idempotency_key] = oid
        self.w.committed("expoit", "create_order")
        return oid

    def patch_order(self, oid: str, changes: dict, idempotency_key: str) -> None:
        self.w.before("expoit", "patch_order")
        order = self.s["orders"][oid]
        if order["status"] != "ordered":
            raise http_error(422, "ORDER_ALREADY_SHIPPED", f"order {oid} is {order['status']}")
        order.update(copy.deepcopy(changes))
        self.w.committed("expoit", "patch_order")

    def cancel_order(self, oid: str) -> None:
        self.w.before("expoit", "cancel_order")
        order = self.s["orders"][oid]
        if order["status"] in ("shipped", "delivered"):
            raise http_error(422, "RETURN_REQUIRED", f"order {oid} already {order['status']}; request a return")
        order["status"] = "cancelled"
        self.w.committed("expoit", "cancel_order")

    def vendor_ships(self, oid: str) -> None:
        self.s["orders"][oid].update(status="shipped", tracking=f"1Z{oid[-4:]}EXP")
        self.w.save()


class FakeSheets(_Fake):
    """The cohort tracker. Engine-owned columns plus human-only columns it never touches."""
    service = "sheets"

    def get_row(self, rid: str) -> dict | None:
        return copy.deepcopy(self.s["rows"].get(rid))

    def find_row(self, hire_id: str) -> str | None:
        return next((rid for rid, r in self.s["rows"].items() if r.get("Hire ID") == hire_id), None)

    def append_row(self, values: dict) -> str:
        self.w.before("sheets", "append_row")
        rid = self.w.next_id("row-")
        self.s["rows"][rid] = {**copy.deepcopy(values), "Buddy": "", "Notes": ""}
        self.w.committed("sheets", "append_row")
        return rid

    def update_row(self, rid: str, changes: dict) -> None:
        self.w.before("sheets", "update_row")
        self.s["rows"][rid].update(copy.deepcopy(changes))
        self.w.committed("sheets", "update_row")


class FakeSlack(_Fake):
    service = "slack"

    def post(self, channel: str, text: str, blocks: list | None = None) -> str:
        ts = self.w.next_id("ts.")
        self.s["messages"].append({"ts": ts, "channel": channel, "text": text, "blocks": blocks or [],
                                   "at": self.w.now()})
        self.w.save()
        return ts
