"""Anthropic Claude integrations: research (§6a) and drafting (§6b).

Uses the native web_search tool for research. The model string is NOT
hardcoded — we resolve a current Claude 3.5 Sonnet variant from the live
model list at runtime, falling back to the configured default. JSON output is
parsed and schema-validated; a malformed/non-conforming response raises rather
than being guessed at.

Every Anthropic API call uses an explicit timeout and catches specific network
exceptions so callers always get a clean RuntimeError they can log.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any

import config

RESEARCH_PROMPT = """You are a research assistant preparing a factual briefing note for a single B2B outreach email. You will be given a lead's name, firm name, and website URL, plus fetched content from that website via web search.

RULES:
- Use ONLY facts present in the provided source material. Never infer, assume, or guess.
- If no reliable information exists for a field, output null for it. Do not fabricate placeholders.
- "notable_fact" must be ONE specific, verifiable detail useful for a one-line personalization (e.g., "handles corporate M&A for mid-market clients in Bangalore"). Generic filler is completely unacceptable—output null instead.

Output strictly as JSON, no other text:
{
  "firm_name": string,
  "practice_areas": string[],
  "location": string | null,
  "notable_fact": string | null,
  "confidence": "high" | "low"
}"""

def _build_draft_prompt() -> str:
    """Build DRAFT_PROMPT with the current banned-phrase list injected.

    The LLM has no knowledge of the guardrail blocklist unless we tell it.
    Including the list here means Claude avoids these words at generation time,
    which prevents wasted retries when the post-generation guardrail catches them.
    The guardrail still runs as the hard backstop — this is additive.
    """
    banned = ", ".join(f'"{p}"' for p in config.BANNED_PHRASES)
    return (
        "You are drafting ONE cold outreach email on behalf of NyayaOS to a legal "
        "professional. You are given a fixed template, a research JSON object, the "
        "recipient's name, and a list of verified product_facts.\n"
        "\n"
        "RULES:\n"
        "- Fill ONLY the marked personalization slots in the template using facts from "
        "research_json. Every other part of the template is fixed and must not be altered.\n"
        "- If research_json.confidence is \"low\" or notable_fact is null, use the "
        "template's generic fallback slot.\n"
        "- Never invent case names, client names, statistics, or claims.\n"
        "- Tone: Highly professional, concise, and respectful. No hype, no exclamation marks.\n"
        "- Body under 120 words.\n"
        f"- Do NOT use any of the following words or phrases (they trigger spam filters): {banned}.\n"
        "\n"
        "Output strictly as JSON, no other text:\n"
        '{ "subject": string, "body": string }'
    )


DRAFT_PROMPT = _build_draft_prompt()


def _get_client():
    import anthropic

    api_key = config.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")
    return anthropic.Anthropic(
        api_key=api_key,
        timeout=config.ANTHROPIC_TIMEOUT,
    )


def resolve_model(client, preferred: str | None = None) -> str:
    """Pick a current stable Claude 3.5 Sonnet model from the live model list.

    Calls the Anthropic models.list() endpoint. Raises RuntimeError on any
    network/auth failure — a silent fallback to a possibly-deprecated model
    string is worse than a loud failure that gets logged in error_log.

    Resolution order:
      1. If ``preferred`` is in the live model list, use it.
      2. Else pick the latest id containing 'claude-3-5-sonnet'.
      3. If neither match, raise — do not silently accept an unknown model.
    """
    preferred = preferred or config.ANTHROPIC_MODEL
    try:
        models = client.models.list()
        ids = [m.id for m in models.data]
    except Exception as exc:
        raise RuntimeError(
            f"resolve_model: Anthropic models.list() failed — "
            f"cannot confirm a live model id: {exc}"
        ) from exc

    if preferred in ids:
        return preferred
    sonnet = [mid for mid in ids if "claude-3-5-sonnet" in mid]
    if sonnet:
        # Prefer the latest (longest id string sorts newest typically).
        return sorted(sonnet)[-1]
    raise RuntimeError(
        f"resolve_model: preferred model {preferred!r} is not in the live "
        f"model list and no claude-3-5-sonnet variant was found. "
        f"Available: {ids}"
    )


def _extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of the model's text response."""
    text = text.strip()
    # The model is instructed to output JSON only, but be defensive.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model did not return valid JSON: {exc}") from exc


def _validate_research(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError("research result is not an object")
    required = {"firm_name", "practice_areas", "location", "notable_fact", "confidence"}
    if not required.issubset(obj.keys()):
        raise ValueError(f"research result missing keys: {required - obj.keys()}")
    if obj["confidence"] not in ("high", "low"):
        raise ValueError("research confidence must be 'high' or 'low'")
    if not isinstance(obj["practice_areas"], list):
        raise ValueError("practice_areas must be a list")
    return obj


def _call_anthropic(client, model: str, system: str, user_content: str,
                    tools: list[dict] | None = None) -> str:
    """Wrapper that catches Anthropic-specific exceptions and raises RuntimeError."""
    try:
        response = client.messages.create(
            model=model,
            max_tokens=config.ANTHROPIC_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user_content}],
            **({"tools": tools} if tools else {}),
        )
    except Exception as exc:
        # Catch anthropic.APIError, APIConnectionError, httpx.TimeoutException,
        # and any other transport/protocol error, then re-raise cleanly.
        raise RuntimeError(f"Anthropic API call failed: {exc}") from exc
    return "".join(
        block.text for block in response.content
        if getattr(block, "type", "") == "text"
    )


def research_lead(lead: dict[str, Any], client=None, model: str | None = None) -> dict[str, Any]:
    """Run the factual research step (§6a) with native web search."""
    client = client or _get_client()
    model = model or resolve_model(client)

    user_content = (
        f"Name: {lead.get('name', '')}\n"
        f"Firm: {lead.get('firm_name', '')}\n"
        f"Website: {lead.get('website', '')}\n"
        "Research this firm and return the JSON briefing."
    )

    text = _call_anthropic(
        client,
        model,
        RESEARCH_PROMPT,
        user_content,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
    )
    return _validate_research(_extract_json(text))


def draft_email(
    lead: dict[str, Any],
    research: dict[str, Any],
    client=None,
    model: str | None = None,
) -> dict[str, str]:
    """Run the contextual drafting step (§6b) and enforce the unsubscribe block."""
    client = client or _get_client()
    model = model or resolve_model(client)

    user_content = (
        f"Recipient name: {lead.get('name', '')}\n"
        f"Research JSON:\n{json.dumps(research)}\n"
        f"Template:\n{config.EMAIL_TEMPLATE}\n"
        f"product_facts: {config.PRODUCT_FACTS}\n"
        "Fill the template slots and return the JSON with subject and body."
    )

    text = _call_anthropic(client, model, DRAFT_PROMPT, user_content)
    obj = _extract_json(text)
    if not isinstance(obj, dict) or "subject" not in obj or "body" not in obj:
        raise ValueError("draft result missing subject/body")

    subject = str(obj["subject"]).strip()
    raw_body = str(obj["body"]).strip()

    # Formulate the deterministic unsubscribe block required by config.
    # URL-encode the email so addresses with '+' or special chars don't
    # produce malformed query strings.
    lead_email = lead.get("email", "")
    encoded_email = urllib.parse.quote(lead_email, safe="")
    unsub_block = config.UNSUBSCRIBE_BLOCK.replace("{{email}}", encoded_email)

    # Ensure the URL pattern is in the body; if not, force the block onto the end.
    if "nyayaworks.in/unsubscribe" not in raw_body:
        final_body = f"{raw_body}\n\n{unsub_block}"
    else:
        final_body = raw_body

    return {"subject": subject, "body": final_body}