""""RFC 3464 hardened bounce detection (§9).

We classify messages by MIME structure. A candidate bounce is one whose sender
is a mailer-daemon/postmaster, whose Content-Type is multipart/report
(message/delivery-status), or whose subject contains common delivery-failure
indicators. We parse delivery-status parts for correlation back to a lead.
"""
from __future__ import annotations

import email
from dataclasses import dataclass
from email.message import Message
from typing import Any

BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")


@dataclass
class BounceResult:
    is_candidate: bool
    action: str | None = None  # "failed" | "delayed" | None
    original_message_id: str | None = None
    final_recipient: str | None = None
    is_bounce: bool = False  # True only when Action: failed or 5xx NDR present


def is_bounce_sender(from_header: str) -> bool:
    lowered = (from_header or "").lower()
    return any(sender in lowered for sender in BOUNCE_SENDERS)


def _walk_delivery_status(mime: Message) -> Message | None:
    """Return the first message/delivery-status part if present."""
    if mime.get_content_type() == "message/delivery-status":
        return mime
    for part in mime.walk():
        if part.get_content_type() == "message/delivery-status":
            return part
    return None


def _parse_ds_fields(ds_part: Message) -> dict[str, str]:
    """Extract per-recipient fields across all header blocks in a delivery-status part."""
    fields: dict[str, str] = {}
    payload = ds_part.get_payload()

    raw_blocks: list[str] = []
    if isinstance(payload, list):
        for subpart in payload:
            if hasattr(subpart, "as_string"):
                raw_blocks.append(subpart.as_string())
            else:
                raw_blocks.append(str(subpart))
        raw = "\n".join(raw_blocks)
    elif isinstance(payload, str):
        raw = payload
    else:
        raw = ds_part.as_string()

    for line in raw.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fields[key.strip().lower()] = val.strip()
    return fields


def _extract_full_text(mime: Message) -> str:
    """Recursively extract and decode all text/plain, text/html, and attachment payloads."""
    text_parts: list[str] = []

    def _walk_mime(m: Message) -> None:
        for part in m.walk():
            if part.get_content_type() == "message/rfc822":
                p = part.get_payload()
                if isinstance(p, list) and p and isinstance(p[0], Message):
                    _walk_mime(p[0])
                elif isinstance(p, Message):
                    _walk_mime(p)
                continue

            content_type = part.get_content_type()
            if content_type in ("text/plain", "text/html", "message/delivery-status"):
                try:
                    p = part.get_payload(decode=True)
                    if isinstance(p, (bytes, bytearray)):
                        text_parts.append(p.decode("utf-8", errors="ignore"))
                    else:
                        text_parts.append(part.as_string())
                except Exception:
                    pass

    _walk_mime(mime)
    return "\n".join(text_parts)


def classify_bounce(mime: Message) -> BounceResult:
    """Classify a single parsed email.Message.

    Returns a BounceResult. is_candidate marks bounce structural/header features;
    is_bounce is True for genuine hard failures.
    """
    from_header = mime.get("From", "")
    content_type = mime.get_content_type()
    subject = mime.get("Subject", "").lower()

    is_candidate = (
        is_bounce_sender(from_header)
        or content_type in ("multipart/report", "message/delivery-status")
        or "delivery status notification" in subject
        or "undeliverable" in subject
        or "mail delivery failed" in subject
        or "message delivery failure" in subject
    )
    if not is_candidate:
        return BounceResult(is_candidate=False)

    ds_part = _walk_delivery_status(mime)
    if ds_part is not None:
        fields = _parse_ds_fields(ds_part)
        action = fields.get("action")
        final_recipient = fields.get("final-recipient")
        original_message_id = fields.get("original-message-id")
        diag_code = fields.get("diagnostic-code", "")

        if final_recipient and ";" in final_recipient:
            final_recipient = final_recipient.split(";", 1)[1].strip()

        is_bounce = (
            (action is not None and action.lower() == "failed")
            or (action is None and ("550" in diag_code or "5." in diag_code or final_recipient is not None))
        )

        return BounceResult(
            is_candidate=True,
            action=action or "failed",
            original_message_id=original_message_id,
            final_recipient=final_recipient,
            is_bounce=is_bounce,
        )

    return BounceResult(is_candidate=True, action="failed", is_bounce=True)


def match_lead(
    result: BounceResult,
    leads: list[dict[str, Any]],
    raw_text: str = "",
) -> dict[str, Any] | None:
    """Find the lead row this bounce refers to.

    Prefers Original-Message-ID matching against provider_message_id; falls back
    to final recipient address or full-text body search.
    """
    if not result.is_bounce:
        return None

    candidates = [
        lead for lead in leads
        if (lead.get("state") or "").strip().upper() == "SENT"
    ]

    # 1. Match by provider_message_id
    if result.original_message_id:
        for lead in candidates:
            if lead.get("provider_message_id") == result.original_message_id:
                return lead

    # 2. Match by parsed final_recipient
    if result.final_recipient:
        target = result.final_recipient.lower()
        for lead in candidates:
            if (lead.get("email") or "").strip().lower() == target:
                return lead

    # 3. Fallback: Search decoded body text for candidate lead's email or message_id
    if raw_text:
        lowered_raw = raw_text.lower()
        for lead in candidates:
            lead_email = (lead.get("email") or "").strip().lower()
            lead_msg_id = (lead.get("provider_message_id") or "").strip("<>").lower()

            if lead_email and lead_email in lowered_raw:
                return lead
            if lead_msg_id and lead_msg_id in lowered_raw:
                return lead

    return None


def process_messages(
    messages: list[dict[str, Any]],
    leads: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], str]]:
    """Return [(lead_row, new_state)] updates derived from polled messages.

    Bounces -> BOUNCED. Delayed -> leave as SENT (no change). A non-bounce
    message from the lead's own address -> REPLIED.
    """
    updates: list[tuple[dict[str, Any], str]] = []
    by_email = {
        (lead.get("email") or "").strip().lower(): lead
        for lead in leads
        if (lead.get("state") or "").strip().upper() == "SENT"
    }

    for msg in messages:
        try:
            mime = (
                msg["mime"]
                if isinstance(msg.get("mime"), Message)
                else email.message_from_string(str(msg.get("raw", "")))
            )
            result = classify_bounce(mime)

            if result.is_candidate and result.is_bounce:
                full_text = _extract_full_text(mime)
                lead = match_lead(result, leads, raw_text=full_text)
                if lead is not None:
                    updates.append((lead, "BOUNCED"))
                continue

            if not result.is_candidate:
                sender_email = _extract_address(str(msg.get("from", "")))
                lead = by_email.get(sender_email.lower())
                if lead is not None:
                    updates.append((lead, "REPLIED"))
        except Exception:
            # Never let a single corrupt/malformed message crash the whole poll.
            continue

    return updates


def _extract_address(from_header: str) -> str:
    import re

    match = re.search(r"<([^>]+)>", from_header)
    if match:
        return match.group(1)
    return from_header.strip()