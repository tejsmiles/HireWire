"""Claude works at the edges of the system and never in the control plane.

It does three jobs:
  judge_location  map a free-text location to the Workday location master (validation's long tail)
  triage          recommend one allowed option for an exception and write the Slack explanation
  draft_nudge     write gate reminders a human will act on

The contract is the same for every job: a forced tool call returns structured output, pydantic
validates it, and any error or invalid answer falls back to something safe (block, or ask a
human). Claude never executes anything. The engine applies a recommendation automatically
only when policy (Settings.auto_resolve) allows it, so the model can't escalate its own
privileges.
"""
from __future__ import annotations

import json
import os
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import Settings
from .models import HireRecord
from .ownership import wire


class LocationProposal(BaseModel):
    site_code: str | None
    confidence: float = Field(ge=0, le=1)
    rationale: str
    source: str = "claude"


class Decision(BaseModel):
    recommended_option: str | None
    action: Literal["auto_resolve", "request_approval", "escalate"] = "request_approval"
    confidence: float = Field(ge=0, le=1)
    rationale: str
    message: str
    source: str = "claude"


def redact_for_llm(rec: HireRecord) -> dict[str, Any]:
    """Minimum necessary context: no street lines, postal codes or full personal email."""
    def place(a):
        return None if a is None else {"city": a.city, "region": a.region, "country": a.country}
    email = rec.personal_email or ""
    return {
        "hire_id": rec.hire_id, "origin": rec.origin, "lifecycle": rec.lifecycle,
        "legal_first_name": rec.legal_first_name, "legal_last_name": rec.legal_last_name,
        "preferred_first_name": rec.preferred_first_name,
        "personal_email": (email[0] + "***@" + email.split("@", 1)[1]) if "@" in email else None,
        "work_email": rec.work_email, "title": rec.title, "department": rec.department,
        "employment_type": rec.employment_type, "location": wire(rec.location),
        "start_date": wire(rec.start_date), "home": place(rec.home_address),
        "ship_to": place(rec.shipping_address or rec.home_address), "systems": sorted(rec.source_refs),
    }


def fallback_decision(reason: str) -> Decision:
    return Decision(recommended_option=None, action="request_approval", confidence=0.0,
                    rationale=f"No automated recommendation ({reason}).",
                    message="Automated triage was unavailable, so this needs a human review.", source="fallback")


def apply_guardrails(d: Decision, kind: str, options: dict[str, str], settings: Settings) -> Decision:
    if d.recommended_option is not None and d.recommended_option not in options:
        return fallback_decision(f"model proposed '{d.recommended_option}', which is not an allowed option")
    if d.action == "auto_resolve" and (
            (kind, d.recommended_option) not in settings.auto_resolve
            or d.confidence < settings.auto_resolve_min_confidence):
        d = d.model_copy(update={
            "action": "request_approval",
            "rationale": d.rationale + " [Policy: this decision type is not cleared for auto-resolution.]"})
    return d


# --------------------------------------------------------------------------------------
# Deterministic stand-in (tests, CI, and demos without an API key). Same contract.
# --------------------------------------------------------------------------------------
_REGION_HINTS = {"bay area": "CA", "silicon valley": "CA", "los angeles": "CA", "socal": "CA",
                 "nyc": "NY", "brooklyn": "NY", "austin": "TX", "dallas": "TX", "seattle": "WA",
                 "portland": "OR"}


class StubAdvisor:
    name = "stub"

    def judge_location(self, ctx: dict, candidates: dict[str, dict]) -> LocationProposal:
        label = ((ctx.get("location") or {}).get("label") or "").casefold()
        for token, region in _REGION_HINTS.items():
            if token in label:
                code = f"US-{region}-REMOTE" if "remote" in label else ("US-CA-SF" if region == "CA" else None)
                if code in candidates:
                    return LocationProposal(site_code=code, confidence=0.9, source="stub",
                                            rationale=f"'{token}' is in {region}; the label says "
                                                      f"{'remote' if 'remote' in label else 'office'} -> {code}")
        return LocationProposal(site_code=None, confidence=0.2, source="stub",
                                rationale="no recognizable place in the label")

    def triage(self, exc: dict, ctx: dict, options: dict[str, str]) -> Decision:
        kind, d, days = exc["kind"], exc.get("details", {}), ctx.get("business_days_to_start")
        if kind == "conflict" and "Hire_Date" in d.get("fields", []):
            opt, conf = "accept_theirs", 0.78
            why = (f"Workday's hire date was edited directly to {d['theirs'].get('Hire_Date')} while Ashby still "
                   f"says {d['ours'].get('Hire_Date')}. Direct Workday edits before day 1 usually come from People "
                   f"Ops after talking to the hire. Accepting moves Okta activation, the laptop delivery date and "
                   f"the cohort; confirm with the recruiter first.")
        elif kind == "conflict":
            opt, conf = "keep_ours", 0.7
            why = "Workday was edited directly for fields Ashby owns before day 1. Re-apply Ashby's values unless HR confirms."
        elif kind == "email_mismatch":
            if days is not None and days >= 3:
                opt, conf = "rename_login", 0.86
                why = (f"The name was corrected after Okta was provisioned. The hire starts in {days} business days and has "
                       f"never signed in, so renaming now avoids a permanently misspelled address. The old login stays as an alias.")
            else:
                opt, conf = "add_alias", 0.8
                why = "The start date is too close to rename the login safely, so add the corrected address as an alias."
        elif kind == "deprovision_pending":
            opt, conf = "approve", 0.8
            why = ("The offer was withdrawn before day 1. Deprovision to avoid orphaned access, cancel the laptop order "
                   "and rescind the Workday pre-hire.")
        elif kind == "equipment_after_ship":
            opt, conf = "mark_resolved", 0.75
            why = (f"The laptop already shipped to {d.get('shipped_to', {}).get('city')}. Ask ExpoIT for a carrier redirect "
                   f"to {d.get('should_ship_to', {}).get('city')} (or a return label and re-ship), then mark this resolved.")
        elif kind == "possible_duplicate":
            opt, conf = "link", 0.6
            why = "The name and start date match an existing hire exactly. Confirm it is the same person before linking."
        elif kind == "validation_blocked":
            opt, conf = "recheck", 0.9
            first = (d.get("findings") or [{}])[0].get("message", "the record failed validation")
            why = (f"Nothing was sent to any system. {first}. Fix it in Ashby; the hold releases automatically "
                   f"once the corrected record passes validation.")
        elif kind == "apply_failed":
            opt, conf = "retry_now", 0.6
            why = f"{d.get('target')} rejected the change ({d.get('error')}). Fix the cause in that system, then retry."
        else:
            opt, conf = next(iter(options)), 0.5
            why = "Fix the data at the source system (Ashby), then re-check."
        return Decision(recommended_option=opt if opt in options else None, action="request_approval",
                        confidence=conf, rationale=why, message=why, source="stub")

    def draft_nudge(self, gate: dict, ctx: dict) -> str:
        verb = "is overdue" if gate["status"] == "overdue" else "is due soon"
        return (f"{gate['gate'].replace('_', ' ').capitalize()} for {ctx.get('name')} {verb} "
                f"(due {gate['due_date']}, start date {ctx.get('start_date')}). Owner: {gate['owner']}.")


# --------------------------------------------------------------------------------------
# Live Claude via the Anthropic Messages API with forced tool use.
# --------------------------------------------------------------------------------------
JUDGE_SYSTEM = (
    "You map a free-text job location from the ATS to exactly one Workday site code from location_master. "
    "Remote employees map to the remote code for the state they will work from (their payroll/tax state). "
    "If the label and the home address point to different states or countries, do NOT pick: return site_code "
    "null with low confidence and explain the discrepancy. Respond only by calling propose_location.")

TRIAGE_SYSTEM = (
    "You triage exceptions for BetterUp's new-hire onboarding sync engine, which keeps Ashby, Workday, Okta, "
    "Lumos, the laptop vendor and the cohort tracker consistent. You get one exception, a redacted hire record "
    "and the allowed options. Recommend exactly one allowed option. Principles: protect the hire's first day; "
    "never let a system of record silently diverge; prefer reversible actions; if facts conflict or are missing, "
    "say what a human should verify. You execute nothing. Use action=auto_resolve only when you are certain and "
    "the action is trivially reversible; the engine applies its own policy on top. Respond only by calling "
    "record_decision.")

NUDGE_SYSTEM = (
    "Write a 1-2 sentence Slack reminder for the owning team about an onboarding gate. Be specific (who, what, "
    "due date, start date, consequence of missing it). No greetings, no emoji. Respond only by calling write_message.")

JUDGE_TOOL = {
    "name": "propose_location",
    "description": "Propose the Workday site code for this hire's work location.",
    "input_schema": {"type": "object", "required": ["site_code", "confidence", "rationale"], "properties": {
        "site_code": {"type": ["string", "null"], "description": "A key of location_master, or null if unsure"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"}}},
}
NUDGE_TOOL = {
    "name": "write_message", "description": "The Slack message text.",
    "input_schema": {"type": "object", "required": ["message"], "properties": {"message": {"type": "string"}}},
}


def triage_tool(options: dict[str, str]) -> dict:
    return {
        "name": "record_decision",
        "description": "Record the recommended resolution for this exception.",
        "input_schema": {"type": "object",
                         "required": ["recommended_option", "action", "confidence", "rationale", "message"],
                         "properties": {
                             "recommended_option": {"type": "string", "enum": list(options)},
                             "action": {"type": "string", "enum": ["auto_resolve", "request_approval", "escalate"]},
                             "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                             "rationale": {"type": "string", "description": "Why, citing the specific facts"},
                             "message": {"type": "string", "description":
                                         "2-3 sentences for the owning team: what happened, what you recommend, "
                                         "and what clicking it will do"}}},
    }


class LiveAdvisor:
    name = "live"
    source = "claude"
    def __init__(self, model: str, timeout: float = 20.0):
        import anthropic  # optional dependency
        self.client = anthropic.Anthropic(timeout=timeout, max_retries=2)
        self.model = model

    def _call(self, system: str, payload: dict, tool: dict) -> dict:
        resp = self.client.messages.create(
            model=self.model, max_tokens=1024, system=system, tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": json.dumps(payload, default=str, indent=1)}])
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input)
        raise ValueError("model returned no tool call")

    def judge_location(self, ctx: dict, candidates: dict[str, dict]) -> LocationProposal:
        data = self._call(JUDGE_SYSTEM, {"hire": ctx, "location_master": candidates}, JUDGE_TOOL)
        return LocationProposal.model_validate({**data, "source": self.source})

    def triage(self, exc: dict, ctx: dict, options: dict[str, str]) -> Decision:
        data = self._call(TRIAGE_SYSTEM, {"exception": exc, "context": ctx, "allowed_options": options},
                          triage_tool(options))
        return Decision.model_validate({**data, "source": self.source})

    def draft_nudge(self, gate: dict, ctx: dict) -> str:
        return str(self._call(NUDGE_SYSTEM, {"gate": gate, "hire": ctx}, NUDGE_TOOL)["message"])

class OpenAIAdvisor(LiveAdvisor):
    """Same prompts, tool schemas and guardrails as LiveAdvisor; only the transport changes."""
    name, source = "openai", "openai"

    def __init__(self, model: str, timeout: float = 20.0):
        import openai  # optional dependency; reads OPENAI_API_KEY
        self.client = openai.OpenAI(timeout=timeout, max_retries=2)
        self.model = model

    def _call(self, system: str, payload: dict, tool: dict) -> dict:
        resp = self.client.chat.completions.create(
            model=self.model, max_completion_tokens=4096,
            messages=[{"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, default=str, indent=1)}],
            tools=[{"type": "function", "function": {
                "name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]}}],
            tool_choice={"type": "function", "function": {"name": tool["name"]}})
        calls = resp.choices[0].message.tool_calls or []
        if not calls:
            raise ValueError("model returned no tool call")
        return json.loads(calls[0].function.arguments)
    
def make_advisor(settings: Settings):
    mode = settings.claude_mode
    if settings.llm_provider == "openai":
        if mode == "live" or (mode == "auto" and os.getenv("OPENAI_API_KEY")):
            return OpenAIAdvisor(settings.openai_model)
        return StubAdvisor()
    if mode == "live" or (mode == "auto" and os.getenv("ANTHROPIC_API_KEY")):
        try:
            return LiveAdvisor(settings.claude_model)
        except ImportError:
            if mode == "live":
                raise
    return StubAdvisor()
