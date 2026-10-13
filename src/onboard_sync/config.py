"""Configuration.

Reference data (locations, bundles, holidays) and tunables (thresholds, retry policy,
secrets) live here so People Ops / IT can change policy without touching engine code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date

# --- Reference data ---------------------------------------------------------------

EMAIL_DOMAIN = os.getenv("ONBOARD_SYNC_EMAIL_DOMAIN", "betterup.com")

# Order matters: Okta needs a Workday worker (identity must trace to an HR record);
# Lumos needs the Okta user. Equipment and the tracker depend on nothing, so a
# failing Okta never hides a hire from the tracker.
TARGET_ORDER = ["workday", "okta", "lumos", "equipment", "tracker"]

# Projections nobody treats as a system of record: the engine may write these
# without a human click (including during deprovisioning).
SAFE_TARGETS = frozenset({"tracker"})

# Workday location master (site code -> attributes).
LOCATION_CODES: dict[str, dict] = {
    "US-CA-SF": {"label": "San Francisco HQ", "country": "US", "region": "CA", "remote": False},
    "US-CA-REMOTE": {"label": "Remote - California", "country": "US", "region": "CA", "remote": True},
    "US-NY-REMOTE": {"label": "Remote - New York", "country": "US", "region": "NY", "remote": True},
    "US-TX-REMOTE": {"label": "Remote - Texas", "country": "US", "region": "TX", "remote": True},
    "US-OR-REMOTE": {"label": "Remote - Oregon", "country": "US", "region": "OR", "remote": True},
    "US-WA-REMOTE": {"label": "Remote - Washington", "country": "US", "region": "WA", "remote": True},
    "GB-LDN": {"label": "London", "country": "GB", "region": None, "remote": False},
    "GB-REMOTE": {"label": "Remote - UK", "country": "GB", "region": None, "remote": True},
}

# Deterministic aliases checked before Claude is ever asked (casefolded keys).
LOCATION_ALIASES: dict[str, str] = {
    "san francisco": "US-CA-SF", "san francisco, ca": "US-CA-SF", "sf hq": "US-CA-SF",
    "remote - california": "US-CA-REMOTE", "remote (ca)": "US-CA-REMOTE",
    "remote - new york": "US-NY-REMOTE", "remote - texas": "US-TX-REMOTE",
    "remote - oregon": "US-OR-REMOTE", "remote - washington": "US-WA-REMOTE",
    "london": "GB-LDN", "remote - uk": "GB-REMOTE",
}

LUMOS_BUNDLES = {
    "Customer Success": "bundle-cs-core",
    "Engineering": "bundle-eng-core",
    "Sales": "bundle-sales-core",
    "People": "bundle-people-core",
    "Clinical": "bundle-clinical-core",
}
DEFAULT_BUNDLE = "bundle-base"

EQUIPMENT_PROFILES = {"Engineering": "mbp-16-eng"}
DEFAULT_EQUIPMENT_PROFILE = "mbp-14-std"

SLACK_CHANNELS = {
    "People Ops": "#people-ops-onboarding",
    "IT": "#it-onboarding",
    "Recruiting": "#recruiting-coordination",
    "Leads": "#people-ops-leads",
}

# US company holidays (configurable; Thanksgiving + day after, observed July 4th).
HOLIDAYS: frozenset[date] = frozenset({
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
    date(2026, 11, 27), date(2026, 12, 25), date(2027, 1, 1),
})

# Gate -> (business days before start, owning team). "Often 7 days out" per the brief.
GATE_RULES: dict[str, tuple[int, str]] = {
    "workday_hire_complete": (7, "People Ops"),
    "hardware_form_submitted": (7, "IT"),
    "background_check_clear": (5, "People Ops"),
    "equipment_shipped": (4, "IT"),
    "okta_provisioned": (2, "IT"),
}


# --- Tunables -----------------------------------------------------------------------

@dataclass
class Settings:
    email_domain: str = EMAIL_DOMAIN
    cohort_weekday: int = 0                 # Monday starts
    min_lead_business_days: int = 10
    gate_warn_business_days: int = 2        # "at risk" window before a gate's due date

    # In-request retries for transient target errors; beyond this the hire is parked
    # in the retry table and the sweep re-runs the *reconciliation* (never a stale op).
    retry_max_attempts: int = 4
    retry_base_seconds: float = 0.5
    retry_park_minutes: int = 30
    retry_escalate_after: int = 3           # parked passes before a human is paged

    # Claude
    claude_mode: str = field(default_factory=lambda: os.getenv("ONBOARD_SYNC_CLAUDE", "auto"))  # auto|live|stub
    claude_model: str = field(default_factory=lambda: os.getenv("ONBOARD_SYNC_CLAUDE_MODEL", "claude-sonnet-5-5"))
    llm_provider: str = field(default_factory=lambda: os.getenv("ONBOARD_SYNC_LLM", "claude"))  # claude|openai
    openai_model: str = field(default_factory=lambda: os.getenv("ONBOARD_SYNC_OPENAI_MODEL", "gpt-6-luna"))
    judge_min_confidence: float = 0.85
    # (exception kind, option) pairs Claude may resolve without a human. Empty by
    # default: autonomy is earned per decision type once its precision is measured.
    auto_resolve: frozenset[tuple[str, str]] = frozenset()
    auto_resolve_min_confidence: float = 0.9

    # Secrets (dev defaults; production reads these from the secret store)
    hmac_secret: str = field(default_factory=lambda: os.getenv("ONBOARD_SYNC_HMAC_SECRET", "dev-hmac-secret"))
    slack_signing_secret: str = field(default_factory=lambda: os.getenv("SLACK_SIGNING_SECRET", "dev-slack-secret"))
