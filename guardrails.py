"""Deterministic guardrail engine (§7).

Pure functions. Every drafted email must clear all of these before it can
move to QUEUED. If any check fails we raise GuardrailError so the caller can
move the row to FAILED and record the reason in error_log.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import config


class GuardrailError(Exception):
    """Raised when a draft fails one of the deterministic checks (§7)."""


@dataclass
class GuardrailReport:
    passed: bool
    reasons: list[str]


def _count_words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _strip_unsubscribe(body: str) -> str:
    """Remove the unsubscribe block from a body so word count reflects only
    the actual message content, not the mandatory footer."""
    lines = body.splitlines()
    result_lines = []
    for line in lines:
        if "unsubscribe" in line.lower() and "nyayaworks.in/unsubscribe" in line:
            continue
        result_lines.append(line)
    return "\n".join(result_lines).strip()


def check_word_count(body: str, cap: int = config.BODY_WORD_CAP) -> bool:
    # Exclude the unsubscribe block from the count — it's a fixed footer,
    # not part of the 120-word body cap the LLM was instructed to respect.
    stripped = _strip_unsubscribe(body)
    return _count_words(stripped) < cap


def check_spam_phrases(text: str, banned: list[str] | None = None) -> list[str]:
    banned = banned if banned is not None else config.BANNED_PHRASES
    lowered = (text or "").lower()
    hits = [phrase for phrase in banned if phrase in lowered]
    return hits


def check_unsubscribe(body: str, lead_email: str = "") -> bool:
    """Check that the mandatory unsubscribe URL is present in the body.

    We require the actual URL domain pattern (nyayaworks.in/unsubscribe),
    not just the word 'unsubscribe' — the latter matches false positives like
    'I am not asking you to unsubscribe'. Only a properly appended block passes.
    """
    return "nyayaworks.in/unsubscribe" in (body or "")


def check_notable_fact_usage(body: str, research: dict[str, Any] | None) -> str | None:
    """notable_fact may appear in the body ONLY if confidence == high.

    Returns the violation reason or None.
    """
    if not research:
        return None
    confidence = research.get("confidence")
    notable_fact = research.get("notable_fact")
    if confidence != "high" and notable_fact:
        # The AI was told to use the fallback; if the notable_fact text still
        # appears verbatim in the body, that's a violation.
        if notable_fact and notable_fact in body:
            return (
                "notable_fact used in body but research confidence is not high"
            )
    return None


def check_do_not_contact(lead: dict[str, Any]) -> bool:
    return not bool(lead.get("do_not_contact"))


def validate_draft(
    lead: dict[str, Any],
    subject: str,
    body: str,
    research: dict[str, Any] | None = None,
) -> GuardrailReport:
    """Run every §7 check. Returns a report; raises GuardrailError on failure.

    Caller should treat a raised GuardrailError as: set state FAILED, append
    ``str(error)`` to error_log. Never silently send.
    """
    reasons: list[str] = []

    if not check_do_not_contact(lead):
        reasons.append("recipient is on do_not_contact")

    if not check_word_count(body):
        reasons.append(
            f"body word count exceeds cap ({config.BODY_WORD_CAP})"
        )

    spam_hits = check_spam_phrases(f"{subject}\n{body}")
    if spam_hits:
        reasons.append(f"banned phrases present: {', '.join(spam_hits)}")

    if not check_unsubscribe(body, lead.get("email", "")):
        reasons.append("unsubscribe block missing or incorrect")

    fact_violation = check_notable_fact_usage(body, research)
    if fact_violation:
        reasons.append(fact_violation)

    report = GuardrailReport(passed=not reasons, reasons=reasons)
    if reasons:
        raise GuardrailError("; ".join(reasons))
    return report