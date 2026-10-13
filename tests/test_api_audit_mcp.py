"""HTTP auth (n8n HMAC, Slack signatures), the audit hash chain, and the MCP console."""
import asyncio
import inspect
import json

import pytest
from conftest import scenario
from onboard_sync.scenarios import Harness
from onboard_sync.slack import action_body, hmac_signature, slack_signature

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from onboard_sync.api import create_app  # noqa: E402

NOW = 1_800_000_000


def _client(svc):
    return TestClient(create_app(svc, now=lambda: NOW))


def _event(h: Harness) -> bytes:
    env = h.ashby("priya_raman", type="offer.accepted", send=False)
    return env.model_dump_json().encode()


def test_events_require_a_valid_hmac(svc):
    client, body = _client(svc), _event(Harness(svc, verbose=False))
    assert client.post("/events", content=body).status_code == 401
    assert client.post("/events", content=body, headers={"X-Signature": "00" * 32}).status_code == 401
    sig = hmac_signature(svc.settings.hmac_secret, body)
    ok = client.post("/events", content=body, headers={"X-Signature": sig})
    assert ok.status_code == 200 and ok.json()["status"] == "accepted"
    again = client.post("/events", content=body, headers={"X-Signature": sig})
    assert again.json()["status"] == "duplicate"


def test_slack_actions_verify_signature_and_replay_window():
    from conftest import make_service
    svc = make_service()
    h = Harness(svc, verbose=False)
    hid = h.ashby("jhon_okafor", type="offer.accepted")["hire_id"]
    h.ashby("jhon_okafor", changes={"candidate.firstName": "John"})
    ex = h.open_exception("email_mismatch", hid)
    client, body = _client(svc), action_body(ex.id, "add_alias", "U07ITOPS")
    secret = svc.settings.slack_signing_secret

    def post(ts, sig):
        return client.post("/slack/actions", content=body, headers={
            "X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sig,
            "Content-Type": "application/x-www-form-urlencoded"})
    assert post(NOW, "v0=bad").status_code == 401
    assert post(NOW - 600, slack_signature(secret, str(NOW - 600), body)).status_code == 401   # replayed
    res = post(NOW, slack_signature(secret, str(NOW), body))
    assert res.status_code == 200 and "add_alias" in res.json()["text"]
    assert svc.store.get_exception(ex.id).resolution["by"] == "slack:U07ITOPS"
    assert post(NOW, slack_signature(secret, str(NOW), body)).json()["text"].endswith("already_resolved")


def test_audit_chain_detects_tampering():
    h, _ = scenario("S08")
    assert h.svc.verify_audit()["ok"]
    h.svc.store._conn.execute("UPDATE audit SET body = replace(body, '2026-11-09', '2026-12-25') WHERE seq = 6")
    report = h.svc.verify_audit()
    assert report == {"ok": False, "first_bad_seq": 6, "entries": report["entries"]}


def test_mcp_console_reads_and_resolves_with_attribution():
    pytest.importorskip("mcp")
    from onboard_sync.mcp_server import build_server
    h = Harness(verbose=False)
    hid = h.ashby("jhon_okafor", type="offer.accepted")["hire_id"]
    h.ashby("jhon_okafor", changes={"candidate.firstName": "John"})
    server = build_server(lambda: h.svc)

    async def call(name, args):
        r = server.call_tool(name, args)
        r = await r if inspect.isawaitable(r) else r
        content = r.content if hasattr(r, "content") else r[0] if isinstance(r, tuple) else r
        return json.loads(content[0].text)

    async def main():
        tools = server.list_tools()
        tools = await tools if inspect.isawaitable(tools) else tools
        assert {t.name for t in tools} >= {"list_hires", "get_hire", "diff_hire", "resolve_exception"}
        exs = (await call("list_exceptions", {}))["exceptions"]
        assert exs[0]["kind"] == "email_mismatch"
        await call("resolve_exception", {"exception_id": exs[0]["id"], "option": "rename_login", "operator": "alice"})
        return (await call("get_hire", {"hire": "John Okafor"}))["record"]["work_email"]

    assert asyncio.run(main()) == "john.okafor@betterup.com"
    assert any(a["actor"] == "mcp:alice" for a in h.svc.audit(hid))
