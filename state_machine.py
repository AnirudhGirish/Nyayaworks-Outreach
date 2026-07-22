""""State machine transitions (§5).

A lead advances exactly ONE stage per cron run, and do_not_contact forces
SUPPRESSED before any transition. Each function mutates a copy of the lead row
and returns it with updated state/timestamps — write_back() persists it.
External calls (LLM, SMTP) are wrapped so failures are recorded in error_log
rather than crashing the run.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import config
import guardrails
import research
from bounce_detection import process_messages
from providers import SendProvider


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _refetch_lead(sheets, row_id: str) -> dict[str, Any] | None:
    """Re-read a single lead from the Sheet by row_id. Returns None on error."""
    try:
        leads = sheets.get_leads()
        for lead in leads:
            if lead.get("row_id") == row_id:
                return lead
    except Exception:
        return None
    return None


def _has_duplicate_sent(sheets, email: str, row_id: str) -> bool:
    """Check if another lead with the same email is already SENT or later.

    Returns True if a duplicate exists (excluding the current lead by row_id).
    """
    if not email:
        return False
    sent_or_later = {config.STATE_SENT, config.STATE_REPLIED, config.STATE_BOUNCED}
    try:
        leads = sheets.get_leads()
        for lead in leads:
            if lead.get("row_id") == row_id:
                continue
            if (lead.get("email") or "").lower() == email.lower() and lead.get("state") in sent_or_later:
                return True
    except Exception:
        return False
    return False


def get_next_lead(leads: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the first row in the sheet that is in an actionable state.

    Processes sequentially row-by-row (top to bottom) so a single lead moves
    towards SENT before picking up arbitrary future rows. Regression test:
    test_get_next_lead_vertical_priority_regression.
    """
    actionable_states = set(config.ACTIONABLE_STATES)
    for lead in leads:
        state = (lead.get("state") or "").strip().upper()
        if state in actionable_states:
            return lead
    return None


def enforce_suppression(lead: dict[str, Any]) -> dict[str, Any] | None:
    """If do_not_contact, force SUPPRESSED. Returns the mutated lead or None."""
    if lead.get("do_not_contact"):
        updated = deepcopy(lead)
        updated["state"] = config.STATE_SUPPRESSED
        updated["last_updated_at"] = _now_iso()
        updated["error_log"] = "suppressed: do_not_contact is true"
        return updated
    return None


def transition(
    lead: dict[str, Any],
    provider: SendProvider,
    client=None,
    model: str | None = None,
    *,
    dry_run: bool = False,
    sheets=None,
) -> dict[str, Any]:
    """Advance one lead exactly one stage. Persists nothing; returns the row.

    If ``sheets`` is provided, the QUEUED→SENT transition re-reads the lead
    row from the Sheet to check do_not_contact at the literal last moment
    before sending — defense in depth against a stale in-memory dict.
    """
    lead = deepcopy(lead)
    lead["attempts"] = int(lead.get("attempts", 0)) + 1
    state = lead.get("state")

    # Early validation: required fields must be present before any LLM/SMTP calls.
    email_val = (lead.get("email") or "").strip()
    if not email_val or "@" not in email_val:
        lead["state"] = config.STATE_FAILED
        lead["error_log"] = f"invalid or missing email: {email_val!r}"
        lead["last_updated_at"] = _now_iso()
        return lead

    try:
        if state == config.STATE_NEW:
            research_json = research.research_lead(lead, client=client, model=model)
            lead["research_json"] = research_json
            lead["state"] = config.STATE_RESEARCHED

        elif state == config.STATE_RESEARCHED:
            research_json = lead.get("research_json") or {}
            draft = research.draft_email(lead, research_json, client=client, model=model)
            lead["draft_subject"] = draft["subject"]
            lead["draft_body"] = draft["body"]
            lead["state"] = config.STATE_DRAFTED

        elif state == config.STATE_DRAFTED:
            research_json = lead.get("research_json") or {}
            guardrails.validate_draft(
                lead, lead["draft_subject"], lead["draft_body"], research_json
            )
            lead["state"] = config.STATE_QUEUED

        elif state == config.STATE_QUEUED:
            guardrails.validate_draft(
                lead, lead["draft_subject"], lead["draft_body"], lead.get("research_json")
            )
            # Defense in depth: re-read the row from the Sheet and re-check
            # do_not_contact at the literal last moment before sending.
            if sheets is not None and not dry_run:
                fresh = _refetch_lead(sheets, lead.get("row_id", ""))
                if fresh is not None and fresh.get("do_not_contact"):
                    lead["state"] = config.STATE_SUPPRESSED
                    lead["error_log"] = "suppressed: do_not_contact set between fetch and send"
                    lead["last_updated_at"] = _now_iso()
                    return lead
                # Duplicate-email protection: skip if another lead with the
                # same email is already SENT or in a later state.
                dup = _has_duplicate_sent(sheets, lead.get("email", ""), lead.get("row_id", ""))
                if dup:
                    lead["state"] = config.STATE_FAILED
                    lead["error_log"] = "duplicate email: another lead with this address is already SENT or later"
                    lead["last_updated_at"] = _now_iso()
                    return lead
            if dry_run:
                msg_id = f"<dry-run-{lead['row_id']}@nyayaworks.in>"
            else:
                msg_id = provider.send(
                    lead["email"], lead["draft_subject"], lead["draft_body"]
                )
            lead["provider_message_id"] = msg_id
            lead["sent_at"] = _now_iso()
            lead["state"] = config.STATE_SENT
        else:
            # Terminal / nothing to advance.
            return lead
    except Exception as exc:  # noqa: BLE001 - record, never crash the run
        lead["error_log"] = f"{type(exc).__name__}: {exc}"
        if lead["attempts"] >= config.MAX_ATTEMPTS:
            lead["state"] = config.STATE_FAILED
        return lead

    lead["last_updated_at"] = _now_iso()
    lead["error_log"] = ""
    return lead


def sync_status(
    leads: list[dict[str, Any]],
    provider: SendProvider,
    sheets=None,
) -> list[dict[str, Any]]:
    """Poll the inbox and return lead rows whose state changed (§9).

    If ``sheets`` is provided, IMAP failures are written to the control tab's
    ``last_error`` field so a broken connection doesn't go unnoticed.
    """
    try:
        messages = provider.fetch_unread()
    except Exception as exc:
        msg = f"IMAP fetch failed: {exc}"
        print(f"sync_status: {msg}")
        if sheets is not None:
            try:
                sheets.set_control({"last_error": msg})
            except Exception:
                pass
        return []
    # Clear any previous IMAP error on success
    if sheets is not None:
        try:
            sheets.set_control({"last_error": ""})
        except Exception:
            pass
    updates = process_messages(messages, leads)
    changed: list[dict[str, Any]] = []
    for lead, new_state in updates:
        updated = deepcopy(lead)
        updated["state"] = new_state
        updated["last_updated_at"] = _now_iso()
        changed.append(updated)
    return changed