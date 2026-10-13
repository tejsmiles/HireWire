"""Admission gate and Claude guardrails."""
from datetime import date
from types import SimpleNamespace

import pytest
from conftest import make_service
from onboard_sync.claude import Decision, LiveAdvisor, LocationProposal, StubAdvisor
from onboard_sync.models import Address, HireRecord, Location
from onboard_sync.scenarios import Harness
from onboard_sync.validation import name_findings


def _held(h: Harness, sample: str, changes: dict) -> dict:
    r = h.ashby(sample, type="offer.accepted", changes=changes)
    assert r["status"] == "held"
    assert h.world.writes() == 0                       # bad data never reaches any system
    return r


@pytest.mark.parametrize("changes, rule", [
    ({"candidate.firstName": "Pr1ya"}, "name_format"),
    ({"candidate.lastName": "TBD"}, "name_format"),
    ({"offer.hiringManagerEmail": "nobody@betterup.com"}, "manager_unknown"),
    ({"offer.startDate": "2026-11-26"}, "start_date"),                      # Thanksgiving
    ({"offer.startDate": "2026-10-31"}, "start_date"),                      # Saturday
    ({"offer.startDate": "2026-10-01"}, "start_date"),                      # in the past
    ({"candidate.homeAddress": {"line1": "1 Main", "city": "London", "postalCode": "N1", "country": "GB"}},
     "ship_to_country"),
    ({"offer.location": {"name": "Mars Base", "countryCode": "US", "region": None}}, "location_unresolved"),
])
def test_blocking_rules(h, changes, rule):
    r = _held(h, "priya_raman", changes)
    assert rule in {f["rule"] for f in r["findings"] if f["severity"] == "block"}


def test_names_are_permissive_by_design():
    for ok in ("Li", "O'Brien", "Anne-Marie", "José", "Nguyễn", "Prince", "毛"):
        assert not [f for f in name_findings("legal_first_name", ok) if f.severity == "block"], ok
    assert [f.severity for f in name_findings("legal_last_name", "OKAFOR")] == ["warn"]


def test_judge_failure_fails_closed_without_crashing():
    class Broken(StubAdvisor):
        def judge_location(self, ctx, candidates):
            raise TimeoutError("anthropic timeout")
    svc = make_service()
    svc.engine.validator.advisor = Broken()
    r = Harness(svc, verbose=False).ashby("marcus_bell", type="offer.accepted")
    assert r["status"] == "held" and "auto-mapping failed" in r["findings"][0]["message"]


def test_low_confidence_judge_blocks(h):
    class Unsure(StubAdvisor):
        def judge_location(self, ctx, candidates):
            return LocationProposal(site_code="US-CA-REMOTE", confidence=0.4, rationale="ambiguous")
    h.svc.engine.validator.advisor = Unsure()
    r = h.ashby("marcus_bell", type="offer.accepted")
    assert r["status"] == "held"


# --- triage guardrails -------------------------------------------------------------------------
class Overconfident(StubAdvisor):
    def triage(self, exc, ctx, options):
        return Decision(recommended_option="accept_theirs", action="auto_resolve", confidence=0.99,
                        rationale="sure", message="sure")


def _conflict(h: Harness) -> str:
    hid = h.ashby("priya_raman", type="offer.accepted")["hire_id"]
    h.workday_edit(h.svc.store.get_hire(hid).source_refs["workday"], {"Hire_Date": "2026-11-09"})
    h.workday_poll()
    return hid


def test_claude_cannot_grant_itself_autonomy(h):
    h.svc.engine.desk.advisor = Overconfident()
    hid = _conflict(h)
    ex = [e for e in h.svc.store.list_exceptions(hid) if e.kind == "conflict"][0]
    assert ex.state == "awaiting_approval" and ex.recommendation["action"] == "request_approval"
    assert str(h.svc.store.get_hire(hid).start_date) == "2026-11-02"            # nothing applied


def test_policy_whitelisted_decision_auto_resolves_and_is_audited_as_claude():
    svc = make_service(auto_resolve=frozenset({("conflict", "accept_theirs")}))
    svc.engine.desk.advisor = Overconfident()
    h = Harness(svc, verbose=False)
    hid = _conflict(h)
    assert str(svc.store.get_hire(hid).start_date) == "2026-11-09"
    assert any(a["actor"] == "claude" and a["action"] == "exception.resolve" for a in svc.audit(hid))


def test_invalid_option_and_outage_fall_back_to_a_human(h):
    class Hallucinating(StubAdvisor):
        def triage(self, exc, ctx, options):
            return Decision(recommended_option="delete_everything", confidence=0.9, rationale="x", message="x")
    h.svc.engine.desk.advisor = Hallucinating()
    hid = _conflict(h)
    ex = [e for e in h.svc.store.list_exceptions(hid) if e.kind == "conflict"][0]
    assert ex.recommendation["source"] == "fallback" and ex.recommendation["recommended_option"] is None
    assert ex.state == "awaiting_approval" and h.world.state["slack"]["messages"]


def test_live_advisor_forces_structured_tool_output():
    sent = {}

    class FakeMessages:
        def create(self, **kw):
            sent.update(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={
                "recommended_option": "keep_ours", "action": "request_approval", "confidence": 0.7,
                "rationale": "r", "message": "m"})])
    adv = LiveAdvisor.__new__(LiveAdvisor)
    adv.client, adv.model = SimpleNamespace(messages=FakeMessages()), "claude-test"
    options = {"accept_theirs": "a", "keep_ours": "b"}
    d = adv.triage({"kind": "conflict", "details": {}}, {}, options)
    assert d.recommended_option == "keep_ours" and d.source == "claude"
    assert sent["tool_choice"] == {"type": "tool", "name": "record_decision"}
    assert sent["tools"][0]["input_schema"]["properties"]["recommended_option"]["enum"] == list(options)


def test_redaction_keeps_pii_out_of_prompts():
    from onboard_sync.claude import redact_for_llm
    rec = HireRecord(hire_id="h", origin="ashby", personal_email="priya.raman@gmail.com",
                     home_address=Address(line1="4127 Piedmont Ave", city="Oakland", region="CA",
                                          postal_code="94611", country="US"),
                     location=Location(label="Remote"), start_date=date(2026, 11, 2))
    blob = str(redact_for_llm(rec))
    assert "Piedmont" not in blob and "94611" not in blob and "priya.raman@" not in blob and "Oakland" in blob

def test_openai_advisor_forces_function_call():
    import json
    from onboard_sync.claude import OpenAIAdvisor
    sent = {}

    class FakeCompletions:
        def create(self, **kw):
            sent.update(kw)
            call = SimpleNamespace(function=SimpleNamespace(arguments=json.dumps({
                "recommended_option": "keep_ours", "action": "request_approval", "confidence": 0.7,
                "rationale": "r", "message": "m"})))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])
    adv = OpenAIAdvisor.__new__(OpenAIAdvisor)
    adv.client, adv.model = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())), "gpt-6-luna"
    d = adv.triage({"kind": "conflict", "details": {}}, {}, {"accept_theirs": "a", "keep_ours": "b"})
    assert d.recommended_option == "keep_ours" and d.source == "openai"
    assert sent["tool_choice"] == {"type": "function", "function": {"name": "record_decision"}}