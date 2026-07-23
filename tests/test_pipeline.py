"""Pytest suite for the NyayaWorks pipeline (§12).

Covers: guardrails, Resend status polling, HTML template rendering & XSS escaping,
state-machine transitions, research/draft JSON parsing, and full integration runs.
No network required (mocked API backends).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import config
import guardrails
import research
import template
from providers import SendProvider
from run import (
    _acquire_lock,
    _preflight_credentials,
    _release_lock,
    _window_open,
    _maybe_reset_daily_cap,
    main,
)
from sheets import SheetsClient
from state_machine import (
    enforce_suppression,
    get_next_lead,
    sync_status,
    transition,
)

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def make_lead(state="NEW", **overrides):
    lead = {
        "row_id": "lead-1",
        "name": "Asha Rao",
        "firm_name": "Rao & Co",
        "type": "law_firm",
        "website": "https://rao.example",
        "email": "asha@rao.example",
        "source": "manual",
        "state": state,
        "research_json": "",
        "draft_subject": "",
        "draft_body": "",
        "provider_message_id": "",
        "sent_at": "",
        "last_updated_at": "",
        "attempts": 0,
        "error_log": "",
        "do_not_contact": False,
    }
    lead.update(overrides)
    return lead


RESEARCH_HIGH = {
    "firm_name": "Rao & Co",
    "practice_areas": ["corporate", "M&A"],
    "location": "Bangalore",
    "notable_fact": "handles corporate M&A for mid-market clients in Bangalore",
    "confidence": "high",
}

RESEARCH_LOW = {
    "firm_name": "Rao & Co",
    "practice_areas": ["general"],
    "location": None,
    "notable_fact": None,
    "confidence": "low",
}


# ---------------------------------------------------------------------------
# In-memory backends for integration testing
# ---------------------------------------------------------------------------
class MemSheets(SheetsClient):
    def __init__(self, leads=None, control=None):
        self._leads = leads or []
        self._control = control or {
            "daily_cap": "5",
            "sent_today": "0",
            "date_reset_at": "",
            "send_window_start": "9",
            "send_window_end": "18",
            "is_locked": "",
            "last_error": "",
        }

    def get_leads(self):
        return [dict(lead) for lead in self._leads]

    def batch_update_leads(self, rows):
        by_id = {lead["row_id"]: lead for lead in self._leads}
        for r in rows:
            if r["row_id"] in by_id:
                by_id[r["row_id"]].update(r)

    def get_control(self):
        return dict(self._control)

    def setup_sheet(self):
        return None

    def set_control(self, updates):
        self._control.update(updates)


class FakeProvider(SendProvider):
    def __init__(self):
        self.sent = []
        self.statuses = {}  # email_id -> last_event string
        self.inbox = []

    def send(self, to_address, subject, html_body, text_body, row_id="", attempts=1):
        eid = f"re_fake_{len(self.sent)+1}"
        self.sent.append({
            "to": to_address,
            "subject": subject,
            "html": html_body,
            "text": text_body,
            "row_id": row_id,
            "attempts": attempts,
            "id": eid,
        })
        return eid

    def get_email_status(self, email_id):
        return self.statuses.get(email_id, "delivered")

    def fetch_unread(self):
        msgs = self.inbox
        self.inbox = []
        return msgs


# ---------------------------------------------------------------------------
# Guardrails (§7)
# ---------------------------------------------------------------------------
def test_guardrail_passes_clean_draft():
    lead = make_lead(state="DRAFTED")
    body = config.EMAIL_TEMPLATE.replace("{{subject}}", "") \
        .replace("{{name}}", "Asha") \
        .replace("{{intro}}", "I'm reaching out about NyayaOS.") \
        .replace("{{product_facts}}", config.PRODUCT_FACTS) \
        .replace("{{closing}}", "Worth a look?") \
        .replace("{{unsubscribe}}",
                 config.UNSUBSCRIBE_BLOCK.replace("{{email}}", lead["email"]))
    report = guardrails.validate_draft(lead, "Quick note", body, RESEARCH_LOW)
    assert report.passed


def test_guardrail_rejects_do_not_contact():
    lead = make_lead(do_not_contact=True)
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_draft(lead, "s", "body", RESEARCH_LOW)


def test_guardrail_rejects_word_over_cap():
    lead = make_lead()
    long_body = "word " * (config.BODY_WORD_CAP + 5)
    long_body = long_body.strip() + "\n\n" + config.UNSUBSCRIBE_BLOCK.replace("{{email}}", lead["email"])
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_draft(lead, "s", long_body, RESEARCH_LOW)


def test_guardrail_word_count_excludes_unsubscribe_block():
    lead = make_lead()
    content = " ".join(["draft"] * 115)
    body = content + "\n\n" + config.UNSUBSCRIBE_BLOCK.replace("{{email}}", lead["email"])
    assert guardrails._count_words(body) >= config.BODY_WORD_CAP
    report = guardrails.validate_draft(lead, "Subject", body, RESEARCH_LOW)
    assert report.passed


def test_guardrail_rejects_banned_phrase():
    lead = make_lead()
    body = "Act now on this limited time offer. " + \
           config.UNSUBSCRIBE_BLOCK.replace("{{email}}", lead["email"])
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_draft(lead, "s", body, RESEARCH_LOW)


def test_guardrail_rejects_missing_unsubscribe():
    lead = make_lead()
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_draft(lead, "s", "this body has no link at all", RESEARCH_LOW)


def test_guardrail_rejects_notable_fact_when_low_confidence():
    lead = make_lead()
    low_with_fact = dict(RESEARCH_LOW, notable_fact=RESEARCH_HIGH["notable_fact"])
    fact = low_with_fact["notable_fact"]
    body = f"We saw that you {fact}. " + \
           config.UNSUBSCRIBE_BLOCK.replace("{{email}}", lead["email"])
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_draft(lead, "s", body, low_with_fact)


# ---------------------------------------------------------------------------
# HTML Template & XSS Escaping Tests
# ---------------------------------------------------------------------------
def test_html_escaping_xss():
    """Adversarial XSS input in AI generated text must render as inert escaped text."""
    bad_subject = "Hello <script>alert('xss_subject')</script>"
    bad_body = "We noticed <img src=x onerror=alert('xss_body')> in your practice."
    bad_email = "test+xss@example.com"

    html_out, text_out = template.render(bad_subject, bad_body, bad_email, recipient_name="<b style='color:red'>Hacker</b>")

    # Verify raw script/img tags do NOT appear in the rendered HTML
    assert "<script>" not in html_out
    assert "<img src=x" not in html_out
    assert "<b style=" not in html_out

    # Verify escaped equivalents are present
    assert "&lt;script&gt;alert(&#x27;xss_subject&#x27;)&lt;/script&gt;" in html_out
    assert "&lt;img src=x onerror=alert(&#x27;xss_body&#x27;)&gt;" in html_out


def test_plaintext_fallback_generated():
    html_out, text_out = template.render("Subject Line", "Body content paragraph.", "user@firm.com")
    assert text_out
    assert "Body content paragraph." in text_out
    assert "https://nyayaworks.in/unsubscribe?email=user%40firm.com" in text_out


def test_idempotency_key_passed():
    """Verify that idempotency key parameters (row_id and attempts) are passed on send."""
    lead = make_lead(state="QUEUED", draft_subject="Sub", draft_body="Body text nyayaworks.in/unsubscribe", attempts=2)
    provider = FakeProvider()
    out = transition(lead, provider, model="m")
    assert out["state"] == "SENT"
    sent_msg = provider.sent[0]
    assert sent_msg["row_id"] == "lead-1"
    assert sent_msg["attempts"] == 3  # attempts incremented to 3 in transition


# ---------------------------------------------------------------------------
# Resend Status Polling Tests (sync_status)
# ---------------------------------------------------------------------------
def test_resend_last_event_delivered():
    lead = make_lead(state="SENT", provider_message_id="re_123")
    provider = FakeProvider()
    provider.statuses["re_123"] = "delivered"
    changed = sync_status([lead], provider)
    assert changed == []  # No state change needed for delivered


def test_resend_last_event_bounced():
    lead = make_lead(state="SENT", provider_message_id="re_123")
    provider = FakeProvider()
    provider.statuses["re_123"] = "bounced"
    changed = sync_status([lead], provider)
    assert len(changed) == 1
    assert changed[0]["state"] == "BOUNCED"


def test_resend_last_event_complained():
    lead = make_lead(state="SENT", provider_message_id="re_123", do_not_contact=False)
    provider = FakeProvider()
    provider.statuses["re_123"] = "complained"
    changed = sync_status([lead], provider)
    assert len(changed) == 1
    assert changed[0]["state"] == "BOUNCED"
    assert changed[0]["do_not_contact"] is True


def test_resend_last_event_delivery_delayed():
    lead = make_lead(state="SENT", provider_message_id="re_123")
    provider = FakeProvider()
    provider.statuses["re_123"] = "delivery_delayed"
    changed = sync_status([lead], provider)
    assert changed == []  # Leaves state as SENT


def test_resend_last_event_suppressed():
    lead = make_lead(state="SENT", provider_message_id="re_123")
    provider = FakeProvider()
    provider.statuses["re_123"] = "suppressed"
    changed = sync_status([lead], provider)
    assert len(changed) == 1
    assert changed[0]["state"] == "BOUNCED"


def test_resend_last_event_unknown():
    lead = make_lead(state="SENT", provider_message_id="re_123")
    provider = FakeProvider()
    provider.statuses["re_123"] = "future_unexpected_event"
    changed = sync_status([lead], provider)
    assert len(changed) == 1
    assert changed[0]["state"] == "SENT"
    assert "Unrecognized Resend event: future_unexpected_event" in changed[0]["error_log"]


def test_reply_detection_via_imap():
    lead = make_lead(state="SENT", email="asha@rao.example")
    provider = FakeProvider()
    provider.inbox = [{"from": "Asha Rao <asha@rao.example>", "subject": "Re: NyayaOS"}]
    changed = sync_status([lead], provider)
    assert len(changed) == 1
    assert changed[0]["state"] == "REPLIED"


# ---------------------------------------------------------------------------
# Research / draft JSON parsing
# ---------------------------------------------------------------------------
def test_research_validates_high_confidence():
    obj = research._validate_research(RESEARCH_HIGH)
    assert obj["confidence"] == "high"


def test_research_rejects_bad_confidence():
    bad = dict(RESEARCH_HIGH, confidence="maybe")
    with pytest.raises(ValueError):
        research._validate_research(bad)


def test_research_rejects_missing_keys():
    with pytest.raises(ValueError):
        research._validate_research({"firm_name": "x"})


def test_extract_json_handles_fenced():
    text = '```json\n{"subject": "Hi", "body": "Hello"}\n```'
    assert research._extract_json(text) == {"subject": "Hi", "body": "Hello"}


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError):
        research._extract_json("not json at all")


def test_resolve_model_raises_on_api_failure():
    class FailingClient:
        class models:
            @staticmethod
            def list():
                raise ConnectionError("network down")

    with pytest.raises(RuntimeError, match="models.list\\(\\) failed"):
        research.resolve_model(FailingClient())


def test_resolve_model_raises_when_no_matching_model():
    class NoSonnetClient:
        class models:
            @staticmethod
            def list():
                class M:
                    id = "claude-2-0-deprecated"
                class Resp:
                    data = [M()]
                return Resp()

    with pytest.raises(RuntimeError, match="no claude-3-5-sonnet variant was found"):
        research.resolve_model(NoSonnetClient(), preferred="claude-3-5-sonnet-nonexistent")


def test_resolve_model_returns_preferred_when_present():
    class GoodClient:
        class models:
            @staticmethod
            def list():
                class M:
                    id = "claude-3-5-sonnet-20241022"
                class Resp:
                    data = [M()]
                return Resp()

    result = research.resolve_model(GoodClient(), preferred="claude-3-5-sonnet-20241022")
    assert result == "claude-3-5-sonnet-20241022"


# ---------------------------------------------------------------------------
# State machine (§5)
# ---------------------------------------------------------------------------
def test_get_next_lead_picks_earliest_state():
    leads = [make_lead(state="DRAFTED"), make_lead(state="NEW", row_id="l2")]
    assert get_next_lead(leads)["row_id"] == "lead-1"


def test_enforce_suppression_forces_terminal():
    lead = make_lead(state="RESEARCHED", do_not_contact=True)
    out = enforce_suppression(lead)
    assert out["state"] == "SUPPRESSED"


def test_transition_new_to_researched(monkeypatch):
    fake_client = object()
    monkeypatch.setattr(research, "research_lead",
                        lambda lead, client=None, model=None: RESEARCH_HIGH)
    out = transition(make_lead(state="NEW"), FakeProvider(),
                     client=fake_client, model="m")
    assert out["state"] == "RESEARCHED"
    assert out["research_json"] == RESEARCH_HIGH


def test_transition_drafted_to_queued_passes_guardrail(monkeypatch):
    monkeypatch.setattr(research, "draft_email",
                        lambda lead, research_json, client=None, model=None:
                        {"subject": "Note", "body":
                         config.UNSUBSCRIBE_BLOCK.replace("{{email}}", lead["email"])})
    lead = make_lead(state="RESEARCHED", research_json=RESEARCH_LOW)
    out = transition(lead, FakeProvider(), model="m")
    assert out["state"] == "DRAFTED"


def test_transition_queued_sends_and_sets_sent(monkeypatch):
    monkeypatch.setattr(guardrails, "validate_draft",
                        lambda *a, **k: guardrails.GuardrailReport(passed=True, reasons=[]))
    lead = make_lead(state="QUEUED", draft_subject="s",
                     draft_body="valid body", email="asha@rao.example",
                     research_json=RESEARCH_LOW)
    provider = FakeProvider()
    out = transition(lead, provider, model="m")
    assert out["state"] == "SENT"
    assert out["provider_message_id"]
    assert provider.sent[0]["to"] == "asha@rao.example"


def test_transition_failure_sets_failed_after_max_attempts(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(research, "research_lead", boom)
    out = transition(make_lead(state="NEW", attempts=config.MAX_ATTEMPTS),
                     FakeProvider(), model="m")
    assert out["state"] == "FAILED"
    assert "llm down" in out["error_log"]


def test_dry_run_does_not_send():
    valid_body = config.UNSUBSCRIBE_BLOCK.replace("{{email}}", "asha@rao.example")
    lead = make_lead(state="QUEUED", draft_subject="s", draft_body=valid_body,
                     research_json=RESEARCH_LOW)
    provider = FakeProvider()
    out = transition(lead, provider, model="m", dry_run=True)
    assert out["state"] == "SENT"
    assert provider.sent == []
    assert out["provider_message_id"].startswith("dry-run-")


def test_transition_aborts_send_if_dnc_set_between_fetch_and_send(monkeypatch):
    monkeypatch.setattr(guardrails, "validate_draft",
                        lambda *a, **k: guardrails.GuardrailReport(passed=True, reasons=[]))
    lead = make_lead(state="QUEUED", draft_subject="s",
                     draft_body="valid body", email="asha@rao.example",
                     research_json=RESEARCH_LOW, do_not_contact=False)
    fresh_lead = dict(lead)
    fresh_lead["do_not_contact"] = True
    sheets = MemSheets(leads=[fresh_lead])
    provider = FakeProvider()
    out = transition(lead, provider, model="m", sheets=sheets)
    assert out["state"] == "SUPPRESSED"
    assert "do_not_contact" in out["error_log"]
    assert provider.sent == []


def test_transition_sends_when_dnc_still_false(monkeypatch):
    monkeypatch.setattr(guardrails, "validate_draft",
                        lambda *a, **k: guardrails.GuardrailReport(passed=True, reasons=[]))
    lead = make_lead(state="QUEUED", draft_subject="s",
                     draft_body="valid body", email="asha@rao.example",
                     research_json=RESEARCH_LOW, do_not_contact=False)
    sheets = MemSheets(leads=[lead])
    provider = FakeProvider()
    out = transition(lead, provider, model="m", sheets=sheets)
    assert out["state"] == "SENT"
    assert len(provider.sent) == 1


def test_transition_blocks_duplicate_email(monkeypatch):
    monkeypatch.setattr(guardrails, "validate_draft",
                        lambda *a, **k: guardrails.GuardrailReport(passed=True, reasons=[]))
    lead1 = make_lead(state="SENT", row_id="lead-sent", email="same@example.com")
    lead2 = make_lead(state="QUEUED", row_id="lead-queued", email="same@example.com",
                      draft_subject="s", draft_body="valid body",
                      research_json=RESEARCH_LOW)
    sheets = MemSheets(leads=[lead1, lead2])
    provider = FakeProvider()
    out = transition(lead2, provider, model="m", sheets=sheets)
    assert out["state"] == "FAILED"
    assert "duplicate" in out["error_log"].lower()
    assert provider.sent == []


def test_transition_allows_different_emails(monkeypatch):
    monkeypatch.setattr(guardrails, "validate_draft",
                        lambda *a, **k: guardrails.GuardrailReport(passed=True, reasons=[]))
    lead1 = make_lead(state="SENT", row_id="lead-sent", email="one@example.com")
    lead2 = make_lead(state="QUEUED", row_id="lead-queued", email="two@example.com",
                      draft_subject="s", draft_body="valid body",
                      research_json=RESEARCH_LOW)
    sheets = MemSheets(leads=[lead1, lead2])
    provider = FakeProvider()
    out = transition(lead2, provider, model="m", sheets=sheets)
    assert out["state"] == "SENT"
    assert len(provider.sent) == 1


def test_transition_fails_on_missing_email():
    lead = make_lead(state="NEW", email="")
    out = transition(lead, FakeProvider(), model="m")
    assert out["state"] == "FAILED"
    assert "email" in out["error_log"].lower()


def test_transition_fails_on_malformed_email():
    lead = make_lead(state="NEW", email="not-an-email")
    out = transition(lead, FakeProvider(), model="m")
    assert out["state"] == "FAILED"
    assert "email" in out["error_log"].lower()


# ---------------------------------------------------------------------------
# Sheets API error handling
# ---------------------------------------------------------------------------
def test_sheets_call_wraps_exceptions():
    from sheets import _sheets_call

    def boom():
        raise ValueError("rate limited")
    with pytest.raises(RuntimeError, match="Sheets API call failed"):
        _sheets_call(boom)


def test_sheets_call_passes_through_on_success():
    from sheets import _sheets_call

    def ok(x):
        return x * 2
    assert _sheets_call(ok, 5) == 10


# ---------------------------------------------------------------------------
# Daily cap reset
# ---------------------------------------------------------------------------
def test_daily_cap_resets_on_new_day():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sheets = MemSheets(control={"daily_cap": "5", "sent_today": "3",
                                "date_reset_at": "2020-01-01"})
    ctrl = _maybe_reset_daily_cap(sheets, sheets.get_control())
    assert ctrl["sent_today"] == "0"
    assert ctrl["date_reset_at"] == today
    assert sheets.get_control()["sent_today"] == "0"


def test_daily_cap_does_not_reset_same_day():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sheets = MemSheets(control={"daily_cap": "5", "sent_today": "3",
                                "date_reset_at": today})
    ctrl = _maybe_reset_daily_cap(sheets, sheets.get_control())
    assert ctrl["sent_today"] == "3"


def test_daily_cap_resets_when_date_reset_at_empty():
    sheets = MemSheets(control={"daily_cap": "5", "sent_today": "5",
                                "date_reset_at": ""})
    ctrl = _maybe_reset_daily_cap(sheets, sheets.get_control())
    assert ctrl["sent_today"] == "0"


# ---------------------------------------------------------------------------
# Full integration run
# ---------------------------------------------------------------------------
def test_integration_full_state_machine(monkeypatch):
    monkeypatch.setattr(research, "research_lead", lambda *a, **k: RESEARCH_HIGH)
    monkeypatch.setattr(research, "draft_email",
                        lambda lead, rj, client=None, model=None:
                        {"subject": "Note", "body":
                         config.UNSUBSCRIBE_BLOCK.replace("{{email}}", lead["email"])})
    monkeypatch.setattr(guardrails, "validate_draft",
                        lambda *a, **k: guardrails.GuardrailReport(passed=True, reasons=[]))

    sheets = MemSheets(leads=[make_lead(state="NEW", row_id="l1")])
    provider = FakeProvider()

    # NEW -> RESEARCHED (one stage per run)
    lead = get_next_lead(sheets.get_leads())
    sheets.batch_update_leads([transition(lead, provider, model="m")])
    # RESEARCHED -> DRAFTED (a second run)
    lead = get_next_lead(sheets.get_leads())
    sheets.batch_update_leads([transition(lead, provider, model="m")])

    states = {row["row_id"]: row["state"] for row in sheets.get_leads()}
    assert states["l1"] == "DRAFTED"
    assert provider.sent == []


def test_unsubscribe_link_url_encodes_plus_address(monkeypatch):
    class FakeResp:
        class Block:
            type = "text"
            text = '{"subject": "Hi", "body": "Let me know."}'
        content = [Block()]

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                return FakeResp()

    monkeypatch.setattr(research, "_get_client", lambda: FakeClient())
    monkeypatch.setattr(research, "resolve_model", lambda c, preferred=None: "m")

    lead = make_lead(email="john+legal@firm.com")
    result = research.draft_email(lead, RESEARCH_LOW, client=FakeClient(), model="m")
    body = result["body"]
    assert "john+legal@firm.com" not in body
    assert "%2B" in body or "%40" in body
    assert "nyayaworks.in/unsubscribe" in body


# ---------------------------------------------------------------------------
# run.py tests
# ---------------------------------------------------------------------------
def test_acquire_lock_grants_when_unlocked():
    sheets = MemSheets(control={"is_locked": "", "daily_cap": "5", "sent_today": "0"})
    assert _acquire_lock(sheets) is True
    assert sheets.get_control()["is_locked"] != ""


def test_acquire_lock_blocks_fresh_lock():
    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    sheets = MemSheets(control={"is_locked": recent, "daily_cap": "5", "sent_today": "0"})
    assert _acquire_lock(sheets) is False


def test_acquire_lock_clears_expired_lock():
    stale = (datetime.now(timezone.utc) - timedelta(minutes=config.LOCK_TTL_MINUTES + 5)).isoformat()
    sheets = MemSheets(control={"is_locked": stale, "daily_cap": "5", "sent_today": "0"})
    assert _acquire_lock(sheets) is True


def test_release_lock_clears_value():
    sheets = MemSheets(control={"is_locked": "2025-01-01T00:00:00+00:00"})
    _release_lock(sheets)
    assert sheets.get_control()["is_locked"] == ""


def test_preflight_raises_on_missing_key(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "key")
    monkeypatch.setattr(config, "SHEET_ID", "ok")
    monkeypatch.setattr(config, "GOOGLE_SERVICE_ACCOUNT_JSON", "ok")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Missing required environment variables"):
        _preflight_credentials()


def test_preflight_passes_with_all_keys(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_key")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "key")
    monkeypatch.setattr(config, "SHEET_ID", "sid")
    monkeypatch.setattr(config, "GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    _preflight_credentials()


def test_window_open_within_hours():
    ctrl = {"send_window_start": "9", "send_window_end": "18"}
    now = datetime(2024, 1, 1, 5, 30, tzinfo=timezone.utc)  # 11:00 IST Monday
    assert _window_open(ctrl, now=now) is True


def test_window_closed_before_start():
    ctrl = {"send_window_start": "9", "send_window_end": "18"}
    now = datetime(2024, 1, 1, 2, 0, tzinfo=timezone.utc)   # 07:30 IST
    assert _window_open(ctrl, now=now) is False


def test_window_closed_after_end():
    ctrl = {"send_window_start": "9", "send_window_end": "18"}
    now = datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc)  # 19:30 IST
    assert _window_open(ctrl, now=now) is False


def test_window_closed_on_sunday():
    ctrl = {"send_window_start": "9", "send_window_end": "18"}
    sunday_noon_ist = datetime(2024, 1, 7, 7, 0, tzinfo=timezone.utc)
    assert _window_open(ctrl, now=sunday_noon_ist) is False


def test_window_open_returns_true_when_no_config():
    assert _window_open({}) is True


def test_main_cap_reached(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "key")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "key")
    monkeypatch.setattr(config, "SHEET_ID", "sid")
    monkeypatch.setattr(config, "GOOGLE_SERVICE_ACCOUNT_JSON", "{}")

    sheets = MemSheets(
        leads=[make_lead(state="QUEUED", draft_subject="s", draft_body="b")],
        control={
            "daily_cap": "3",
            "sent_today": "3",
            "date_reset_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "is_locked": "",
            "send_window_start": "0",
            "send_window_end": "23",
        },
    )
    provider = FakeProvider()

    monkeypatch.setattr("run.GspreadClient", lambda: sheets)
    monkeypatch.setattr("run.ResendProvider", lambda: provider)
    monkeypatch.setattr("run.research.resolve_model", lambda c, preferred=None: "m")
    monkeypatch.setattr("run.research._get_client", lambda: object())

    rc = main([])
    assert rc == 0
    assert provider.sent == []


def test_get_next_lead_vertical_priority_regression():
    leads = [
        {"row_id": "1", "name": "Lead 1", "state": config.STATE_NEW},
        {"row_id": "2", "name": "Lead 2", "state": config.STATE_NEW},
        {"row_id": "3", "name": "Lead 3", "state": config.STATE_NEW},
    ]

    selected = get_next_lead(leads)
    assert selected is not None
    assert selected["row_id"] == "1"

    leads[0]["state"] = config.STATE_RESEARCHED
    selected = get_next_lead(leads)
    assert selected is not None
    assert selected["row_id"] == "1"

    leads[0]["state"] = config.STATE_DRAFTED
    selected = get_next_lead(leads)
    assert selected is not None
    assert selected["row_id"] == "1"

    leads[0]["state"] = config.STATE_QUEUED
    selected = get_next_lead(leads)
    assert selected is not None
    assert selected["row_id"] == "1"

    leads[0]["state"] = config.STATE_SENT
    selected = get_next_lead(leads)
    assert selected is not None
    assert selected["row_id"] == "2"


def test_main_window_closed(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "key")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "key")
    monkeypatch.setattr(config, "SHEET_ID", "sid")
    monkeypatch.setattr(config, "GOOGLE_SERVICE_ACCOUNT_JSON", "{}")

    sheets = MemSheets(
        leads=[make_lead(state="QUEUED", draft_subject="s", draft_body="b")],
        control={
            "daily_cap": "5",
            "sent_today": "0",
            "date_reset_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "is_locked": "",
            "send_window_start": "9",
            "send_window_end": "10",
        },
    )
    provider = FakeProvider()

    monkeypatch.setattr("run.GspreadClient", lambda: sheets)
    monkeypatch.setattr("run.ResendProvider", lambda: provider)
    monkeypatch.setattr("run._window_open", lambda ctrl, now=None: False)
    monkeypatch.setattr("run.research.resolve_model", lambda c, preferred=None: "m")
    monkeypatch.setattr("run.research._get_client", lambda: object())

    rc = main([])
    assert rc == 0
    assert provider.sent == []


def test_main_no_lead(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "key")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "key")
    monkeypatch.setattr(config, "SHEET_ID", "sid")
    monkeypatch.setattr(config, "GOOGLE_SERVICE_ACCOUNT_JSON", "{}")

    sheets = MemSheets(
        leads=[make_lead(state="SENT")],
        control={
            "daily_cap": "5",
            "sent_today": "0",
            "date_reset_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "is_locked": "",
            "send_window_start": "0",
            "send_window_end": "23",
        },
    )
    provider = FakeProvider()

    monkeypatch.setattr("run.GspreadClient", lambda: sheets)
    monkeypatch.setattr("run.ResendProvider", lambda: provider)
    monkeypatch.setattr("run._window_open", lambda ctrl, now=None: True)
    monkeypatch.setattr("run.research.resolve_model", lambda c, preferred=None: "m")
    monkeypatch.setattr("run.research._get_client", lambda: object())
    monkeypatch.setattr("run.sync_status", lambda *a, **k: [])

    rc = main([])
    assert rc == 0
    assert provider.sent == []
