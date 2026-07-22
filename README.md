# NyayaWorks Outreach Engine

A fault-tolerant, stateless cold-outreach automation engine for NyayaOS. The system autonomously researches legal professionals via targeted web retrieval, generates personalized communications using large language models, enforces structural and compliance guardrails, and manages delivery via direct SSL SMTP and IMAP interfaces.

The architecture uses Google Sheets as a centralized, transactional state machine and source of truth. Designed to execute within a serverless or scheduled container environment (such as Railway Cron), the engine is fully idempotent: crashed or interrupted runs resume safely on subsequent executions without state drift or duplicate dispatches.

---

## 1. Executive Summary & Design Rationale

### 1.1 Architectural Guarantees
- **Idempotency & State Recovery:** Execution state resides entirely within external storage (Google Sheets). The application process retains no local memory between invocations. If an execution terminates unexpectedly mid-cycle, the subsequent scheduled run evaluates the current sheet state and resumes cleanly.
- **Strict Row-Sequential Vertical Processing:** Leads advance vertically row-by-row through their full lifecycle before subsequent rows are picked up (`NEW` -> `RESEARCHED` -> `DRAFTED` -> `QUEUED` -> `SENT`). This prevents resource starvation on in-flight leads, ensures daily send capacities directly translate to completed communications rather than stranded drafts, and maintains deterministic ordering across execution ticks.
- **Double-Gated Delivery:** Every draft must pass automated validation checks (length, compliance, banned term checks) both at the drafting stage and at the literal millisecond prior to SMTP socket dispatch.
- **Zero Lock-Contention Leaks:** System locks are bound by Time-To-Live (TTL) expiration timestamps. Dead containers cannot permanently lock the pipeline; lock release is guaranteed via isolated execution blocks.

### 1.2 Key Design Decisions
- **Google Sheets as a Database:** Provides a zero-infrastructure, real-time administrative control board. It allows non-technical operators to review drafts, mark suppression flags (`do_not_contact`), or alter volume thresholds without redeploying code or accessing database terminals.
- **Single-Lead Processing per Cron Tick:** Invoking short, high-frequency execution cycles (e.g., every 30 minutes) that process a single lead transition guarantees short execution times, avoids container timeout limits, prevents rate-limit spikes on external APIs, and isolates failures to individual records.
- **Dynamic Model Resolution:** Model strings are not hardcoded. At runtime, the system queries the live Anthropic API endpoint (`models.list()`) to resolve the latest active Claude 3.5 Sonnet variant. On network or authorization failure, the system raises a loud exception to record in the control log rather than falling back silently to a deprecated model string.
- **Proactive Spam Term Injection:** Banned terms defined in `config.BANNED_PHRASES` are dynamically injected into the LLM system prompt at module load time. This instructs the model to avoid spam triggers during copy generation, while `guardrails.py` maintains an independent post-generation backstop.

---

## 2. System Architecture & Flow Control

### 2.1 Macro Lifecycle Flow

```
Railway Cron Schedule (UTC)
│
├──► Preflight Check (Verify Credentials & Env)
│
├──► Lock Acquisition (control.is_locked check & TTL evaluation)
│      ├── Lock Active & Unexpired ──► Exit (Code 0)
│      └── Lock Free or Expired   ──► Acquire Lock
│
├──► Inbox Synchronization (sync_status via IMAP)
│      └── Poll RFC 3464 DSN / Non-Delivery Reports ──► Update SENT → BOUNCED / REPLIED
│
├──► Operation Phase Evaluation
│      ├── warmup_phase == active   ──► Execute One Warmup Step ──► Release Lock ──► Exit
│      └── warmup_phase == complete ──► Check Daily Sending Cap & Send Window
│                                           ├── Cap Reached / Window Closed ──► Release Lock ──► Exit
│                                           └── Cap & Window Open ──► Process Next Actionable Lead
│
└──► Lock Release (Executed inside guaranteed finally block)
```

### 2.2 Vertical State Machine Progression

Leads transition through a strictly monotonic state machine. A single lead record must progress sequentially through all intermediate states to reach completion.

```
[ NEW ] ──► [ RESEARCHED ] ──► [ DRAFTED ] ──► [ QUEUED ] ──► [ SENT ]
│               │               │               │
└───────────────┴───────────────┴───────────────┴──► [ FAILED ] / [ SUPPRESSED ]
```

| State | Trigger | Operation Performed | Next State |
|---|---|---|---|
| `NEW` | Scheduled Run | Deep web research and context extraction via LLM with web search | `RESEARCHED` |
| `RESEARCHED` | Scheduled Run | Personalized copy generation using research JSON and template | `DRAFTED` |
| `DRAFTED` | Scheduled Run | Deterministic guardrail validation (length, banned terms, unsubscribe link) | `QUEUED` |
| `QUEUED` | Scheduled Run | Live DNC refetch, duplicate email check, and SMTP dispatch | `SENT` |
| `SENT` | IMAP Sync | Asynchronous bounce or reply detection via RFC 3464 DSN parsing | `BOUNCED` / `REPLIED` |
| `SUPPRESSED` | Any Point | Forced terminal state if `do_not_contact == TRUE` | Terminal |
| `FAILED` | Any Point | Reached `MAX_ATTEMPTS` (3) due to persistent errors | Terminal |

---

## 3. Resilience, Error Boundaries & Safety Mechanisms

### 3.1 Network Timeout & Retry Matrix

All external network operations enforce strict socket and HTTP timeouts to prevent process hanging.

| Boundary | Interface | Timeout | Failure Behavior |
|---|---|---|---|
| **Google Sheets API** | `gspread` Client (`gc.set_timeout`) | 30 seconds | Trapped by execution boundary; logged to `control.last_error`. Container exits cleanly. |
| **LLM Research & Draft** | Anthropic Client SDK | 60 seconds | Exception caught. Lead `attempts` counter incremented. Message logged to lead `error_log`. |
| **SMTP Dispatch** | `smtplib.SMTP_SSL` | 30 seconds | Email retained in `QUEUED` state. `attempts` counter incremented. Error logged. |
| **IMAP Sync** | `imaplib.IMAP4_SSL` | 30 seconds | Failure logged to `control.last_error`. Non-fatal; execution proceeds to lead processing. |

### 3.2 Last-Millisecond Do-Not-Contact (DNC) Gate
To eliminate the risk of emailing a recipient who unsubscribed or was manually suppressed while a draft was pending in the queue:
1. When a lead reaches the `QUEUED` state and is selected for dispatch, the system bypasses in-memory caches and re-fetches the fresh row directly from the Google Sheet (`_refetch_lead`).
2. If `do_not_contact` has been set to `TRUE`, the transmission is aborted instantly, and the record transitions directly to `SUPPRESSED`.

### 3.3 Duplicate Email Prevention
Before any message is passed to the SMTP socket, the state machine executes an explicit search across the entire dataset (`_has_duplicate_sent`). If another record with an identical email address exists in state `SENT`, `REPLIED`, or `BOUNCED`, the current record is immediately marked `FAILED` with a duplicate warning in the error log.

### 3.4 Unsubscribe Link and Word Count Isolation
To ensure legitimate copy is not rejected due to appended metadata:
- Guardrail checks evaluate body word count on the generated core copy *prior* to appending the unsubscribe block.
- The fixed `UNSUBSCRIBE_BLOCK` is appended deterministically, URL-encoding the recipient email parameter to prevent malformed query parameters on addresses with special characters.

### 3.5 Soft-Lock Expiration (TTL)
To prevent orphaned locks caused by sudden container preemptions or infrastructure failures:
- Locks are recorded as ISO UTC timestamps in `control.is_locked`.
- Subsequent runs evaluate the lock age against `LOCK_TTL_MINUTES` (default: 15 minutes).
- If a lock exceeds the TTL, it is treated as stale, overridden, and claimed by the active process.
- Lock release is guaranteed via isolated `try...finally` blocks in `run.py`.

---

## 4. Repository Structure

```
.
├── run.py                 # Stateless entrypoint (preflight, lock, sync, transition, unlock)
├── state_machine.py       # Sequential state transitions, lead selection, and suppression
├── research.py            # Deep web research and personalization drafting engines
├── guardrails.py          # Deterministic draft validation (word counts, compliance, banned terms)
├── providers.py           # Thread-safe SSL-only SMTP and IMAP abstraction layers
├── bounce_detection.py    # Multi-part MIME and RFC 3464 DSN bounce classification engine
├── sheets.py              # Batched gspread network client wrapper with configurable timeouts
├── warmup.py              # Peer-to-peer mailbox warmup controller
├── config.py              # Environment variables, prompt definitions, and system constants
├── verify_run.py          # Non-destructive local diagnostic script
├── requirements.txt       # Production dependency manifest
└── tests/
    └── test_pipeline.py   # Comprehensive test suite (unit, integration, and regression)
```

---

## 5. Google Sheet Data Schema

The database must contain three explicit worksheets (headers in Row 1):

### 5.1 `leads` Tab
| Column Name | Type | Description |
|---|---|---|
| `row_id` | String / Integer | Unique record identifier |
| `name` | String | Target recipient full name |
| `firm_name` | String | Organization or law firm name |
| `type` | String | Firm categorization (e.g., Law Firm, Corporate) |
| `website` | String | Fully qualified target URL for research |
| `email` | String | Target email address |
| `source` | String | Lead generation source tag |
| `state` | Enum String | Current lifecycle state (`NEW`, `RESEARCHED`, `DRAFTED`, `QUEUED`, `SENT`, `BOUNCED`, `REPLIED`, `SUPPRESSED`, `FAILED`) |
| `research_json` | JSON String | Extracted firm context and structured research |
| `draft_subject` | String | Generated email subject line |
| `draft_body` | String | Generated email message body |
| `provider_message_id` | String | SMTP Provider Message-ID header value |
| `sent_at` | ISO Timestamp | UTC timestamp of successful SMTP dispatch |
| `last_updated_at` | ISO Timestamp | UTC timestamp of last state mutation |
| `attempts` | Integer | Consecutive failure count (max 3) |
| `error_log` | String | Last recorded exception or validation failure |
| `do_not_contact` | Boolean (`TRUE`/`FALSE`) | Hard suppression flag |

### 5.2 `control` Tab
Row 1 contains column headers; Row 2 contains active runtime values.

| Column Name | Description |
|---|---|
| `daily_cap` | Maximum allowed email dispatches per calendar day |
| `sent_today` | Count of emails dispatched during the current UTC date |
| `date_reset_at` | UTC date string tracking the last counter reset (`YYYY-MM-DD`) |
| `send_window_start` | Allowed send window start hour in IST (e.g., `9`) |
| `send_window_end` | Allowed send window end hour in IST (e.g., `18`) |
| `is_locked` | Empty string when free; ISO UTC timestamp when locked |
| `warmup_phase` | Operating mode: `active` (warmup only) or `complete` (production) |
| `warmup_started_at` | ISO timestamp of warmup initiation |
| `warmup_daily_target` | Targeted daily volume during warmup phase |
| `last_error` | System-level error message display for administrative visibility |

### 5.3 `warmup_peers` Tab
| Column Name | Description |
|---|---|
| `peer_email` | Email address of peer mailbox |
| `imap_host` | IMAP server host for peer mailbox |
| `app_password_env_var` | Name of environment variable holding peer authentication |
| `last_sent_at` | UTC timestamp of last exchange |
| `last_received_at` | UTC timestamp of last received reply |

---

## 6. Environment Configuration

All sensitive parameters are populated via environment variables.

```bash
# Google Sheets Integration
GOOGLE_SERVICE_ACCOUNT_JSON='{"type": "service_account", ...}'
SHEET_ID="your_google_sheet_id_here"

# SMTP & IMAP Transport (SSL Only)
SMTP_HOST="smtpout.secureserver.net"
SMTP_PORT=465
IMAP_HOST="imap.secureserver.net"
IMAP_PORT=993
EMAIL_USER="founder@nyayaworks.in"
EMAIL_PASSWORD="your_secure_app_password"
FROM_ADDRESS="founder@nyayaworks.in"
FROM_NAME="NyayaOS"

# AI Core
ANTHROPIC_API_KEY="sk-ant-api03-..."
ANTHROPIC_MODEL="claude-3-5-sonnet-latest"

# Peer Warmup App Passwords
WARMUP_PEER_1_APP_PASSWORD="peer_1_password"
WARMUP_PEER_2_APP_PASSWORD="peer_2_password"

# Operational Tuning & Network Timeouts (Seconds / Limits)
LOCK_TTL_MINUTES=15
BODY_WORD_CAP=120
MAX_ATTEMPTS=3
SHEETS_TIMEOUT=30
ANTHROPIC_TIMEOUT=60
SMTP_TIMEOUT=30
IMAP_TIMEOUT=30
PYTHONUNBUFFERED=1
```

---

## 7. Diagnostics, Verification & Test Suite

### 7.1 Running the Automated Test Suite

The test suite contains unit, integration, and discriminating regression tests. External network operations are fully mocked.

```bash
pytest
```

To run the explicit vertical priority regression test:

```bash
pytest -k test_get_next_lead_vertical_priority_regression
```

### 7.2 Executing System Diagnostics

Run the local diagnostic tool to evaluate environment variables, Google Sheets connectivity, time-window gates, and candidate lead selection without executing state mutations or transmissions:

```bash
python verify_run.py
```

### 7.3 Executing a Local Dry Run

To execute the end-to-end pipeline locally (processing research, LLM copy generation, and guardrail validation) while mocking final SMTP dispatch and Sheet mutations:

```bash
python run.py --dry-run
```

---

## 8. Deployment & Operational Guidance

### 8.1 Railway Deployment

1. Connect the repository to a Railway project.
2. Configure environment variables in the Railway dashboard using raw string values (ensure single-line formatting for `GOOGLE_SERVICE_ACCOUNT_JSON`).
3. Configure the **Cron Schedule**:
```cron
0,30 3-12 * * 1-6
```
*(Railway Cron evaluates in UTC. The schedule `0,30 3-12 * * 1-6` covers 09:00 - 18:00 IST, Monday through Saturday. The internal `_window_open` check provides a second layer of verification.)*

### 8.2 Operational Best Practices

- **Lead Placement:** Always append new lead records to the **bottom** of the `leads` tab. The selection engine processes top-to-bottom (`get_next_lead`). Appending ensures in-flight records complete before fresh rows commence.
- **Capacity Scaling:** Increase `control.daily_cap` gradually in the Google Sheet. Monitor bounce rates via the sheet's `BOUNCED` states. Maintain total bounce rates below 2.0%.
- **Handling Outages:** If an upstream network or API outage causes records to reach `FAILED` state due to attempt exhaustion, reset `state = NEW` and `attempts = 0` directly within the Google Sheet to resume processing once service is restored.

---

## 9. Known Accepted System Risks

The following technical risks have been evaluated, audited, and accepted for operational awareness:

1. **Shared Attempt Counter for Transient and Content Errors:** `attempts` increments on both network timeouts and content validation failures. Three consecutive network blips will transition a lead to `FAILED`. Resolution: manual reset of `state = NEW` and `attempts = 0` in Google Sheets.
2. **Partial Failure Duplicate Send Window:** If SMTP dispatch succeeds but the subsequent Google Sheets write (`batch_update_leads`) fails, the lead remains `QUEUED` in storage. The next run will re-dispatch to the recipient.
3. **Extended Upstream Outage Progression:** During multi-hour API outages, consecutive runs will attempt and fail active leads until `MAX_ATTEMPTS` is reached. Leads will require manual state reset after service restoration.
4. **DSN Parsing Variations:** Bounce classification is validated against standard RFC 3464 MIME structures. Non-standard provider DSN formats may fail to parse as hard bounces, leaving lead state as `SENT`.
