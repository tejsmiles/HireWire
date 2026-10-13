# Deliverable 3: Build note

## Why this slice

I chose **change propagation**, built as a reconciliation engine. The three symptoms in the brief share one root cause: there is no canonical hire record and nothing ever converges on one. Validation and exception monitoring are *components* of that engine, not alternatives to it:

- **Validation** is the admission gate in front of every write.
- **Monitoring** is a view over the gates and the exception ledger.

So this slice delivers all three, kept thin, on one spine. It is also the only slice where low-code is the wrong tool. Ordering, idempotency, three-way conflicts and partial failure are what this role exists to handle. See the scoring table in `01_systems_map.md`.

## Key design decisions

**1. Canonical record plus reconciliation, not event chaining.** An event only updates the desired state (`HireRecord`). The reconciler then diffs desired against observed state for each target and applies the difference. One code path handles late webhooks, retries, off-cycle hires and repeated edits. It is level-triggered rather than edge-triggered: if the process crashes between ingest and push, the next sweep finishes the job (`test_crash_between_ingest_and_reconcile_is_healed_by_the_sweep`).

**2. Field ownership by lifecycle phase, with per-source change detection.** Ashby owns HR fields until day 1, then Workday does. Off-cycle hires are Workday-owned from the start. Okta owns the login once it is created. The IT form owns the ship-to address. A source "changed" a field only if its value differs from what *that same source* last said, so full-snapshot webhooks can't clobber a newer decision. Ordering is per field, and ties go to the first message seen.

**3. Three-way diff.** For each target the planner compares:
- `desired`: the projection of the canonical record
- `observed`: what the target holds now
- `base`: what *we* last wrote

| Situation | What the planner does |
|---|---|
| `observed == base` (only the canonical record moved) | Safe update |
| `observed != base` (someone edited the target directly) | Conflict. Never silently overwritten. |
| The target owns the field | Never pushed to it |

Workday is the only conflict-detecting target, because it is the only one people edit as a system of record. The others are projections.

**4. Idempotency in three layers.**

| Layer | Mechanism | Covered by |
|---|---|---|
| Ingest | `event_id` dedupe | S07 |
| State | Version compare-and-set, plus per-field ordering and repeat detection | S01, S07 |
| Writes | Lookup by natural key before every create; adopt on 409; idempotency keys | S06 |

The idempotency keys work like this:
- **Creates:** one key per (hire, target) forever, so a lost response can never create a second laptop order.
- **Updates:** keys are scoped to the record version, so a later correction is never deduped away.

The proof is S01: replaying every webhook twice causes **zero** target writes.

**5. Failure handling.**
- Transient errors (429, 5xx, timeouts) retry with exponential backoff and jitter.
- When retries run out, the target is **parked**, and the sweep re-runs the *reconciliation*, never the stale operation.
- 4xx errors fail fast into an exception.
- A target that failed in this run is not retried again until the next run.
- Dependent targets wait: Lumos waits for Okta, and Okta waits for a Workday worker, so every identity traces to an HR record.
- The tracker depends on nothing, so a hire never disappears from view while Okta is down.
- A shipped laptop can't be edited, so instead of a doomed PATCH the engine raises an advisory for IT.
- Deprovisioning always waits for a human click.

**6. Claude works at the edges, never in the control plane.**

| Job | Where | How it's constrained |
|---|---|---|
| Map a free-text location to the Workday location master | Validation | A deterministic guard must agree: country and payroll state have to match the home address |
| Triage exceptions | Exception desk | Recommends one *allowed* option and writes the Slack explanation |
| Draft gate reminders | Gate monitor | Plain text for the owning team |

Every call is a forced tool call with a pydantic-validated schema, and prompts carry redacted PII. If Claude errors, is unsure, or proposes an option that isn't allowed, the result falls back to "ask a human". Claude cannot give itself autonomy: `auto_resolve` only applies to (exception kind, option) pairs that policy whitelists, which is **empty by default**. Autonomy is earned per decision type once its override rate has been measured. The recommended-vs-chosen option is in the audit log for exactly that purpose.

**7. n8n is the trigger layer, and the logic is code.** Webhooks, polling with overlapping windows, cron and retries live in n8n, which BetterUp already runs. All the logic lives in tested Python. Every body is serialized once, HMAC-signed, and sent byte-for-byte. Slack interactivity goes straight to the service so the Slack signature can be verified over the raw body.

## Bonus items

- **MCP tool.** `mcp_server.py` exposes an operator console with 8 tools, usable in Claude Desktop, Claude Code or n8n's AI Agent node. Only `resolve_exception` and `run_sweep` write.
- **Authentication.** The prototype uses HMAC for n8n→service and Slack's v0 signing secret with a 5-minute replay window. In production:
  - n8n→service: Okta OAuth2 client credentials.
  - Target APIs: least-privilege OAuth service accounts.
  - MCP over HTTP: Okta OIDC (auth code + PKCE), with an `onboarding:resolve` scope required for write tools. The operator's identity flows into the audit log.
- **Audit trail.** An append-only, SHA-256 hash-chained ledger with actors `source:*`, `engine`, `claude`, `slack:<user>` and `mcp:<user>`. `onboard-sync audit --verify` detects tampering (tested).

## What I'd productionize

1. **Postgres.** Same schema, with a per-hire advisory lock instead of the process lock, and a small worker for parked retries.
2. **Real adapters.**
   - Workday: RaaS read, plus the Hire/Change/Rescind business processes. These are asynchronous in reality: poll the BP status and treat it as in flight.
   - Okta: switch to verify-only mode if HR-driven provisioning is already on.
   - Lumos and ExpoIT: whatever interface ExpoIT actually exposes.
   - Google Sheets.
3. **Secrets and auth.** Secrets in a vault, and Okta OAuth on every HTTP and MCP surface.
4. **Metrics.** Time-to-converge, drift rate, exception age, and Claude's override rate per decision type. That last one drives earned autonomy.
5. **Backfill.** Point the engine at in-flight hires. Lookup-before-create adopts their existing records rather than duplicating them.
6. **Rehire detection.** Today a login collision with a deprovisioned user just gets a suffix (S04). That spot is a good candidate for a guarded Claude judge.
7. **More targets and correctness work.** Equipment conflict detection, Highspot enrolment, and business-day math in the hire's timezone. The prototype uses UTC dates and a fixed US holiday list.

**Known limitations, stated plainly:**
- The targets are fakes.
- The n8n JSON was written by hand and has not been imported into a live n8n.
- The live Claude path was verified against a mocked Anthropic client and the deterministic stub, not the real API. The build environment had no API key. Run it with `--live-claude`.

## Tools I chose not to add

No Temporal or Airflow, no Workato, no Merge or Finch, and no queue. n8n plus a retry table plus the sweep cover this volume. The one ask is a Postgres schema next to n8n's database.

## AI tools used, and one thing the AI got wrong

**Tools:** Claude (design, code, tests and docs, working agentically in a repo session). At runtime, the Claude API through the advisor interface, and MCP.

**What it got wrong.** The merge step originally let the *later-arriving* message win when two messages from the same source had the *same timestamp*. During the S01 replay storm, an old snapshot with the same timestamp flipped `bgc_status` from `clear` back to `in_progress`, and the next replay flipped it forward again.

No target write happened, so a write-count assertion alone would never have noticed. I caught it because the replay output said `accepted` where it should have said `no_change`.

The fix: ties go to the first message seen, and repeats are never edits. That is pinned by a unit test. It was one of five real misses; all five are logged in `ai_log.md`, including the design outline's own ownership rule, which would have blocked start-date propagation entirely.
