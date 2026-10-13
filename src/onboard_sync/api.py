"""HTTP surface (FastAPI). n8n calls /events and /sweep, and Slack calls /slack/actions.

Every inbound write is authenticated against the *raw* body: an HMAC from n8n, or Slack's v0
signature with a replay window. Handlers are async only to read the raw bytes. Engine work
runs in a threadpool, so the event loop is never blocked.
"""
from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .models import EventEnvelope
from .service import Service
from .slack import parse_action, verify_hmac, verify_slack


def create_app(svc: Service | None = None, now=time.time) -> FastAPI:
    svc = svc or Service.from_env()
    app = FastAPI(title="onboard-sync", version="0.1.0")
    app.state.svc = svc

    async def signed_body(request: Request) -> bytes:
        body = await request.body()
        if not verify_hmac(svc.settings.hmac_secret, body, request.headers.get("X-Signature")):
            raise HTTPException(status_code=401, detail="bad signature")
        return body

    @app.post("/events")
    async def events(request: Request):
        body = await signed_body(request)
        try:
            env = EventEnvelope.model_validate_json(body)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return await run_in_threadpool(svc.ingest, env)

    @app.post("/sweep")
    async def sweep(request: Request):
        await signed_body(request)
        return await run_in_threadpool(svc.sweep)

    @app.post("/slack/actions")
    async def slack_actions(request: Request):
        body = await request.body()
        if not verify_slack(svc.settings.slack_signing_secret, request.headers.get("X-Slack-Request-Timestamp"),
                            body, request.headers.get("X-Slack-Signature"), now()):
            raise HTTPException(status_code=401, detail="bad slack signature")
        ex_id, option, user = parse_action(body)
        result = await run_in_threadpool(svc.resolve, ex_id, option, f"slack:{user}")
        return {"response_type": "in_channel", "replace_original": False,
                "text": f"<@{user}> chose *{option}*: {result['status']}"}

    @app.get("/hires")
    def hires():
        return svc.hires()

    @app.get("/hires/{hire_id}")
    def hire(hire_id: str):
        try:
            return svc.hire(hire_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown hire")

    @app.get("/hires/{hire_id}/plan")
    def plan(hire_id: str):
        return svc.diff(hire_id)

    @app.get("/exceptions")
    def exceptions(include_closed: bool = False):
        return svc.exceptions(include_closed)

    @app.get("/audit/verify")
    def audit_verify():
        return svc.verify_audit()

    @app.get("/health")
    def health():
        return {"ok": True, "advisor": getattr(svc.advisor, "name", "unknown")}

    return app
