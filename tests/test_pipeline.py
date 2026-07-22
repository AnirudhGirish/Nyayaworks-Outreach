"""Pytest suite for the NyayaWorks pipeline (§12).

Covers: guardrails, RFC 3464 bounce detection, state-machine transitions,
research/draft JSON parsing, the warmup gate, and one full integration run
against an in-memory Sheet + mocked LLM/SMTP. No network required.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

import config
import guardrails
import research
from bounce_detection import classify_bounce, process_messages
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
from warmup import within_trickle_budget

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
    def __init__(self, leads=None, control=None, peers=None):
        self._leads = leads or []
        self._control = control or {"daily_cap": "5", "sent_today": "0",
                                    "warmup_phase": "complete"}
        self._peers = peers or []

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
        # In-memory backend needs no tab/header bootstrap.
        return None

    def set_control(self, updates):
        self._control.update(updates)

    def get_warmup_peers(self):
        return [dict(p) for p in self._peers]

    def update_warmup_peers(self, rows):
        by_email = {p["peer_email"]: p for p in self._peers}
        for r in rows:
            if r["peer_email"] in by_email:
                by_email[r["peer_email"]].update(r)


class FakeProvider(SendProvider):
    def __init__(self):
        self.sent = []
        self.inbox = []

    def send(self, to_address, subject, body):
        mid = f"<{to_address}-{len(self.sent)}@nyayaworks.in>"
        self.sent.append({"to": to_address, "subject": subject, "body": body,
                          "mid": mid})
        return mid

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
    # 130 words of actual content (excluding unsubscribe) — should still fail
    long_body = "word " * (config.BODY_WORD_CAP + 5)
    long_body = long_body.strip() + "\n\n" + config.UNSUBSCRIBE_BLOCK.replace("{{email}}", lead["email"])
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_draft(lead, "s", long_body, RESEARCH_LOW)


def test_guardrail_word_count_excludes_unsubscribe_block():
    """A 115-word draft + 15-word unsubscribe block should pass (115 < 120).
    Before the fix, total was 130 > 120 and this would fail."""
    lead = make_lead()
    # Build a 115-word body that doesn't trigger spam phrases
    content = " ".join(["draft"] * 115)
    body = content + "\n\n" + config.UNSUBSCRIBE_BLOCK.replace("{{email}}", lead["email"])
    # Verify total word count (with unsubscribe) exceeds the cap
    assert guardrails._count_words(body) >= config.BODY_WORD_CAP
    # Now verify it passes with the fixed logic (unsubscribe excluded from count)
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
    # Body has no nyayaworks.in/unsubscribe URL — check_unsubscribe must reject it.
    # (Note: 'no unsubscribe here' would have matched the old word-only check;
    # the new URL-pattern check correctly rejects this.)
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_draft(lead, "s", "this body has no link at all", RESEARCH_LOW)


def test_guardrail_rejects_notable_fact_when_low_confidence():
    lead = make_lead()
    # Low confidence but a notable_fact string was emitted and reused in body.
    low_with_fact = dict(RESEARCH_LOW, notable_fact=RESEARCH_HIGH["notable_fact"])
    fact = low_with_fact["notable_fact"]
    body = f"We saw that you {fact}. " + \
           config.UNSUBSCRIBE_BLOCK.replace("{{email}}", lead["email"])
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_draft(lead, "s", body, low_with_fact)


# ---------------------------------------------------------------------------
# RFC 3464 bounce detection (§9)
# ---------------------------------------------------------------------------
def build_bounce_mime(action="failed", recipient="asha@rao.example",
                      original_mid="<orig-1@nyayaworks.in>"):
    msg = EmailMessage()
    msg["From"] = "mailer-daemon@secureserver.net"
    msg["Subject"] = "Delivery Status Notification (Failure)"
    msg.make_mixed()
    # human-readable part
    human = EmailMessage()
    human.set_content("Delivery failed")
    msg.attach(human)
    # delivery-status part
    ds = EmailMessage()
    ds.add_header("Content-Type", "message/delivery-status")
    ds.set_payload(
        f"Reporting-MTA: dns; secureserver.net\r\n"
        f"Original-Message-ID: {original_mid}\r\n"
        f"\r\n"
        f"Action: {action}\r\n"
        f"Final-Recipient: rfc822; {recipient}\r\n"
        f"Status: 5.1.1\r\n"
    )
    msg.attach(ds)
    return msg


def test_bounce_classify_failed():
    mime = build_bounce_mime(action="failed")
    res = classify_bounce(mime)
    assert res.is_candidate and res.is_bounce
    assert res.action == "failed"
    assert res.final_recipient == "asha@rao.example"


def test_bounce_delayed_is_not_hard_bounce():
    mime = build_bounce_mime(action="delayed")
    res = classify_bounce(mime)
    assert res.is_candidate and not res.is_bounce
    assert res.action == "delayed"


def test_bounce_classify_failed_case_insensitive():
    """Real bounces use varying capitalization: 'Failed', 'FAILED', etc."""
    for action_val in ("Failed", "FAILED", "fAiLeD"):
        mime = build_bounce_mime(action=action_val)
        res = classify_bounce(mime)
        assert res.is_bounce, f"Action: {action_val} should be classified as bounce"


def test_bounce_matches_lead_by_message_id():
    mime = build_bounce_mime(original_mid="<orig-1@nyayaworks.in>")
    leads = [make_lead(state="SENT", provider_message_id="<orig-1@nyayaworks.in>")]
    lead = process_messages([{"mime": mime}], leads)
    assert lead and lead[0][1] == "BOUNCED"


def test_bounce_match_by_recipient_address():
    mime = build_bounce_mime(original_mid="<unknown>")
    leads = [make_lead(state="SENT", email="asha@rao.example",
                       provider_message_id="<other>")]
    out = process_messages([{"mime": mime}], leads)
    assert out and out[0][1] == "BOUNCED"


def test_normal_reply_marks_replied():
    msg = EmailMessage()
    msg["From"] = "Asha Rao <asha@rao.example>"
    msg.set_content("Thanks, not interested right now.")
    leads = [make_lead(state="SENT", email="asha@rao.example")]
    out = process_messages([{"mime": msg, "from": "Asha Rao <asha@rao.example>"}],
                           leads)
    assert out and out[0][1] == "REPLIED"


def test_process_messages_returns_replies_not_bounces():
    # A mailer-daemon delayed message must NOT flip SENT to BOUNCED.
    mime = build_bounce_mime(action="delayed")
    leads = [make_lead(state="SENT", provider_message_id="<orig-1@nyayaworks.in>")]
    out = process_messages([{"mime": mime}], leads)
    assert out == []


def test_process_messages_never_crashes_on_corrupt_mime():
    """Corrupt/malformed MIME payloads must be skipped, never crash the poll."""
    leads = [make_lead(state="SENT", email="asha@rao.example")]
    corrupt_messages = [
        {},  # completely empty dict
        {"raw": None},  # None raw
        {"raw": b"\x00\x01\x02 corrupt binary"},  # binary garbage
        {"mime": "not a Message object", "from": "x@y.com"},  # wrong type
        {"raw": "", "from": ""},  # empty strings
    ]
    out = process_messages(corrupt_messages, leads)
    assert out == []  # no crashes, no false positives


def test_bounce_no_delivery_status_part_treats_as_bounce():
    """A bounce sender with no parseable delivery-status part should still
    be classified as a candidate bounce (conservative default)."""
    msg = EmailMessage()
    msg["From"] = "mailer-daemon@secureserver.net"
    msg["Subject"] = "Delivery Status Notification (Failure)"
    msg.set_content("Delivery failed but no DSN part")
    res = classify_bounce(msg)
    assert res.is_candidate
    assert res.is_bounce  # conservative: bounce sender with no DSN = bounce


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
    """If models.list() throws (network/auth), resolve_model must raise, not silently
    fall back to a possibly-deprecated model string."""
    class FailingClient:
        class models:
            @staticmethod
            def list():
                raise ConnectionError("network down")

    with pytest.raises(RuntimeError, match="models.list\\(\\) failed"):
        research.resolve_model(FailingClient())


def test_resolve_model_raises_when_no_matching_model():
    """If the live model list has no claude-3-5-sonnet entry and preferred is
    not present, resolve_model must raise rather than returning a stale string."""
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
    """Happy path: preferred model is in the live list — return it directly."""
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
    """get_next_lead() returns the first row in the list in an actionable state
    (top-to-bottom scan), regardless of which state is more advanced.
    A DRAFTED lead in row 1 beats a NEW lead in row 2 because it comes first.
    """
    leads = [make_lead(state="DRAFTED"), make_lead(state="NEW", row_id="l2")]
    assert get_next_lead(leads)["row_id"] == "lead-1"  # Row 1 (DRAFTED) comes first


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
    # Make the research call raise on the NEW->RESEARCHED transition.
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
    assert out["provider_message_id"].startswith("<dry-run-")


def test_transition_aborts_send_if_dnc_set_between_fetch_and_send(monkeypatch):
    """If do_not_contact is set in the Sheet after the lead was fetched but
    before the SMTP send, the lead must be SUPPRESSED, not sent."""
    monkeypatch.setattr(guardrails, "validate_draft",
                        lambda *a, **k: guardrails.GuardrailReport(passed=True, reasons=[]))
    lead = make_lead(state="QUEUED", draft_subject="s",
                     draft_body="valid body", email="asha@rao.example",
                     research_json=RESEARCH_LOW, do_not_contact=False)
    # Simulate the partner setting DNC=True in the Sheet between fetch and send
    fresh_lead = dict(lead)
    fresh_lead["do_not_contact"] = True
    sheets = MemSheets(leads=[fresh_lead])
    provider = FakeProvider()
    out = transition(lead, provider, model="m", sheets=sheets)
    assert out["state"] == "SUPPRESSED"
    assert "do_not_contact" in out["error_log"]
    assert provider.sent == []  # no email was sent


def test_transition_sends_when_dnc_still_false(monkeypatch):
    """When the re-fetched lead still has DNC=False, the send proceeds."""
    monkeypatch.setattr(guardrails, "validate_draft",
                        lambda *a, **k: guardrails.GuardrailReport(passed=True, reasons=[]))
    lead = make_lead(state="QUEUED", draft_subject="s",
                     draft_body="valid body", email="asha@rao.example",
                     research_json=RESEARCH_LOW, do_not_contact=False)
    sheets = MemSheets(leads=[lead])  # DNC still False in sheet
    provider = FakeProvider()
    out = transition(lead, provider, model="m", sheets=sheets)
    assert out["state"] == "SENT"
    assert len(provider.sent) == 1


def test_transition_blocks_duplicate_email(monkeypatch):
    """If another lead with the same email is already SENT, don't send again."""
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
    """Two leads with different emails should both be allowed to send."""
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
    """A lead with no email should go straight to FAILED, no LLM/SMTP calls."""
    lead = make_lead(state="NEW", email="")
    out = transition(lead, FakeProvider(), model="m")
    assert out["state"] == "FAILED"
    assert "email" in out["error_log"].lower()


def test_transition_fails_on_malformed_email():
    """A lead with a malformed email (no @) should go straight to FAILED."""
    lead = make_lead(state="NEW", email="not-an-email")
    out = transition(lead, FakeProvider(), model="m")
    assert out["state"] == "FAILED"
    assert "email" in out["error_log"].lower()


# ---------------------------------------------------------------------------
# Warmup gate (§8)
# ---------------------------------------------------------------------------
def test_trickle_budget_respects_target():
    ctrl = {"warmup_daily_target": "2"}
    assert within_trickle_budget(0, ctrl)
    assert not within_trickle_budget(2, ctrl)


# ---------------------------------------------------------------------------
# Sheets API error handling (§3 fix)
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
# Daily cap reset (§8.3 fix)
# ---------------------------------------------------------------------------
def test_daily_cap_resets_on_new_day():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sheets = MemSheets(control={"daily_cap": "5", "sent_today": "3",
                                "date_reset_at": "2020-01-01",
                                "warmup_phase": "complete"})
    ctrl = _maybe_reset_daily_cap(sheets, sheets.get_control())
    assert ctrl["sent_today"] == "0"
    assert ctrl["date_reset_at"] == today
    # Verify it actually wrote to the sheet
    assert sheets.get_control()["sent_today"] == "0"


def test_daily_cap_does_not_reset_same_day():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sheets = MemSheets(control={"daily_cap": "5", "sent_today": "3",
                                "date_reset_at": today,
                                "warmup_phase": "complete"})
    ctrl = _maybe_reset_daily_cap(sheets, sheets.get_control())
    assert ctrl["sent_today"] == "3"  # unchanged


def test_daily_cap_resets_when_date_reset_at_empty():

    sheets = MemSheets(control={"daily_cap": "5", "sent_today": "5",
                                "date_reset_at": "",
                                "warmup_phase": "complete"})
    ctrl = _maybe_reset_daily_cap(sheets, sheets.get_control())
    assert ctrl["sent_today"] == "0"


# ---------------------------------------------------------------------------
# Full integration run (§12)
# ---------------------------------------------------------------------------
def test_integration_full_state_machine(monkeypatch):
    # Mock the LLM calls inside transition.
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
    assert provider.sent == []  # nothing gets sent until QUEUED->SENT


def test_integration_sync_status_updates_bounced(monkeypatch):
    mime = build_bounce_mime(original_mid="<orig-x>")
    sheets = MemSheets(leads=[make_lead(state="SENT", row_id="l1",
                                        provider_message_id="<orig-x>")])
    provider = FakeProvider()
    provider.inbox = [{"mime": mime}]
    changed = sync_status(sheets.get_leads(), provider, sheets=sheets)
    sheets.batch_update_leads(changed)
    assert sheets.get_leads()[0]["state"] == "BOUNCED"


def test_sync_status_writes_imap_error_to_control():
    """When IMAP fails, the error must be written to control.last_error."""
    class FailingProvider(FakeProvider):
        def fetch_unread(self):
            raise RuntimeError("IMAP connection refused")

    sheets = MemSheets(leads=[], control={"daily_cap": "5", "sent_today": "0",
                                          "warmup_phase": "complete", "last_error": ""})
    changed = sync_status([], FailingProvider(), sheets=sheets)
    assert changed == []
    assert "IMAP connection refused" in sheets.get_control().get("last_error", "")


def test_sync_status_clears_imap_error_on_success():
    """When IMAP succeeds, any previous error should be cleared."""
    sheets = MemSheets(leads=[], control={"daily_cap": "5", "sent_today": "0",
                                          "warmup_phase": "complete",
                                          "last_error": "old IMAP error"})
    provider = FakeProvider()  # succeeds, returns empty inbox
    sync_status([], provider, sheets=sheets)
    assert sheets.get_control().get("last_error", "") == ""


def test_warmup_failure_written_to_control(monkeypatch):
    """When a warmup send fails, the error must be written to control.last_error."""
    from run import _run_one_warmup

    class FailingProvider(FakeProvider):
        def send(self, to_address, subject, body):
            raise RuntimeError("SMTP auth failed")

    # Mock the Anthropic client so no real API call is made
    class FakeClient:
        def messages_create(self, **kw):
            class Resp:
                class Block:
                    text = "warmup message"
                    type = "text"
                content = [Block()]
            return Resp()
        messages = type("M", (), {"create": messages_create})()

    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: FakeClient())
    monkeypatch.setattr(research, "resolve_model", lambda client, preferred=None: "fake-model")

    peer = {"peer_email": "peer@example.com", "app_password_env_var": "WARMUP_PEER_1_APP_PASSWORD",
            "last_sent_at": "", "last_received_at": ""}
    sheets = MemSheets(peers=[peer], control={"daily_cap": "5", "sent_today": "0",
                                               "warmup_phase": "active", "last_error": ""})
    provider = FailingProvider()

    _run_one_warmup(sheets, provider, [peer])
    ctrl = sheets.get_control()
    assert "warmup" in ctrl.get("last_error", "").lower()
    assert "SMTP auth failed" in ctrl.get("last_error", "")


# ---------------------------------------------------------------------------
# URL-encode email in unsubscribe link (item 12)
# ---------------------------------------------------------------------------
def test_unsubscribe_link_url_encodes_plus_address(monkeypatch):
    """Emails with '+' characters must be percent-encoded in the unsubscribe URL.
    john+legal@firm.com -> john%2Blegal%40firm.com (or similar), never raw '+'.
    """
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
    # The raw '+' must not appear unencoded in the URL query string.
    assert "john+legal@firm.com" not in body, "Email must be URL-encoded in unsubscribe link"
    assert "%2B" in body or "%40" in body, "Expected percent-encoding in unsubscribe URL"
    # The URL itself must still be present.
    assert "nyayaworks.in/unsubscribe" in body


# ---------------------------------------------------------------------------
# run.py — lock, preflight, window, daily cap, main() flow (item 13)
# ---------------------------------------------------------------------------
# (imports moved to the top of the file)


def test_acquire_lock_grants_when_unlocked():
    """An empty is_locked value means no lock is held — acquire must succeed."""
    sheets = MemSheets(control={"is_locked": "", "daily_cap": "5", "sent_today": "0",
                                "warmup_phase": "complete"})
    assert _acquire_lock(sheets) is True
    # Lock timestamp must have been written.
    assert sheets.get_control()["is_locked"] != ""


def test_acquire_lock_blocks_fresh_lock():
    """A lock set 2 minutes ago (well within TTL) must block a second run."""
    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    sheets = MemSheets(control={"is_locked": recent, "daily_cap": "5", "sent_today": "0",
                                "warmup_phase": "complete"})
    assert _acquire_lock(sheets) is False


def test_acquire_lock_clears_expired_lock():
    """A lock older than LOCK_TTL_MINUTES must be treated as stale and over-written."""
    import config as cfg
    stale = (datetime.now(timezone.utc) - timedelta(minutes=cfg.LOCK_TTL_MINUTES + 5)).isoformat()
    sheets = MemSheets(control={"is_locked": stale, "daily_cap": "5", "sent_today": "0",
                                "warmup_phase": "complete"})
    assert _acquire_lock(sheets) is True


def test_release_lock_clears_value():
    sheets = MemSheets(control={"is_locked": "2025-01-01T00:00:00+00:00",
                                "daily_cap": "5", "sent_today": "0",
                                "warmup_phase": "complete"})
    _release_lock(sheets)
    assert sheets.get_control()["is_locked"] == ""


def test_preflight_raises_on_missing_key(monkeypatch):
    """If a required env var is absent, preflight must raise RuntimeError immediately."""
    import config as cfg
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(cfg, "SHEET_ID", "ok")
    monkeypatch.setattr(cfg, "EMAIL_PASSWORD", "ok")
    monkeypatch.setattr(cfg, "GOOGLE_SERVICE_ACCOUNT_JSON", "ok")
    # Also blank the env var so the fallback os.environ.get() also fails.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Missing required environment variables"):
        _preflight_credentials()


def test_preflight_passes_with_all_keys(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "key")
    monkeypatch.setattr(cfg, "SHEET_ID", "sid")
    monkeypatch.setattr(cfg, "EMAIL_PASSWORD", "pw")
    monkeypatch.setattr(cfg, "GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    # Should not raise.
    _preflight_credentials()


def _utc(hour: int, weekday: int = 0) -> datetime:
    """Build a UTC datetime with the given hour and weekday (Monday=0, Sunday=6)."""
    # Start from a known Monday (2024-01-01 was a Monday).
    monday = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return monday + timedelta(days=weekday, hours=hour)


def test_window_open_within_hours():
    """11:00 IST = 05:30 UTC — that's inside the 9-18 IST window."""
    ctrl = {"send_window_start": "9", "send_window_end": "18"}
    # Monday 05:30 UTC = Monday 11:00 IST
    now = datetime(2024, 1, 1, 5, 30, tzinfo=timezone.utc)
    assert _window_open(ctrl, now=now) is True


def test_window_closed_before_start():
    ctrl = {"send_window_start": "9", "send_window_end": "18"}
    # 02:00 UTC = 07:30 IST (before 9am window)
    now = datetime(2024, 1, 1, 2, 0, tzinfo=timezone.utc)
    assert _window_open(ctrl, now=now) is False


def test_window_closed_after_end():
    ctrl = {"send_window_start": "9", "send_window_end": "18"}
    # 14:00 UTC = 19:30 IST (after 6pm window)
    now = datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc)
    assert _window_open(ctrl, now=now) is False


def test_window_closed_on_sunday():
    """Sundays must be blocked even if the IST hour is within 9-18."""
    ctrl = {"send_window_start": "9", "send_window_end": "18"}
    # Sunday in IST: pick a UTC time that is within window hours but on Sunday IST.
    # 2024-01-07 is a Sunday. 07:00 UTC = 12:30 IST (within window).
    sunday_noon_ist = datetime(2024, 1, 7, 7, 0, tzinfo=timezone.utc)
    assert _window_open(ctrl, now=sunday_noon_ist) is False


def test_window_open_monday_within_hours():
    """Monday at the same IST hour must be open."""
    ctrl = {"send_window_start": "9", "send_window_end": "18"}
    # 2024-01-08 is a Monday. 07:00 UTC = 12:30 IST.
    monday_noon_ist = datetime(2024, 1, 8, 7, 0, tzinfo=timezone.utc)
    assert _window_open(ctrl, now=monday_noon_ist) is True


def test_window_open_returns_true_when_no_config():
    """If send_window_start/end are absent, always return True (no restriction)."""
    assert _window_open({}) is True
    assert _window_open({"send_window_start": "", "send_window_end": ""}) is True


def test_main_cap_reached(monkeypatch):
    """When sent_today >= daily_cap, main() must exit 0 without processing any lead."""
    import config as cfg
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "key")
    monkeypatch.setattr(cfg, "SHEET_ID", "sid")
    monkeypatch.setattr(cfg, "EMAIL_PASSWORD", "pw")
    monkeypatch.setattr(cfg, "GOOGLE_SERVICE_ACCOUNT_JSON", "{}")

    sheets = MemSheets(
        leads=[make_lead(state="QUEUED", draft_subject="s", draft_body="b")],
        control={
            "daily_cap": "3",
            "sent_today": "3",
            "date_reset_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "warmup_phase": "complete",
            "is_locked": "",
            "send_window_start": "0",
            "send_window_end": "23",
            "warmup_daily_target": "2",
        },
    )
    provider = FakeProvider()

    monkeypatch.setattr("run.GspreadClient", lambda: sheets)
    monkeypatch.setattr("run.SMTPProvider", lambda: provider)
    monkeypatch.setattr("run.research.resolve_model", lambda c, preferred=None: "m")
    monkeypatch.setattr("run.research._get_client", lambda: object())

    rc = main([])
    assert rc == 0
    assert provider.sent == []  # no email sent


def test_get_next_lead_vertical_priority_regression():
    """Prove get_next_lead() stays locked on an in-flight row (Row 1) until
    terminal/sent, rather than picking fresh NEW rows or most-advanced rows.

    This test discriminates between the fixed row-order implementation and
    the old buggy sort-by-state-progression implementation:
    - Fixed:  always returns the first actionable row top-to-bottom.
    - Buggy:  sorts by state order, so a RESEARCHED Row 1 loses to NEW Row 2
              if NEW sorts before RESEARCHED in ACTIONABLE_STATES.

    Step 2 (Row1=RESEARCHED, Row2=NEW) is the critical discrimination point.
    Under the buggy code that call returns Row 2 (NEW comes first in sort).
    Under the fixed code it returns Row 1 (top-to-bottom scan, Row 1 first).
    """
    # Seed 3 leads, all NEW
    leads = [
        {"row_id": "1", "name": "Lead 1", "state": config.STATE_NEW},
        {"row_id": "2", "name": "Lead 2", "state": config.STATE_NEW},
        {"row_id": "3", "name": "Lead 3", "state": config.STATE_NEW},
    ]

    # All NEW → must pick Row 1 (first row)
    selected = get_next_lead(leads)
    assert selected is not None
    assert selected["row_id"] == "1", "Expected Row 1 when all are NEW"

    # Row 1 advances to RESEARCHED; Rows 2 & 3 remain NEW.
    # CRITICAL DISCRIMINATION POINT:
    # Buggy sort: NEW < RESEARCHED in progression order → returns Row 2 (NEW).
    # Fixed scan: Row 1 is still first in the list → returns Row 1 (RESEARCHED).
    leads[0]["state"] = config.STATE_RESEARCHED
    selected = get_next_lead(leads)
    assert selected is not None, "Should still find an actionable lead"
    assert selected["row_id"] == "1", (
        f"REGRESSION: get_next_lead() returned Row {selected['row_id']} "
        f"(state={selected['state']}) instead of staying on Row 1 (RESEARCHED). "
        "The buggy sort-by-state behavior has returned."
    )

    # Row 1 advances to DRAFTED; Rows 2 & 3 still NEW.
    leads[0]["state"] = config.STATE_DRAFTED
    selected = get_next_lead(leads)
    assert selected is not None
    assert selected["row_id"] == "1", "Must stay on Row 1 when DRAFTED"

    # Row 1 advances to QUEUED; Rows 2 & 3 still NEW.
    leads[0]["state"] = config.STATE_QUEUED
    selected = get_next_lead(leads)
    assert selected is not None
    assert selected["row_id"] == "1", "Must stay on Row 1 when QUEUED"

    # Row 1 reaches SENT (terminal for selection purposes).
    leads[0]["state"] = config.STATE_SENT

    # Now Row 1 is done → must advance to Row 2.
    selected = get_next_lead(leads)
    assert selected is not None
    assert selected["row_id"] == "2", (
        f"Must advance to Row 2 after Row 1 reaches SENT, got Row {selected['row_id']}"
    )


def test_main_window_closed(monkeypatch):
    """When outside the send window, main() must exit 0 without processing any lead."""
    import config as cfg
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "key")
    monkeypatch.setattr(cfg, "SHEET_ID", "sid")
    monkeypatch.setattr(cfg, "EMAIL_PASSWORD", "pw")
    monkeypatch.setattr(cfg, "GOOGLE_SERVICE_ACCOUNT_JSON", "{}")

    # Window is 9-10 IST; force current time to be 00:00 UTC (= 05:30 IST = before window).
    sheets = MemSheets(
        leads=[make_lead(state="QUEUED", draft_subject="s", draft_body="b")],
        control={
            "daily_cap": "5",
            "sent_today": "0",
            "date_reset_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "warmup_phase": "complete",
            "is_locked": "",
            "send_window_start": "9",
            "send_window_end": "10",
            "warmup_daily_target": "2",
        },
    )
    provider = FakeProvider()

    monkeypatch.setattr("run.GspreadClient", lambda: sheets)
    monkeypatch.setattr("run.SMTPProvider", lambda: provider)
    # Force _window_open to return False by setting a very narrow window
    # and freezing the clock to a time outside it.
    monkeypatch.setattr("run._window_open", lambda ctrl, now=None: False)
    monkeypatch.setattr("run.research.resolve_model", lambda c, preferred=None: "m")
    monkeypatch.setattr("run.research._get_client", lambda: object())

    rc = main([])
    assert rc == 0
    assert provider.sent == []


def test_main_no_lead(monkeypatch):
    """When there are no actionable leads, main() must exit 0 cleanly."""
    import config as cfg
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "key")
    monkeypatch.setattr(cfg, "SHEET_ID", "sid")
    monkeypatch.setattr(cfg, "EMAIL_PASSWORD", "pw")
    monkeypatch.setattr(cfg, "GOOGLE_SERVICE_ACCOUNT_JSON", "{}")

    sheets = MemSheets(
        leads=[make_lead(state="SENT")],  # terminal — nothing to advance
        control={
            "daily_cap": "5",
            "sent_today": "0",
            "date_reset_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "warmup_phase": "complete",
            "is_locked": "",
            "send_window_start": "0",
            "send_window_end": "23",
            "warmup_daily_target": "2",
        },
    )
    provider = FakeProvider()

    monkeypatch.setattr("run.GspreadClient", lambda: sheets)
    monkeypatch.setattr("run.SMTPProvider", lambda: provider)
    monkeypatch.setattr("run._window_open", lambda ctrl, now=None: True)
    monkeypatch.setattr("run.research.resolve_model", lambda c, preferred=None: "m")
    monkeypatch.setattr("run.research._get_client", lambda: object())
    monkeypatch.setattr("run.sync_status", lambda *a, **k: [])

    rc = main([])
    assert rc == 0
    assert provider.sent == []

