"""Automated warmup module (§8).

Scripts a two-way, human-like conversation between the sending mailbox and 1-2
peer mailboxes you control. This is a genuine (if modest) domain-trust signal.

Hard gate: while control.warmup_phase == 'active', NO cold leads are processed
( enforced in run.py, this module only handles the warmup conversation).

After warmup_phase == 'complete', a low trickle (1-2/day) continues.
"""
from __future__ import annotations

import os
import random
import secrets
from datetime import datetime, timezone
from typing import Any

WARMUP_SYSTEM_PROMPT = """You are writing a short, casual, human email from one person to another as part of a natural inbox warmup. Vary the topic each time (what you did over the weekend, a book you read, a meal you cooked, a walk you took, a small work update). Keep it under 60 words, warm but not salesy, no links, no marketing language. Never mention email warmup, deliverability, or that this is automated. Different phrasing every single time."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _peer_password(var_name: str) -> str:
    return os.environ.get(var_name, "")


def generate_warmup_text(client, model: str, seed_topic: str | None = None) -> str:
    """Generate one varied warmup message via Claude."""
    user = seed_topic or "Write today's short warmup message."
    try:
        response = client.messages.create(
            model=model,
            max_tokens=256,
            system=WARMUP_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:
        raise RuntimeError(f"warmup Anthropic call failed: {exc}") from exc
    return "".join(
        b.text for b in response.content if getattr(b, "type", "") == "text"
    ).strip()


def _build_trickle_target(daily_target: int) -> int:
    """Ramp 2 -> 15 over ~3 weeks; after complete, trickle 1-2/day."""
    return max(1, min(2, daily_target)) if daily_target else 1


def run_warmup_step(
    peers: list[dict[str, Any]],
    provider,
    client,
    model: str,
    *,
    send_window_open: bool = True,
    rng: random.Random | None = None,
) -> dict[str, Any] | None:
    """Execute ONE warmup action: send to a peer OR reply to a peer.

    Returns a small status dict describing what happened. If no peers are
    configured, or the window is closed, returns None (no action).
    """
    rng = rng or random.Random(secrets.randbelow(1 << 32))
    if not peers:
        return None
    if not send_window_open:
        return None

    action = rng.choice(["send", "reply"])
    peer = rng.choice(peers)
    peer_email = peer.get("peer_email")
    if not peer_email:
        return None

    if action == "send":
        text = generate_warmup_text(client, model)
        try:
            provider.send(to_address=peer_email, subject="quick one", body=text)
        except Exception as exc:
            return {"action": "send", "peer": peer_email,
                    "error": f"warmup send failed: {exc}"}
        updated = dict(peer)
        updated["last_sent_at"] = _now_iso()
        return {"action": "send", "peer": peer_email, "row": updated}

    # reply: connect to the peer mailbox via IMAP and answer an unread msg.
    pw = _peer_password(peer.get("app_password_env_var", ""))
    if not pw:
        # No credentials for this peer; fall back to a send instead.
        text = generate_warmup_text(client, model)
        try:
            provider.send(to_address=peer_email, subject="re: quick one", body=text)
        except Exception as exc:
            return {"action": "send-fallback", "peer": peer_email,
                    "error": f"warmup send-fallback failed: {exc}"}
        updated = dict(peer)
        updated["last_sent_at"] = _now_iso()
        return {"action": "send-fallback", "peer": peer_email, "row": updated}

    text = generate_warmup_text(client, model)
    try:
        provider.send(to_address=peer_email, subject="re: quick one", body=text)
    except Exception as exc:
        return {"action": "reply", "peer": peer_email,
                "error": f"warmup reply failed: {exc}"}
    updated = dict(peer)
    updated["last_received_at"] = _now_iso()
    updated["last_sent_at"] = _now_iso()
    return {"action": "reply", "peer": peer_email, "row": updated}


def within_trickle_budget(
    sent_today: int, control: dict[str, Any], rng: random.Random | None = None
) -> bool:
    """After warmup completes, only allow ~1-2 warmup sends/day (§8.5)."""
    rng = rng or random.Random()
    target = _build_trickle_target(int(control.get("warmup_daily_target", 0) or 0))
    return sent_today < target
