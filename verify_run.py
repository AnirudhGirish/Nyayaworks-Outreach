"""Pipeline Execution Flow Verification Script.

Run this script to simulate a single execution run end-to-end.
"""
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

import config
from sheets import GspreadClient
import state_machine

load_dotenv()


def verify_pipeline():
    print("=" * 60)
    print("         NYAYAWORKS OUTREACH PIPELINE DIAGNOSTIC         ")
    print("=" * 60)

    # 1. Initialize Sheets Client
    try:
        sheets = GspreadClient()
        print("✔ Sheets Client initialized successfully.")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Failed to initialize Sheets Client: {exc}")
        return

    # 2. Check Control Settings & Flags
    try:
        control = sheets.get_control()
        print("\n--- [CONTROL TAB STATUS] ---")
        print(f"Daily Cap           : {control.get('daily_cap')}")
        print(f"Sent Today          : {control.get('sent_today')}")
        print(f"Is Locked           : {control.get('is_locked')}")
        print(f"Last Error          : {control.get('last_error') or 'None'}")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Failed to read control tab: {exc}")
        return

    # 3. Verify IST Send Window
    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    print("\n--- [SEND WINDOW CHECK] ---")
    print(f"Current UTC Time    : {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Current IST Time    : {ist.strftime('%Y-%m-%d %H:%M:%S IST')} (Weekday: {ist.strftime('%A')})")
    
    is_sunday = ist.weekday() == 6
    in_hours = 9 <= ist.hour < 18
    window_open = in_hours and not is_sunday
    print(f"Send Window Open?   : {'YES' if window_open else 'NO (Outside 9am-6pm IST or Sunday)'}")

    # 4. Scan Leads Queue
    leads = sheets.get_leads()
    print("\n--- [LEADS QUEUE BREAKDOWN] ---")
    print(f"Total Leads Loaded  : {len(leads)}")
    
    states_count = {}
    for lead in leads:
        st = (lead.get("state") or "UNKNOWN").strip().upper()
        states_count[st] = states_count.get(st, 0) + 1
    
    for st, count in states_count.items():
        print(f"  • {st:<12}: {count}")

    # 5. Identify Next Candidate Lead
    next_lead = state_machine.get_next_lead(leads)
    print("\n--- [NEXT ACTIONABLE LEAD] ---")
    if not next_lead:
        print("No lead found in an actionable state (NEW, RESEARCHED, DRAFTED, QUEUED). Queue is idle.")
    else:
        current_st = (next_lead.get("state") or "").strip().upper()
        next_st = config.STATE_PROGRESSION.get(current_st, "UNKNOWN")
        print(f"Row ID              : {next_lead.get('row_id')}")
        print(f"Name / Firm         : {next_lead.get('name')} ({next_lead.get('firm_name')})")
        print(f"Email Address       : {next_lead.get('email')}")
        print(f"Current State       : {current_st}")
        print(f"Target Next State   : {next_st}")
        
        print("\n--- [PLANNED ACTION ON NEXT RUN] ---")
        if current_st == "NEW":
            print("Action: Perform web research & extract structured JSON context.")
        elif current_st == "RESEARCHED":
            print("Action: Generate draft subject & body via Anthropic LLM.")
        elif current_st == "DRAFTED":
            print("Action: Validate draft against guardrails (word count, unsubscribe, banned terms).")
        elif current_st == "QUEUED":
            print("Action: Check DNC & duplicates, then dispatch email via Resend API.")

    print("=" * 60)
    print("Diagnostic Complete.")
    print("=" * 60)


if __name__ == "__main__":
    verify_pipeline()