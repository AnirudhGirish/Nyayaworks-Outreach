# NyayaWorks Outreach Engine

A fault-tolerant, stateless cold-outreach automation engine for NyayaOS. The system autonomously researches legal professionals via targeted web retrieval, generates personalized communications using large language models, enforces structural and compliance guardrails, renders high-deliverability Primary-First HTML emails with plain-text fallbacks, and manages delivery via the official Resend API with IMAP reply tracking.

The architecture uses Google Sheets as a centralized, transactional state machine and source of truth. Designed to execute within a serverless or scheduled container environment (such as Railway Cron), the engine is fully idempotent: crashed or interrupted runs resume safely on subsequent executions without state drift or duplicate dispatches.

---

## 1. Executive Summary & Design Rationale

### 1.1 Architectural Guarantees
- **Idempotency & State Recovery:** Execution state resides entirely within external storage (Google Sheets). The application process retains no local memory between invocations. Every Resend API dispatch includes an explicit `Idempotency-Key` (`{row_id}_{attempts}`) to prevent double-sends even if a network timeout occurs after delivery but prior to state persistence.
- **Strict Row-Sequential Vertical Processing:** Leads advance vertically row-by-row through their full lifecycle before subsequent rows are picked up (`NEW` -> `RESEARCHED` -> `DRAFTED` -> `QUEUED` -> `SENT`). This prevents resource starvation on in-flight leads, ensures daily send capacities directly translate to completed communications rather than stranded drafts, and maintains deterministic ordering across execution ticks.
- **Double-Gated Delivery & HTML Security:** Every draft must pass automated validation checks (length, compliance, banned term checks) both at the drafting stage and at the literal millisecond prior to dispatch. All AI-generated text and web search data are strictly HTML-escaped (`html.escape()`) to eliminate XSS / prompt injection risks.
- **Zero Lock-Contention Leaks:** System locks are bound by Time-To-Live (TTL) expiration timestamps. Dead containers cannot permanently lock the pipeline; lock release is guaranteed via isolated execution blocks.

### 1.2 Architectural Evolution & Key Decisions
- **Transport Layer Evolution (SMTP $\rightarrow$ Resend HTTPS API):** The engine originally sent via raw SSL SMTP (`smtpout.secureserver.net:465`) and parsed MIME bounce reports via RFC 3464. To eliminate socket timeout risks, avoid non-standard provider bounce formats, and achieve native idempotency, the system migrated to Resend's HTTPS API (`ResendProvider`) via the official `resend` Python SDK, with `Idempotency-Key: {row_id}_{attempts}` headers and direct event status polling (`delivered`, `bounced`, `complained`, `delivery_delayed`, `suppressed`).
- **Warmup System Lifecycle (Peer-to-Peer Warmup $\rightarrow$ Lean Engine):** Early pipeline versions included an automated peer-to-peer warmup module (`warmup.py`, `warmup_peers` tab, ramp gates) scripting two-way AI conversations. Once initial domain reputation was established and volume stabilized, peer warmup was removed in full to maintain a lean, production-only pipeline. *(Note: legacy spreadsheet columns or `warmup_peers` tabs in existing Google Sheets are safely ignored by the active codebase).*
- **Domain Strategy & Custom Reply-To:** Dispatches originate from a dedicated outreach address (`founder@reach.nyayaworks.in`) with custom `Reply-To` routing (`founder@nyayaworks.in`). Dedicated SPF/DKIM/DMARC alignment on `reach.nyayaworks.in` insulates root domain reputation while routing human replies directly to Titan IMAP (`founder@nyayaworks.in`) for inbox polling.
- **Minimalist Primary-First HTML Engine (`template.py`):** Designed specifically to evade Gmail and Outlook "Promotions" tab algorithms. Stripped heavy outer table wrappers, card borders, and hero header banners in favor of native system typography (`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto...`), comfortable line height (`1.6`), high-contrast text (`#1e293b`), inline text links instead of styled CTA buttons, and 1:1 plain-text fallback alignment.
- **Google Sheets as a Database:** Provides a zero-infrastructure, real-time administrative control board. It allows non-technical operators to review drafts, mark suppression flags (`do_not_contact`), or alter volume thresholds without redeploying code or accessing database terminals.
- **Single-Lead Processing per Cron Tick:** Invoking short, high-frequency execution cycles (e.g., every 30 minutes) that process a single lead transition guarantees short execution times, avoids container timeout limits, prevents rate-limit spikes on external APIs, and isolates failures to individual records.
- **Dynamic Model Resolution:** Model strings are not hardcoded. At runtime, the system queries the live Anthropic API endpoint (`models.list()`) to resolve the latest active Claude 3.5 Sonnet variant. On network or authorization failure, the system raises a loud exception to record in the control log rather than falling back silently to a deprecated model string.

---

## 2. System Architecture & Flow Control

### 2.1 Macro Lifecycle Flow

```
Railway Cron Schedule (UTC: 0,30 3-12 * * 1-6)
│
├──► Preflight Check (Verify RESEND_API_KEY, ANTHROPIC_API_KEY, SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON)
│
├──► Lock Acquisition (control.is_locked check & TTL evaluation)
│      ├── Lock Active & Unexpired (<15m) ──► Exit (Code 0)
│      └── Lock Free or Expired           ──► Acquire Lock (Write ISO UTC timestamp)
│
├──► Status & Inbox Synchronization (sync_status)
│      ├── Resend API Event Polling (delivered, bounced, complained, suppressed)
│      └── IMAP Reply Monitoring (Inbound emails from leads ──► Update SENT → REPLIED)
│
├──► Daily Cap & Send Window Evaluation
│      ├── Cap Reached / Window Closed ──► Release Lock ──► Exit (Code 0)
│      └── Cap & Window Open           ──► Process Next Actionable Lead
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
| `QUEUED` | Scheduled Run | Live DNC refetch, duplicate check, HTML rendering, Resend dispatch | `SENT` |
| `SENT` | Sync Status | Resend event polling (`bounced`/`complained`/`suppressed`) or IMAP reply check | `BOUNCED` / `REPLIED` |
| `SUPPRESSED` | Any Point | Forced terminal state if `do_not_contact == TRUE` | Terminal |
| `FAILED` | Any Point | Reached `MAX_ATTEMPTS` (3) due to persistent errors | Terminal |

---

## 3. Resilience, Error Boundaries & Safety Mechanisms

### 3.1 Network Timeout & Retry Matrix

All external network operations enforce strict HTTP and socket timeouts to prevent process hanging.

| Boundary | Interface | Timeout | Failure Behavior |
|---|---|---|---|
| **Google Sheets API** | `gspread` Client (`gc.set_timeout`) | 30 seconds | Trapped by execution boundary; logged to `control.last_error`. Container exits cleanly. |
| **LLM Research & Draft** | Anthropic Client SDK | 60 seconds | Exception caught. Lead `attempts` counter incremented. Message logged to lead `error_log`. |
| **Resend API Dispatch** | Resend Python SDK | 30 seconds | Email retained in `QUEUED` state. `attempts` counter incremented. Error logged. |
| **IMAP Reply Sync** | `imaplib.IMAP4_SSL` | 30 seconds | Failure logged to `control.last_error`. Non-fatal; execution proceeds to lead processing. |

### 3.2 Last-Millisecond Do-Not-Contact (DNC) Gate
To eliminate the risk of emailing a recipient who unsubscribed or was manually suppressed while a draft was pending in the queue:
1. When a lead reaches the `QUEUED` state and is selected for dispatch, the system bypasses in-memory caches and re-fetches the fresh row directly from the Google Sheet (`_refetch_lead`).
2. If `do_not_contact` has been set to `TRUE`, the transmission is aborted instantly, and the record transitions directly to `SUPPRESSED`.

### 3.3 Duplicate Email Prevention
Before any message is passed to Resend, the state machine executes an explicit search across the entire dataset (`_has_duplicate_sent`). If another record with an identical email address exists in state `SENT`, `REPLIED`, or `BOUNCED`, the current record is immediately marked `FAILED` with a duplicate warning in the error log.

### 3.4 HTML Escaping & Compliance Footer
To ensure safe rendering across all web and mobile email clients:
- All dynamic subject lines, lead names, firm names, and AI-generated paragraphs are strictly HTML-escaped (`html.escape()`).
- Unsubscribe links (`https://nyayaworks.in/unsubscribe?email={escaped_email}`) and subtle brand footers (`NyayaOS | Legal Operating Infrastructure`) are deterministically rendered into primary-first container structures.
- Resend dispatches both rendered HTML and raw plain-text fallback as `multipart/alternative`.

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
├── .github/
│   └── workflows/
│       └── ci.yml         # GitHub Actions CI workflow (Python 3.12, ruff, mypy, bandit, pytest)
├── Procfile               # Railway process definition (worker: python run.py)
├── run.py                 # Stateless entrypoint (preflight, lock, sync, transition, unlock)
├── state_machine.py       # Sequential state transitions, lead selection, and Resend polling
├── template.py            # Primary-First HTML email template & plain-text fallback renderer
├── research.py            # Deep web research and personalization drafting engines
├── guardrails.py          # Deterministic draft validation (word counts, compliance, banned terms)
├── providers.py           # Resend API client abstraction & IMAP reply fetcher
├── sheets.py              # Batched gspread network client wrapper with configurable timeouts
├── config.py              # Environment variables, prompt definitions, and system constants
├── verify_run.py          # Non-destructive local diagnostic script
├── requirements.txt       # Production dependency manifest
├── nyayaworks-reach.md    # Comprehensive engineering spec and historical record
└── tests/
    └── test_pipeline.py   # Comprehensive test suite (unit, integration, and regression)
```

---

## 5. Google Sheet Data Schema

The database contains two active operational worksheets (headers in Row 1):

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
| `provider_message_id` | String | Resend email ID (UUID) |
| `sent_at` | ISO Timestamp | UTC timestamp of successful Resend dispatch |
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
| `last_error` | System-level error message display for administrative visibility |

---

## 6. Environment Configuration

All sensitive parameters are populated via environment variables.

```bash
# Google Sheets Integration
GOOGLE_SERVICE_ACCOUNT_JSON='{"type": "service_account", ...}'
SHEET_ID="your_google_sheet_id_here"

# Resend API Transport
RESEND_API_KEY="re_123456789..."

# Inbound Reply Monitoring (IMAP - SSL Only)
IMAP_HOST="imap.secureserver.net"
IMAP_PORT=993
EMAIL_USER="founder@nyayaworks.in"
EMAIL_PASSWORD="your_secure_app_password"
FROM_ADDRESS="founder@reach.nyayaworks.in"
FROM_NAME="NyayaOS"

# AI Core
ANTHROPIC_API_KEY="sk-ant-api03-..."
ANTHROPIC_MODEL="claude-3-5-sonnet-latest"

# Operational Tuning & Network Timeouts (Seconds / Limits)
LOCK_TTL_MINUTES=15
BODY_WORD_CAP=120
MAX_ATTEMPTS=3
HTTP_TIMEOUT=30
IMAP_TIMEOUT=30
ANTHROPIC_TIMEOUT=60
SHEETS_TIMEOUT=30
PYTHONUNBUFFERED=1
```

---

## 7. Diagnostics, Verification & Test Suite

### 7.1 Running the Automated Test Suite

The test suite contains unit, integration, security, and discriminating regression tests. External network operations are fully mocked.

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

To execute the end-to-end pipeline locally (processing research, LLM copy generation, HTML rendering, and guardrail validation) while mocking final Resend dispatch and Sheet mutations:

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

1. **`attempts` Counter Behavior:** `attempts` increments on every transition attempt. A lead at state `QUEUED` has `attempts = 3`. If a single transient network error occurs during Resend dispatch, `attempts` reaches 4 ($\ge \text{MAX\_ATTEMPTS}=3$) and the lead transitions to `FAILED`. Resolution: manual reset of `state = NEW` and `attempts = 0` in Google Sheets.
2. **Partial Failure Duplicate Send Window:** If Resend API dispatch succeeds but the subsequent Google Sheets write (`batch_update_leads`) fails, the lead remains `QUEUED` in storage. The next run re-dispatches to the recipient (mitigated by Resend's `Idempotency-Key` header).
3. **Extended Upstream Outage Progression:** During multi-hour API outages, consecutive cron runs will attempt active leads until `MAX_ATTEMPTS` is reached. Leads will require manual state reset after service restoration.
4. **Google Sheet Cleanup Note:** Legacy columns (`warmup_phase`, `warmup_started_at`) or `warmup_peers` tabs in existing spreadsheets physically remain until manually deleted by the user, but are completely ignored by the active codebase.
