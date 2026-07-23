# NyayaWorks Outreach Pipeline — Engineering Spec & Architectural Record

A comprehensive, production-grade technical specification and architectural record for the NyayaWorks cold-outreach automation engine (built for NyayaOS). This document details the system's setup, data model, state machine, resilience mechanisms, and full architectural evolution—from initial design choices to current production state.

---

## 0. Setup & Infrastructure Walkthrough

The outreach engine runs autonomously as a stateless scheduled job. Before deploying, configure account credentials, domain records, and secrets in order.

### 0a. Google Sheets & Google Cloud Platform
1. **Google Sheet Setup:** Create a master Google Sheet. Header rows reside in Row 1. Required tabs:
   - `leads`: Canonical schema (§4.1).
   - `control`: System control values in Row 2 (§4.2).
2. **GCP Project Setup:** Log into `console.cloud.google.com` $\rightarrow$ Create project `nyayaworks-outreach`.
3. **Enable API:** Enable **Google Sheets API** under APIs & Services.
4. **Service Account:** Create service account `sheets-bot@nyayaworks-outreach.iam.gserviceaccount.com`. No project roles required.
5. **Key Generation:** Under **Keys** $\rightarrow$ **Add Key** $\rightarrow$ **Create new key (JSON)**. Download the JSON secret key. This value is passed directly via `GOOGLE_SERVICE_ACCOUNT_JSON`.
6. **Sheet Permissions:** Share the master Google Sheet with the service account email as **Editor**.

### 0b. Domain & Authentication Strategy (`founder@reach.nyayaworks.in`)
To protect root domain reputation while maintaining direct human email engagement, the pipeline uses a dedicated subdomain sending strategy paired with custom reply routing:

- **Sending Address:** `founder@reach.nyayaworks.in` (Outreach subdomain).
- **Reply-To Address:** `founder@nyayaworks.in` (Main inbox on Titan / GoDaddy Professional Email).
- **DNS Authentication Records (Cloudflare / GoDaddy DNS):**
  - **SPF:** `v=spf1 include:resend.com ~all` configured on `reach.nyayaworks.in`.
  - **DKIM:** Resend-generated TXT records added to `reach.nyayaworks.in` for cryptographic signing.
  - **DMARC:** `v=DMARC1; p=none; rua=mailto:dmarc@nyayaworks.in` configured on `_dmarc.reach.nyayaworks.in`.
- **IMAP Reply Monitoring:** Inbound replies to `founder@nyayaworks.in` are fetched via Titan IMAP (`imap.secureserver.net:993`, SSL-only).

### 0c. Resend API Setup
1. Log into `resend.com` $\rightarrow$ Add and verify the domain `reach.nyayaworks.in`.
2. Generate an API Key under **API Keys** with full sending permissions.
3. Save as environment variable `RESEND_API_KEY`.

### 0d. Anthropic API
1. Log into `console.anthropic.com` $\rightarrow$ Billing $\rightarrow$ API Keys $\rightarrow$ Create key.
2. Save as environment variable `ANTHROPIC_API_KEY`.
3. At runtime, `research.py` queries `client.models.list()` to resolve the active `claude-3-5-sonnet` model variant.

### 0e. Railway Deployment (Stateless Cron)
1. Connect the GitHub repository to Railway.
2. Configure environment variables in Railway project settings (never commit secrets to git).
3. **Cron Schedule:** `0,30 3-12 * * 1-6` in UTC (covers 09:00 - 18:00 IST, Monday through Saturday).
4. **Start Command:** `python run.py`.
5. **Spending Cap:** Enable spending cap under Railway Billing to enforce a hard budget ceiling.

---

## 1. Architectural Guarantees & Evolution

### 1.1 Architectural Guarantees
- **Stateless & Idempotent:** State resides entirely in Google Sheets. If a cron execution terminates unexpectedly mid-run, the next scheduled invocation evaluates Sheet state and resumes without duplication or state drift.
- **Strict Row-Sequential Vertical Processing:** Leads advance vertically row-by-row through their full lifecycle (`NEW` $\rightarrow$ `RESEARCHED` $\rightarrow$ `DRAFTED` $\rightarrow$ `QUEUED` $\rightarrow$ `SENT`) before subsequent rows are picked up. This prevents resource starvation on in-flight leads and ensures daily send capacities yield completed dispatches rather than stranded drafts.
- **Double-Gated Delivery:** Every email draft must pass automated validation checks (word count, banned phrases, unsubscribe URL, notable fact usage) both at drafting time and at the literal millisecond prior to API dispatch (`_refetch_lead`).
- **Zero Lock Contention Leaks:** System locks in `control.is_locked` use ISO UTC timestamps with a 15-minute Time-To-Live (TTL). Crashed containers cannot lock the engine permanently; lock release is guaranteed via `try...finally` blocks in `run.py`.

### 1.2 Architectural Evolution & Historical Rationale

#### Transport Layer: SMTP $\rightarrow$ Resend HTTPS API
- **Original Design:** Raw SSL SMTP via `smtplib` (`smtpout.secureserver.net:465`) and RFC 3464 MIME bounce parsing (`bounce_detection.py`).
- **Why We Refactored:** Raw SMTP connections lacked native idempotency headers, presented socket timeout risks, required complex MIME parsing for non-standard provider non-delivery reports (NDRs), and risked double-sends if network blips occurred mid-session.
- **Current Production State:** `ResendProvider` dispatches via Resend's HTTPS API using the official `resend` Python SDK. Dispatches include `options={"idempotency_key": f"{row_id}_{attempts}"}` to guarantee idempotency. Status tracking relies on Resend API event polling (`GET /emails/{id}`).

#### Warmup System Lifecycle: Peer-to-Peer Warmup $\rightarrow$ Production-Only Engine
- **Original Design:** Automated peer-to-peer warmup module (`warmup.py`, `warmup_peers` tab, ramp-up volume gates) scripting simulated two-way AI conversations.
- **Why We Refactored:** Once initial domain reputation was established and volume stabilized, peer-to-peer warmup became redundant compute overhead.
- **Current Production State:** `warmup.py` and warmup execution gates were completely removed to keep the pipeline lightweight and production-focused. Legacy `warmup_peers` tabs or unused control columns in existing spreadsheets are safely ignored by the active codebase.

#### Email Presentation: Rich Card Design $\rightarrow$ Primary-First Minimalist Engine
- **Original Design:** Styled HTML emails featuring dark header banners (`"NyayaOS | Legal Workspace"`), card borders, background colors, and styled CTA buttons.
- **Why We Refactored:** Rich template wrappers, heavy table nesting, and styled button blocks trigger Gmail and Outlook "Promotions" tab classification algorithms.
- **Current Production State:** `template.py` implements a minimalist "Primary-First" HTML engine mimicking personal 1-to-1 executive communications written in Apple Mail or Outlook. Heavy table wrappers, card borders, hero banners, and button blocks were removed in favor of system typography and subtle inline text links.

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
- Unsubscribe links (`https://nyayaworks.in/unsubscribe?email={escaped_email}`) and brand footers are deterministically rendered into responsive table-based layouts.
- Resend dispatches both rendered HTML and raw plain-text fallback as `multipart/alternative`.

---

## 4. Google Sheet Data Schema

The database contains two active operational worksheets (headers in Row 1):

### 4.1 `leads` Tab
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

### 4.2 `control` Tab
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

*(Note: Legacy spreadsheet columns such as `warmup_phase` or legacy `warmup_peers` tabs may physically exist in historical Google Sheets, but are ignored by the active codebase.)*

---

## 5. AI Prompts & Guardrails

### 5.1 Research Prompt (`research.py`)
```
You are a research assistant preparing a factual briefing note for a single
B2B outreach email. You will be given a lead's name, firm name, and website
URL, plus fetched content from that website (or search results if no
site is reachable).

RULES (do not break these):
- Use ONLY facts present in the provided source material. Never infer,
  assume, or guess. If you are not looking at explicit evidence for a
  fact, do not include it.
- If no reliable information exists for a field, output null for it —
  do not fabricate a plausible-sounding placeholder.
- "notable_fact" must be ONE specific, verifiable detail useful for a
  one-line personalization (e.g. "handles corporate M&A for mid-market
  clients in Bangalore"). Generic filler like "professional and
  experienced" is not acceptable — output null instead.

Output strictly as JSON, no other text:
{
  "firm_name": string,
  "practice_areas": string[],
  "location": string | null,
  "notable_fact": string | null,
  "confidence": "high" | "low"
}
```

### 5.2 Drafting Prompt (`research.py`)
```
You are drafting ONE cold outreach email on behalf of NyayaOS to a lawyer,
law firm, or legal council. You are given a fixed template, a research
JSON object, the recipient's name, and a short list of true facts about
NyayaOS (product_facts).

RULES (do not break these):
- Fill ONLY the marked personalization slots in the template using facts
  from research_json. Every other part of the template — subject
  structure, CTA, footer, unsubscribe link — is fixed and must not be
  altered or removed.
- If research_json.confidence is "low" or notable_fact is null, use the
  template's generic fallback slot instead of forcing a personalization.
  A generic-but-honest email beats a fabricated-but-specific one.
- Never invent case names, client names, statistics, or claims about the
  firm. Never claim anything about NyayaOS beyond product_facts.
- Tone: professional, concise, respectful of a busy legal professional's
  time. No hype, no exclamation marks, no "I hope this email finds you
  well."
- Body under 120 words.

Output strictly as JSON, no other text:
{ "subject": string, "body": string }
```

### 5.3 Deterministic Guardrails (`guardrails.py`)
Run as pure Python functions before moving a draft to `QUEUED`:
- Unsubscribe link present (`nyayaworks.in/unsubscribe`)
- Body word count under cap (`BODY_WORD_CAP = 120`, excluding mandatory unsubscribe block)
- No banned spam phrases (`config.BANNED_PHRASES`)
- `notable_fact` used in body only if `confidence == "high"`
- Recipient `do_not_contact` is `False`

---

## 6. Known Accepted System Risks & Recovery Procedures

1. **`attempts` Counter Behavior:**
   - `attempts` increments on every transition attempt (including `NEW` $\rightarrow$ `RESEARCHED`, `RESEARCHED` $\rightarrow$ `DRAFTED`, `DRAFTED` $\rightarrow$ `QUEUED`, `QUEUED` $\rightarrow$ `SENT`).
   - A lead at state `QUEUED` has `attempts = 3`. If a single transient network error occurs during Resend dispatch, `attempts` reaches 4 ($\ge \text{MAX\_ATTEMPTS}=3$) and the lead transitions to `FAILED`.
   - **Recovery Procedure:** Manually edit the Google Sheet row, setting `state = NEW` and `attempts = 0`.
2. **Partial Failure Duplicate Send Window:**
   - If Resend API dispatch succeeds but the subsequent Google Sheets write (`batch_update_leads`) fails, the lead remains `QUEUED` in storage. The next run re-dispatches to the recipient. (Mitigated by Resend's `Idempotency-Key` header).
3. **Extended Upstream Outage Progression:**
   - During multi-hour API outages, consecutive cron runs will attempt active leads until `MAX_ATTEMPTS` is reached. Leads will require manual state reset after service restoration.
