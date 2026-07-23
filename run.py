""""Stateless entrypoint executed by the Railway Cron Job.

Flow per run (§2):
  1. Check control.is_locked; if set and < LOCK_TTL_MINUTES old, exit (another
     run is still in flight). Otherwise acquire the lock.
  2. sync_status() — poll Resend API event status & IMAP for replies on SENT rows.
  3. Check daily cap and send window, fetch next lead, advance ONE stage, write back.
  4. Clear control.is_locked.

Every external call is wrapped so failures go to error_log, never crash the
whole run. With --dry-run we skip the actual send.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

# load_dotenv() must be called before the local imports below so that config.py
# picks up .env variables at module-import time. The E402 noqa tags are
# intentional — do NOT reorder these imports.
import argparse  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import config  # noqa: E402
import research  # noqa: E402
from providers import ResendProvider  # noqa: E402
from sheets import GspreadClient, SheetsClient  # noqa: E402
from state_machine import (  # noqa: E402
    enforce_suppression,
    get_next_lead,
    sync_status,
    transition,
)


def _preflight_credentials() -> None:
    """Enforce that every required secret is present at startup (§ security).

    If any mandatory variable is missing we raise RuntimeError immediately so
    the cron job fails fast rather than silently sending nothing.
    """
    missing: list[str] = []
    checks = [
        ("RESEND_API_KEY", config.RESEND_API_KEY or os.environ.get("RESEND_API_KEY")),
        ("ANTHROPIC_API_KEY", config.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY")),
        ("SHEET_ID", config.SHEET_ID or os.environ.get("SHEET_ID")),
        ("GOOGLE_SERVICE_ACCOUNT_JSON", config.GOOGLE_SERVICE_ACCOUNT_JSON or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")),
    ]
    for name, val in checks:
        if not val:
            missing.append(name)
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Set them in Railway → Variables before running."
        )


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _acquire_lock(sheets: SheetsClient) -> bool:
    """Return True if we may proceed (lock free or expired). Acquires it."""
    ctrl = sheets.get_control()
    locked_at = _parse_iso(ctrl.get("is_locked", ""))
    now = datetime.now(timezone.utc)
    if locked_at is not None:
        age_min = (now - locked_at).total_seconds() / 60.0
        if age_min < config.LOCK_TTL_MINUTES:
            print(f"Lock held ({age_min:.1f}m old) — exiting to avoid overlap.")
            return False
    sheets.set_control({"is_locked": now.isoformat()})
    return True


def _release_lock(sheets: SheetsClient) -> None:
    sheets.set_control({"is_locked": ""})


def _window_open(ctrl: dict, now: datetime | None = None) -> bool:
    """Send window check (9am-6pm IST — UTC+5:30). Skips Sundays.

    Accepts an optional ``now`` parameter (UTC datetime) for testability.
    """
    start = ctrl.get("send_window_start")
    end = ctrl.get("send_window_end")
    if not start or not end:
        return True
    now = now or datetime.now(timezone.utc)
    # IST is UTC+5:30. Compute IST hour and weekday.
    ist = now + timedelta(hours=5, minutes=30)
    # Sunday = weekday 6
    if ist.weekday() == 6:
        return False
    ist_hour = ist.hour
    return int(start) <= ist_hour < int(end)


def _maybe_reset_daily_cap(sheets: SheetsClient, ctrl: dict) -> dict:
    """Reset sent_today to 0 if the date has rolled over since date_reset_at.

    Uses UTC date for consistency with Railway's cron evaluation. If
    date_reset_at is empty or stale (different UTC date than today), reset
    sent_today and write the new date. Returns the updated control dict.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    reset_at = ctrl.get("date_reset_at", "") or ""
    if reset_at != today:
        sheets.set_control({"sent_today": "0", "date_reset_at": today})
        ctrl["sent_today"] = "0"
        ctrl["date_reset_at"] = today
        print(f"Daily cap reset (was {reset_at!r}, now {today!r}).")
    return ctrl


def _maybe_suppress(sheets: SheetsClient, leads: list[dict]) -> list[dict]:
    suppressed = []
    for lead in leads:
        sup = enforce_suppression(lead)
        if sup is not None:
            suppressed.append(sup)
    if suppressed:
        sheets.batch_update_leads(suppressed)
    return suppressed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NyayaWorks outreach cron run")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline except actual Resend email dispatch.",
    )
    args = parser.parse_args(argv)
    dry_run = args.dry_run

    _preflight_credentials()

    sheets = GspreadClient()
    provider = ResendProvider()

    # Bootstrap tabs + headers + control defaults if missing.
    sheets.setup_sheet()

    if not _acquire_lock(sheets):
        return 0  # clean exit; another run owns the lock

    try:
        ctrl = sheets.get_control()
        leads = sheets.get_leads()

        # Re-check suppression on every run before anything else.
        _maybe_suppress(sheets, leads)
        leads = sheets.get_leads()

        # Step 2: sync status (Resend event polling & IMAP replies).
        changed = sync_status(leads, provider, sheets=sheets)
        if changed:
            sheets.batch_update_leads(changed)
            leads = sheets.get_leads()

        # Process ONE lead transition.
        ctrl = _maybe_reset_daily_cap(sheets, ctrl)
        daily_cap = int(ctrl.get("daily_cap", 0) or 0)
        sent_today = int(ctrl.get("sent_today", 0) or 0)
        if sent_today >= daily_cap:
            print(f"Daily cap reached ({sent_today}/{daily_cap}). No lead this run.")
            return 0

        if not _window_open(ctrl):
            print("Outside send window. No lead this run.")
            return 0

        model = research.resolve_model(research._get_client())
        lead = get_next_lead(leads)
        if lead is None:
            print("No actionable lead. Nothing to do.")
            return 0

        updated = transition(
            lead,
            provider,
            model=model,
            dry_run=dry_run,
            sheets=sheets,
        )
        sheets.batch_update_leads([updated])

        if updated.get("state") == config.STATE_SENT and not dry_run:
            sheets.set_control({"sent_today": sent_today + 1})
        return 0
    except Exception as exc:  # noqa: BLE001
        # Surface, never swallow silently.
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            _release_lock(sheets)
        except Exception:  # noqa: BLE001, S110
            # Never let a lock-release failure mask the original error.
            pass


if __name__ == "__main__":
    raise SystemExit(main())