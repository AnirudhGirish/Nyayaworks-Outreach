"""Central configuration for the NyayaWorks outreach pipeline.

All secrets are read from environment variables (set in Railway, never committed).
This module also defines the email template, verified product facts, and the
hard-coded spam-phrase list used by the guardrail engine.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
# The service-account JSON may be provided either as a file path
# (GOOGLE_CREDENTIALS_PATH) or inline as a base64/JSON string
# (GOOGLE_SERVICE_ACCOUNT_JSON). Railway recommends the inline secret.
GOOGLE_CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
SHEET_ID = os.environ.get("SHEET_ID", "")

LEADS_TAB = os.environ.get("LEADS_TAB", "leads")
CONTROL_TAB = os.environ.get("CONTROL_TAB", "control")

# ---------------------------------------------------------------------------
# Resend API & IMAP (Titan for inbound reply polling — SSL only)
# ---------------------------------------------------------------------------
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.secureserver.net")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
EMAIL_USER = os.environ.get("EMAIL_USER", "founder@nyayaworks.in")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
FROM_ADDRESS = os.environ.get("FROM_ADDRESS", EMAIL_USER)
FROM_NAME = os.environ.get("FROM_NAME", "NyayaOS")

# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Do not hardcode a specific model. Read from env, with a safe default that
# is resolved against the live model list at runtime (see research.py/draft.py).
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
ANTHROPIC_MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "1024"))

# ---------------------------------------------------------------------------
# Network timeouts (seconds) — configurable via env, with strict defaults
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))
IMAP_TIMEOUT = int(os.environ.get("IMAP_TIMEOUT", "30"))
ANTHROPIC_TIMEOUT = int(os.environ.get("ANTHROPIC_TIMEOUT", "60"))
SHEETS_TIMEOUT = int(os.environ.get("SHEETS_TIMEOUT", "30"))

# ---------------------------------------------------------------------------
# Concurrency / lock
# ---------------------------------------------------------------------------
LOCK_TTL_MINUTES = int(os.environ.get("LOCK_TTL_MINUTES", "15"))

# ---------------------------------------------------------------------------
# State machine tuning
# ---------------------------------------------------------------------------
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))

# ---------------------------------------------------------------------------
# Email template (fixed structure, {{slots}} are the only personalization points)
# ---------------------------------------------------------------------------
# product_facts is substituted into {{product_facts}} at draft time.
EMAIL_TEMPLATE = """Subject: {{subject}}

Hi {{name}},

{{intro}}

NyayaOS is an AI legal workspace built for Indian law firms. {{product_facts}}

{{closing}}

{{unsubscribe}}
"""

# The fixed, non-negotiable unsubscribe block appended to every email.
UNSUBSCRIBE_BLOCK = (
    "If you'd rather not receive these emails, unsubscribe here: "
    "https://nyayaworks.in/unsubscribe?email={{email}}"
)

# Verified facts about NyayaOS. The drafting AI may only use these plus facts
# it actually found during research. Never edited by the AI.
PRODUCT_FACTS = (
    "It drafts, reviews, and summarizes contracts in plain language, keeps your "
    "matters searchable in one place, and runs on infrastructure built for "
    "confidential client data."
)

# ---------------------------------------------------------------------------
# Spam-phrase blocklist (deterministic guardrail, §7)
# ---------------------------------------------------------------------------
BANNED_PHRASES = [
    "100% free",
    "act now",
    "buy now",
    "call now",
    "click here",
    "congratulations",
    "earn money",
    "free money",
    "guarantee",
    "limited time",
    "make money fast",
    "no obligation",
    "offer expires",
    "order now",
    "risk free",
    "special promotion",
    "urgent",
    "winner",
    "you have won",
    "best price",
    "cash bonus",
    "double your",
    "income",
    "investment opportunity",
    "no cost",
    "prize",
    "promo",
    "sale",
    "cheap",
    "discount",
    "!!!",
]

# ---------------------------------------------------------------------------
# Body word-count cap (§6b / §7)
# ---------------------------------------------------------------------------
BODY_WORD_CAP = int(os.environ.get("BODY_WORD_CAP", "120"))

# ---------------------------------------------------------------------------
# Canonical sheet column order (§4)
# ---------------------------------------------------------------------------
LEADS_COLUMNS = [
    "row_id",
    "name",
    "firm_name",
    "type",
    "website",
    "email",
    "source",
    "state",
    "research_json",
    "draft_subject",
    "draft_body",
    "provider_message_id",
    "sent_at",
    "last_updated_at",
    "attempts",
    "error_log",
    "do_not_contact",
]

CONTROL_COLUMNS = [
    "daily_cap",
    "sent_today",
    "date_reset_at",
    "send_window_start",
    "send_window_end",
    "is_locked",
    "last_error",
]

# State machine constants
STATE_NEW = "NEW"
STATE_RESEARCHED = "RESEARCHED"
STATE_DRAFTED = "DRAFTED"
STATE_QUEUED = "QUEUED"
STATE_SENT = "SENT"
STATE_REPLIED = "REPLIED"
STATE_BOUNCED = "BOUNCED"
STATE_FAILED = "FAILED"
STATE_SUPPRESSED = "SUPPRESSED"

# Linear forward progression (one step per run).
STATE_PROGRESSION = {
    STATE_NEW: STATE_RESEARCHED,
    STATE_RESEARCHED: STATE_DRAFTED,
    STATE_DRAFTED: STATE_QUEUED,
    STATE_QUEUED: STATE_SENT,
}

TERMINAL_STATES = {STATE_REPLIED, STATE_BOUNCED, STATE_FAILED, STATE_SUPPRESSED}
ACTIONABLE_STATES = list(STATE_PROGRESSION.keys())
