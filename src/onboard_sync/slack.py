"""Slack Block Kit cards and request signing.

Inbound requests are authenticated two ways:
  n8n   -> /events, /sweep        HMAC-SHA256 of the raw body (X-Signature)
  Slack -> /slack/actions         Slack v0 signing secret, with a 5-minute replay window
Both verify the raw bytes. That's why the n8n workflows build the JSON string once, sign that
string, and send it unmodified.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlencode

from .models import ExceptionItem, HireRecord


def hmac_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_hmac(secret: str, body: bytes, signature: str | None) -> bool:
    return bool(signature) and hmac.compare_digest(hmac_signature(secret, body), signature)


def slack_signature(secret: str, timestamp: str, body: bytes) -> str:
    return "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()


def verify_slack(secret: str, timestamp: str | None, body: bytes, signature: str | None, now_ts: float,
                 tolerance: int = 300) -> bool:
    try:
        ts = int(timestamp or "")
    except ValueError:
        return False
    if abs(now_ts - ts) > tolerance:
        return False
    return bool(signature) and hmac.compare_digest(slack_signature(secret, str(ts), body), signature)


def action_body(exception_id: str, option: str, user_id: str) -> bytes:
    """The form-encoded body Slack POSTs when someone clicks a button (used by demos/tests)."""
    payload = {"type": "block_actions", "user": {"id": user_id},
               "actions": [{"action_id": f"resolve:{option}",
                            "value": json.dumps({"exception_id": exception_id, "option": option})}]}
    return urlencode({"payload": json.dumps(payload)}).encode()


def parse_action(body: bytes) -> tuple[str, str, str]:
    payload = json.loads(parse_qs(body.decode())["payload"][0])
    value = json.loads(payload["actions"][0]["value"])
    return value["exception_id"], value["option"], payload["user"]["id"]


def approval_blocks(item: ExceptionItem, rec: HireRecord, decision) -> list[dict]:
    rec_opt = decision.recommended_option
    buttons = []
    for opt in item.options:
        label = opt.replace("_", " ").capitalize() + (" (recommended)" if opt == rec_opt else "")
        b = {"type": "button", "action_id": f"resolve:{opt}", "text": {"type": "plain_text", "text": label},
             "value": json.dumps({"exception_id": item.id, "option": opt})}
        if opt == rec_opt:
            b["style"] = "primary"
        buttons.append(b)
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"{rec.display_name}: {item.kind.replace('_', ' ')}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{item.summary}*\n{decision.message}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"Recommendation: *{rec_opt or 'none'}* ({decision.source}, confidence {decision.confidence:.2f}) · "
            f"hire `{rec.hire_id}` · exception `{item.id}`"}]},
        {"type": "actions", "elements": buttons},
    ]


def nudge_blocks(item: ExceptionItem, text: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": f"resolve:{opt}", "text": {"type": "plain_text", "text": opt.replace('_', ' ')},
             "value": json.dumps({"exception_id": item.id, "option": opt})} for opt in item.options]},
    ]
