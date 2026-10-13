# onboard-sync

This is a reconciliation engine that keeps a new hire consistent across **Ashby, Workday, Okta/Lumos, the laptop vendor (ExpoIT) and the cohort tracker**. It is BetterUp's AI Automation Engineer take-home.

| Deliverable | Where |
|---|---|
| 1. Systems map | [`docs/01_systems_map.md`](docs/01_systems_map.md) |
| 2. Working prototype | this repo: `src/onboard_sync/`, 11 scenarios, 43 tests |
| 3. Build note | [`docs/03_build_note.md`](docs/03_build_note.md), with the raw [`docs/ai_log.md`](docs/ai_log.md) |

**Slice:** change propagation. Cross-system validation is its admission gate, and exception monitoring is a view over its ledger.

## Run it (3 commands)

```bash
pip install -e ".[all]"          # Python 3.11+. Core needs only pydantic.
onboard-sync replay all          # 11 scenarios on sample data, narrated
pytest                           # 43 tests: every scenario asserted, plus engine, auth, audit, MCP
```

No credentials are needed. The targets are in-memory fakes that misbehave like the real APIs: Okta returns 409 on a duplicate login, ExpoIT refuses edits once an order ships, Sheets rate-limits, and calls can be made to fail N times or to fail *after committing*.

Claude runs through a deterministic stub by default. To use the real API:

```bash
ANTHROPIC_API_KEY=... onboard-sync replay S08 --live-claude
or 
OPENAI_API_KEY=... onboard-sync replay S08 --live-claude
```

The model is set by `ONBOARD_SYNC_CLAUDE_MODEL` and defaults to `claude-sonnet-5-5`.

## How it works

```
n8n (triggers only)                 onboard-sync (Python)
 Ashby webhook ─┐   HMAC-signed      ingest ─ dedupe event_id ─ normalize ─ match identity
 Workday poll  ─┼──► POST /events ─► merge  ─ field ownership, per-source change detection
 Hardware form ─┘                    validate ─ 8 rules → Claude location judge → deterministic guard
 Sweep cron ──────► POST /sweep      save canonical HireRecord (version CAS, hash-chained audit)
                                     plan ─ 3-way diff per target (desired / observed / last-written)
 Slack buttons ───► /slack/actions   execute ─ lookup-before-create, adopt-on-409, retries, park
                                     monitor ─ gates from start date, exceptions, Claude triage → Slack
 Claude Desktop/Code ◄── MCP ───────  operator console (8 tools)
```

## Scenarios

`onboard-sync replay <id>` persists the result to `./var/`. Inspect it with `hires`, `show <name>`, `exceptions --all`, `audit <name>` or `audit --verify`.

| ID | Story | What it proves |
|---|---|---|
| S01 | Offer accepted, form, BGC, laptop ships, then every webhook is replayed twice | End-to-end across 5 systems. The **replay storm makes zero writes.** |
| S02 | Start date +2 weeks after the laptop is ordered | Minimal updates everywhere. The laptop is **rescheduled, not re-ordered.** Gate deadlines move. |
| S03 | "Jhon" fixed to "John" after Okta was provisioned | The login is immutable. Claude recommends a rename, IT approves in Slack, and the old login is kept as an alias. |
| S04 | Off-cycle hire created directly in Workday | Multi-source ingest. Workday-owned, so the engine never pushes to it. The login collides with a former employee and gets a suffix. A missing BGC is flagged. |
| S05 | Address change, then another after the laptop shipped | A one-field diff becomes 2 writes. After shipping, the engine raises an advisory instead of a doomed PATCH, and the human acknowledgement sticks. |
| S06 | Okta 500s, Sheets 429, a lost response, a hard outage | Retries, adopt-on-timeout, park and sweep. **Exactly one** Okta user per hire. |
| S07 | Newer webhook before older, then a duplicate | Per-field ordering, a stale drop, and dedupe. |
| S08 | People Ops edits the hire date directly in Workday | The three-way diff raises a **conflict, never an overwrite**. Claude triages, then Slack approves, then the change propagates. |
| S09 | "Remote - Bay Area" location (twice) | Claude proposes and a deterministic guard decides. A bad record is **held with zero writes**, and the hold releases itself once the data is fixed. |
| S10 | Candidate withdraws | Deprovisioning waits for a human. The tracker updates immediately. |
| S11 | Hardware form late, snooze, overdue, then the start date moves | Gate reminders via Claude-drafted nudges. Deadlines move and the reminders clear themselves. |

## Five-minute demo (Loom script)

1. `onboard-sync replay S01`: one offer becomes 5 systems, then the replay storm makes `target writes during replay: 0`.
2. `onboard-sync replay S02`: the plan line shows only the deltas, `equipment:UPDATE(deliverBy)`, and the gate due dates move.
3. `onboard-sync replay S06`: `okta:create->adopted ... after 2 attempts` (lost response), then parked, then converged by the sweep.
4. `onboard-sync replay S08`: a conflict card with Claude's reasoning, the click, and the propagation. Then `onboard-sync audit Priya` and `onboard-sync audit --verify`.
5. In Claude Desktop with the MCP server connected: ask *"Why did Priya's start date change, and who approved it?"*

## Interfaces

**HTTP:** `onboard-sync serve`. Endpoints:
- `POST /events` and `POST /sweep`: HMAC-SHA256 of the raw body in `X-Signature`
- `POST /slack/actions`: Slack v0 signature with a 5-minute window
- `GET /hires`, `/hires/{id}`, `/hires/{id}/plan`, `/exceptions`, `/audit/verify`, `/health`

**MCP:** add this to the Claude Desktop config:

```json
{"mcpServers": {"onboard-sync": {"command": "onboard-sync", "args": ["mcp"],
  "env": {"ONBOARD_SYNC_HOME": "/absolute/path/to/onboard-sync/var"}}}}
```

The tools are `list_hires`, `get_hire`, `diff_hire`, `list_exceptions`, `explain_exception`, `resolve_exception` (write; recorded as `mcp:<operator>`), `audit_trail` and `run_sweep`.

**n8n:** import `n8n/*.json` and set these environment variables:
- `ONBOARD_SYNC_URL`
- `ONBOARD_SYNC_HMAC_SECRET`
- `WORKDAY_RAAS_URL` (plus an OAuth2 credential)

The workflows hold no business logic. They serialize once, sign, and POST the raw body. The JSON was written by hand, so check node versions when you import it.

## Repo map

```
src/onboard_sync/
  models.py       HireRecord (canonical), EventEnvelope, PlanOp, ExceptionItem
  ownership.py    who owns which field, and the merge step (ordering, repeats, drift)
  normalize.py    Ashby / Workday / form payloads -> canonical fields
  matching.py     identity resolution: ref -> hire_id -> email -> possible duplicate
  validation.py   admission gate + Claude location judge + deterministic guard
  reconcile.py    Planner (3-way diff) and Executor (idempotent, retries, adopt, park)
  targets.py      Workday, Okta, Lumos, ExpoIT, tracker adapters
  monitoring.py   exception desk (dedupe, triage, Slack) and gate monitor
  engine.py       ingest -> merge -> validate -> reconcile -> monitor; resolutions; sweep
  claude.py       advisor interface: live (Anthropic forced tool use) + deterministic stub; guardrails
  store.py        SQLite: records, identity map, applied state, retries, gates, exceptions, audit chain
  api.py / cli.py / mcp_server.py / slack.py / scenarios.py / mocks.py
sample_data/      Ashby, Workday and hardware-form payloads
n8n/              4 trigger workflows
tests/            scenarios + engine + validation/Claude + API/audit/MCP
```

## Assumptions

These would be confirmed in the debrief:
- **A1.** Okta is the IdP and Lumos handles access bundles on top of it.
- **A2.** ExpoIT is the equipment vendor that takes orders from the IT hardware form. Its interface is abstracted behind an adapter.
- **A3.** The cohort tracker is a Google Sheet, and background-check status reports into Ashby.
- **A4.** If Okta is already HR-driven from Workday, the Okta adapter switches to verify-only. The diff loop stays the same; writes are disabled.
- **A5.** Volume is tens of hires a week, so a serial engine plus an hourly sweep is enough.
- **A6.** Existing Zapier zaps are retired into these n8n triggers.
