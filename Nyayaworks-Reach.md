# NyayaWorks Outreach Pipeline — Engineering Spec

## 0. Setup Walkthrough — exactly what to do, in order, before the coding agent starts

Do these six things yourself first. None of them require code — they're
account/DNS/config setup. The coding agent builds against what you set up here.

### 0a. Google Sheets + Google Cloud

1. Create the Sheet. Add three tabs: `leads` (schema in §4), `control`
   (schema in §4/§8), and `warmup_peers` (schema in §8).
2. Go to console.cloud.google.com → create a new project (e.g. `nyayaworks-outreach`).
3. In the search bar at the top, search "Google Sheets API" → click **Enable**.
4. Go to **IAM & Admin → Service Accounts → Create Service Account**. Name it
   anything (e.g. `sheets-bot`). No project-level roles are needed — skip that step.
5. Open the new service account → **Keys** tab → **Add Key → Create new key → JSON**.
   This downloads a `.json` file. This *is* the credential the pipeline uses —
   keep it, don't lose it, don't put it in git.
6. Copy the service account's email (looks like
   `sheets-bot@nyayaworks-outreach.iam.gserviceaccount.com`).
7. Open your Sheet → **Share** → paste that email in → give it **Editor** access.
8. Also add your partner's real Google account as a normal human **Editor** —
   unrelated to the step above, just the usual "Share" flow.

### 0b. GoDaddy

Confirmed from your screenshots: you're on **GoDaddy Professional Email,
powered by Titan** (not Microsoft 365 — the "Manage email users" panel and
lack of any Outlook/Microsoft branding confirms this). Exact server settings:

- **SMTP (sending)**: `smtpout.secureserver.net`, port `465`, SSL
- **IMAP (reading — for bounce/reply polling)**: `imap.secureserver.net`,
  port `993`, SSL

**On the subdomain question — here's exactly how it works and the decision
to make:**

An email address needs an actual *mailbox* (a paid account with its own
login/storage) to send and receive from, plus its own SPF/DKIM/DMARC records
authenticating that specific hostname. A subdomain doesn't inherit any of
this automatically from the root domain. So `hi@reach.nyayaworks.in` is only
possible if you either buy a second GoDaddy Professional Email seat
(you saw "1 account available, Buy more" — check that price) and go through
GoDaddy's setup to host mail on that subdomain, or you skip the subdomain
and send from the mailbox you already have.

**Given this, here's my recommendation to keep you moving today:** default
to sending from your existing mailbox (`founder@nyayaworks.in`) on the root
domain. Zero extra cost, zero extra setup, and it's already yours. The
downside is that if cold sending ever gets flagged, it could in theory
affect deliverability for your other founder@ correspondence too — but
given your volume (5–10/day), the guardrails already in this spec, and the
warmup plan below, that risk is small and manageable. If you later want the
subdomain's isolation, it's a config change (one `FROM_ADDRESS` variable),
not a rebuild — set it up whenever it feels worth the extra mailbox cost.

1. Log into GoDaddy → **My Products** → find `nyayaworks.in` → **DNS** (or
   "Manage DNS").
2. Add SPF, DKIM, and DMARC records for the **root domain** (since we're
   sending from `founder@nyayaworks.in` by default). GoDaddy's Professional
   Email admin panel (under Email → Email Deliverability) generates the
   exact DKIM record for you — copy its Host/Value into the DNS page here.
3. DNS changes take up to 48 hours to fully propagate — do this today.
4. If you later add the subdomain: repeat this for `reach.nyayaworks.in`
   (or whichever you choose) once the second mailbox is set up, and update
   `FROM_ADDRESS` in Railway.

### 0c. Railway

1. Confirm your Hobby ($5/mo) plan is active, and create a GitHub repo for
   this project (Railway deploys from GitHub).
2. Create a new Railway Project, connect the GitHub repo. It'll deploy once
   the coding agent has pushed code.
3. Open the service → **Settings** tab → **Cron Schedule**. Enter a crontab
   expression. Important: **Railway evaluates cron in UTC, not IST.** 9am–6pm
   IST is roughly 3:30am–12:30pm UTC, so an expression like `0,30 3-12 * * 1-6`
   (every 30 min, 3am–1pm UTC, Mon–Sat) covers your window with a little room
   either side — double check with a cron expression validator before saving.
4. In the same Settings tab, set the **Start Command** (e.g. `python run.py`).
5. Go to the **Variables** tab and add every secret from §0a/§0d/§0f as an
   environment variable (never commit these to the repo).
6. Go to **Usage/Billing** and turn on the **spending cap** — this is the one
   setting that puts a hard ceiling on cost no matter what a bug does.
7. After the first few scheduled runs, check the **Deployments/Logs** tab to
   confirm it actually ran and the Sheet updated as expected.

### 0d. AI (Anthropic)

1. Go to console.anthropic.com → sign up / log in → set up billing.
2. **API Keys → Create Key** → copy it immediately (it's shown once) → this
   becomes the `ANTHROPIC_API_KEY` Railway variable in §0c.
3. That's it — this one key covers both the research step and the drafting
   step below. No second AI account needed.

### 0e. Research

This is simpler than it sounds — skip building a scraper entirely. Claude's
API has a **native web search tool**: you add one parameter to the same API
call (`tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]`)
and Claude searches the web itself, reads the results, and returns them —
using the same `ANTHROPIC_API_KEY` from §0d. No separate search API account,
no scraping library to maintain, no extra bill beyond a small per-search fee
(negligible at your volume). This *is* the research step — feed it the
research prompt in §6a pointed at the firm's name and website.

### 0f. Sending / delivery (SMTP, in-house — decision made)

1. Server settings (confirmed, §0b): SMTP `smtpout.secureserver.net:465`
   (SSL), IMAP `imap.secureserver.net:993` (SSL) → Railway variables
   `SMTP_HOST`, `SMTP_PORT`, `IMAP_HOST`, `IMAP_PORT`, `EMAIL_USER`,
   `EMAIL_PASSWORD` (an app-specific password if your account has 2FA).
2. **For automated warmup (§12), you need 1–2 mailboxes you control** —
   your own personal Gmail, your partner's, or similar — with IMAP access
   enabled (an app password for each). The pipeline will script both sides
   of a conversation between your sending mailbox and these peers. This
   can't be fully automated from nothing; you still have to supply the
   "other end" of the conversation. Add each as `WARMUP_PEER_1_EMAIL` /
   `WARMUP_PEER_1_APP_PASSWORD` etc. in Railway.
3. Run every lead's email through a verifier (Hunter or NeverBounce)
   *before* it's added to the Sheet — this is what keeps bounce rate low
   regardless of anything else in this spec.

Once all six are done, hand §10's master prompt to the coding agent.

## 1. Goals & Non-Goals

**Goals**
- Pick leads from a Google Sheet, research them, draft a personalized email, send it, and log status back — reliably, forever, with zero manual babysitting.
- Never send twice. Never send to someone who opted out. Never send a hallucinated claim.
- Every failure is visible and retryable, never silent.

**Non-Goals (explicitly out of scope)**
- No custom UI. The Sheet *is* the UI.
- No open-tracking pixels (unreliable since Apple MPP, and a spam signal in themselves).
- No multi-channel (LinkedIn/WhatsApp) yet — add later only if this proves out.
- No n8n / visual workflow tool — see reasoning above. This is a single, testable codebase.

## 2. Architecture

**One service. No always-on component.** On Railway's $5 Hobby plan, a container
that runs 24/7 (like a webhook receiver) burns compute every idle minute — a
minimal always-on service can eat most of your $5 credit on its own, per
Railway's own pricing docs. A Cron Job only bills for the seconds it actually
runs, so a single cron-triggered script is both simpler *and* the right
architecture for this budget — not just a compromise.

```
Railway Cron Job (every 30 min, 9am–6pm IST, Mon–Sat)
  → run.py (single stateless invocation, exits when done)
      1. sync_status()               — poll for bounces/replies on any row in
                                        SENT state (IMAP poll, or the send
                                        provider's status API), update rows
      2. check_daily_cap()           — read/increment a counter row in the sheet
      3. get_next_lead()             — pull one row in the earliest actionable state
      4. advance(lead)                — run exactly ONE stage transition, then exit
      5. write_back(lead)             — persist new state + data to the row
```

No webhooks, no always-on receiver, no second service to secure or pay for.
Bounce/reply detection is just another polling step inside the same script —
slightly less instant than a webhook, but "checked every 30 minutes" is more
than fine for cold outreach, and it keeps the entire system to one deployable
unit that either runs correctly or doesn't run at all.

Each invocation does **one stage for one lead**, then exits. This is deliberate:
a crashed long-running daemon fails silently; a cron job that fails just doesn't
advance that one lead, and the next run picks up cleanly. No internal scheduler,
no background threads, nothing that can wedge.

## 3. Tech Stack

- **Language**: Python 3.12 (matches your stack, best Sheets/LLM SDK support)
- **Sheets**: `gspread` + a Google service account (key stored as a Railway
  secret, never in the repo). Batch reads/writes — don't call the API once
  per field — to stay well clear of rate limits.
- **LLM**: `anthropic` SDK, structured JSON output (see prompts below). Don't
  hardcode a specific model string in the code — have the coding agent check
  the current model list at build time. Models get deprecated on Anthropic's
  own schedule, not yours.
- **Web fetch for research**: Claude's native `web_search` tool — no separate
  scraping library or search API needed (see §0e)
- **Sending & status**: an abstract `SendProvider` interface, two implementations:
  - `SMTPProvider` (chosen for now — raw SMTP send via `smtplib`, IMAP poll via
    `imaplib` for bounces/replies on the same inbox). **Both connections must
    use `ssl.create_default_context()`** — non-negotiable, no exceptions for
    convenience during testing.
  - `SmartleadProvider` (not built now, but the interface leaves room for it —
    if bounce-parsing edge cases ever eat real time, this is a small swap,
    not a rebuild)
- **Deploy**: Railway, **one service** — the Cron Job. Nothing always-on.
- **Tests**: `pytest`, with fixture-based tests for every stage transition

## 4. Data Model — Sheet Schema

| Column | Notes |
|---|---|
| `row_id` | stable UUID, never reused |
| `name`, `firm_name`, `type`, `website`, `email` | lead data |
| `source` | `manual` \| `scraped` |
| `state` | see state machine below |
| `research_json` | full research object, stored for audit — never re-derive silently |
| `draft_subject`, `draft_body` | stored before send, so a send failure never loses the draft |
| `provider_message_id` | for bounce/reply correlation |
| `sent_at`, `last_updated_at` | timestamps |
| `attempts` | retry counter per stage |
| `error_log` | last error message, human-readable |
| `do_not_contact` | boolean — **checked before every single stage**, independent of state |

A separate **`control`** sheet tab holds: `daily_cap`, `sent_today`, `date_reset_at`,
`send_window_start`, `send_window_end`, **`is_locked`** (a timestamp), and the
warmup fields added in §8 (`warmup_phase`, `warmup_started_at`,
`warmup_daily_target`) — so you change the ramp by editing a cell, never by
redeploying code. A third tab, `warmup_peers`, is also added per §8.

**Race-condition guard:** on wake, `run.py` checks `is_locked`. If it's set and
less than ~15 minutes old, the run exits immediately — a previous run is still
in flight (e.g. a slow API call made it run long). Otherwise it writes the
current timestamp to `is_locked` and clears it before exiting. This is what
stops two overlapping runs from grabbing the same lead and double-sending.

## 5. State Machine

```
NEW → RESEARCHED → DRAFTED → QUEUED → SENT → REPLIED
                                          ↘ BOUNCED
any state → FAILED (after N attempts; requires human review to re-queue)
any state → SUPPRESSED (terminal, if do_not_contact = true)
```

Rules:
- A row only ever moves **one stage forward per cron run**. This bounds the blast
  radius of any single bug.
- Before every transition: check `do_not_contact`. If true, force state to
  `SUPPRESSED` and stop, regardless of what state it was in.
- Before `QUEUED → SENT`: hard gate — verify the email passed the guardrail
  checks in §7. No draft reaches the send provider unvalidated.

## 6. AI Prompts (runtime — these run per-lead, not by the coding agent)

### 6a. Research prompt

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

### 6b. Drafting prompt

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

## 7. Guardrails (hard gate before send — code, not AI)

Run these checks in code on every drafted email before it can move to `QUEUED`:
- Unsubscribe link present and correct
- Body word count under the cap
- No banned phrases (spam trigger words list, maintained in config)
- `notable_fact` used in body only if `confidence == "high"` — reject and
  regenerate with fallback if this doesn't hold
- Recipient not on `do_not_contact` / suppression list (re-check even if
  already checked earlier — defense in depth)

If any check fails: state → `FAILED`, `error_log` populated, never silently
skipped or silently sent anyway.

## 8. Automated Warmup Module (best-effort, not a silver bullet)

**Be clear-eyed about what this is:** a script conversing between mailboxes
you control is a genuine, real signal to Gmail/Outlook, but it's a weaker
signal than a dedicated warmup service with thousands of seed accounts. This
gets you real warmup, not the same strength as a paid tool. If inbox
placement is still poor after this, that's the sign to reconsider Smartlead
— not a failure of this design.

**New `control` fields**: `warmup_phase` (`active` | `complete`),
`warmup_started_at`, `warmup_daily_target` (ramps 2 → 15 over ~3 weeks).

**New `warmup_peers` tab**: `peer_email`, `imap_host`, `app_password_env_var`
(name of the Railway variable, never the password itself), `last_sent_at`,
`last_received_at`.

**Mechanism**, run as an extra step inside the same `run.py` invocation:
1. While `warmup_phase == active`: **no cold leads are processed at all** —
   this is a hard gate, not a suggestion.
2. On a randomized subset of ticks, send a short, natural, non-templated
   message (generated by Claude with a distinct "warmup" system prompt —
   varied phrasing, never identical twice) from the sending mailbox to a
   randomly chosen peer.
3. On other ticks, check each peer's inbox via IMAP for an unanswered
   warmup message, and send a natural-sounding reply back — scripting both
   sides of a real conversation, with randomized delay (never instant).
4. `warmup_started_at` + elapsed days + a manual `warmup_phase = complete`
   flip (you check deliverability looks healthy first) — not just a timer
   — moves the pipeline into cold-sending mode.
5. **Warmup traffic continues indefinitely at a low trickle (1–2/day) even
   after cold sending starts** — reputation needs ongoing positive signal,
   not a one-time push.

## 9. Bounce Detection — Hardened

Bounces don't look like normal replies, and getting this wrong either hides
real delivery failures or misclassifies real human replies as bounces. On
every `sync_status()` poll:
1. Fetch new messages in the mailbox's inbox since the last poll.
2. Flag as a **candidate bounce** if the sender is `mailer-daemon@`,
   `postmaster@`, or the Content-Type is `multipart/report` /
   `message/delivery-status` (per RFC 3464) — not just a keyword match on
   the subject line, which misses most real bounce formats.
3. Parse the `message/delivery-status` part for the `Action:` field
   (`failed` = hard bounce, `delayed` = not yet a bounce, leave as SENT) and
   the original recipient address to correlate back to a lead row.
4. Correlate to a lead via the stored `provider_message_id` (match against
   `Original-Message-ID` in the delivery-status part) — fall back to
   matching the recipient's email address if the ID isn't present.
5. A message that **doesn't** match the bounce pattern above but comes from
   the lead's own email address → `REPLIED`, not `BOUNCED`. Don't guess
   intent from the reply body (e.g. don't auto-set `do_not_contact` just
   because it contains a word like "remove") — flag it for you to read and
   decide, since that nuance isn't safe to automate.

## 10. Deliverability Config

- Sending from `founder@nyayaworks.in` by default (see §0b) — SPF/DKIM/DMARC
  configured on the root domain
- Automated warmup per §8 must reach `warmup_phase = complete` before any
  cold lead is processed
- Verify emails (Hunter/NeverBounce) before they ever reach `NEW`
- `control.daily_cap` starts at 5, manually raised weekly as bounce rate
  stays under ~2–3% and inbox placement holds
- Send window: 9am–6pm IST, skip Sundays

## 11. Railway Deployment & Cost

- **One Cron Job service**, nothing else always-on. At your volume (a few
  seconds of work every 30 minutes, working hours only, 6 days a week), actual
  compute usage is a small fraction of the $5 Hobby credit — realistically
  cents per month, not dollars.
- **Set a hard spending cap.** Railway supports an opt-in spending limit —
  turn this on. It's the single best protection against any bug (a runaway
  retry loop, a misconfigured schedule) turning into a surprise bill. Without
  it, overage is billed automatically with no ceiling.
- **All secrets as Railway environment variables**: Anthropic API key, Google
  service account JSON (as a variable, not a committed file), SMTP/IMAP
  credentials, warmup peer credentials. Never in the git repo, never in
  code, never logged.
- **Check the Railway usage dashboard weekly for the first month** to confirm
  actual spend matches expectations before treating it as "set and forget."

## 12. Testing Strategy

- Unit tests for every stage function with saved fixture inputs/outputs
  (a `low confidence` research fixture, a `high confidence` one, a
  malformed-JSON-from-LLM fixture to prove the guardrail catches it)
- A `--dry-run` flag that runs the full pipeline except the actual send
  call, for safe end-to-end testing against a test sheet
- One integration test that runs the whole state machine start-to-finish
  against a mocked Sheet and mocked LLM/send provider

## 13. Master Prompt — hand this to the coding agent

```
Build the NyayaWorks outreach pipeline exactly as specified in this
document: [paste this whole spec].

Implement it as:
- run.py — single stateless entrypoint. On each run: check `control.is_locked`
  first — if set and under ~15 minutes old, exit immediately (another run is
  still in flight). Otherwise set the lock, call sync_status() (§9), then
  either run one warmup step (§8, if warmup_phase == active) OR run ONE
  stage transition for ONE lead (§2 and §5, if warmup_phase == complete),
  then clear the lock before exiting. No webhook server, no long-running
  process — this deploys as a Railway Cron Job only.
- An SMTPProvider class: SMTP via smtplib to smtpout.secureserver.net:465
  (SSL), IMAP via imaplib to imap.secureserver.net:993 (SSL) — both using
  ssl.create_default_context(), no exceptions. Build it behind a
  SendProvider abstract interface so a future provider can be swapped in
  without touching business logic, but only implement SMTPProvider now.
- bounce_detection.py — implement §9 exactly: MIME-aware parsing of
  multipart/report and message/delivery-status parts per RFC 3464, not
  keyword-matching on subject lines. Include unit tests with real sample
  bounce MIME messages as fixtures.
- warmup.py — implement §8: gated hard-stop on cold-lead processing while
  warmup_phase == active, scripted two-way conversation with peer mailboxes
  using varied AI-generated content, randomized send/reply delay.
- sheets.py — all Google Sheets I/O, schema per §4 plus the warmup_peers
  tab and control fields in §8, using batched reads/writes rather than one
  API call per field
- research.py, draft.py — LLM calls using the exact prompts in §6, using
  whichever current Claude model is available at build time (don't hardcode
  a version), parsing/validating JSON output (raise, don't guess, if it
  doesn't match the schema)
- guardrails.py — every check in §7, as pure functions with unit tests
- Full pytest suite per §12 before considering this done
- A README covering local dev, the Railway Cron Job setup, setting the
  Railway spending cap, all required environment variables (list them
  explicitly, grouped by module), and how to raise control.daily_cap safely

Non-negotiables: no silent failures, no fabricated content ever reaches
a send call, every external call (Sheets, LLM, SMTP/IMAP) has explicit
error handling that writes to error_log rather than crashing the whole
run, no secrets ever committed to the repo, no overlapping runs (the lock
must actually be enforced), no cold lead is ever processed while
warmup_phase == active. Ask me before making any architecture decision
not covered in this spec.
```

## 14. How It All Works, End to End

**A single run, in plain terms.** Every 30 minutes during working hours,
Railway starts a short-lived container, it runs `run.py`, and the container
disappears. That one run: checks a couple of rows for bounce/reply updates,
checks whether today's send cap is used up, and — if not — moves exactly one
lead one step forward (research it, or draft its email, or send it). Then it
exits. Nothing is ever "running" in the background between ticks. If Railway
itself has a bad five minutes, you simply lose one tick — nothing crashes,
nothing is lost, because the Sheet (not memory) is the only source of truth.

**Why nothing breaks.**
- Every lead's progress lives in the Sheet as an explicit state, so a crash
  mid-run just means that one row didn't advance this tick — it's retried
  next tick, never duplicated, never lost.
- A row only ever moves one step per run, so the worst a bug can do is stall
  one lead in one state, not corrupt the whole pipeline.
- A `FAILED` state (after a few retries) stops and waits for you rather than
  looping forever or sending something unvalidated.
- The daily cap is enforced in the Sheet itself, so even a scheduling bug
  that somehow ran the cron every minute couldn't blow past your ramp.

**Why nothing gets sent that shouldn't.**
- The drafting AI can only use facts you've verified are true (`product_facts`)
  and facts it actually found about the firm — never invented ones.
- A separate, deterministic code check (not the AI) gates every email before
  it can be sent: unsubscribe link present, length limit respected, no banned
  phrases, suppression list re-checked one more time right before send.
- `do_not_contact` is checked before *every* stage, not just before sending —
  so someone who opts out mid-pipeline can't accidentally get a later-stage
  email anyway.

**Why it's secure.**
- The Google service account can only see the one Sheet you shared with it —
  not your Drive, not your other Sheets.
- Every credential (Anthropic key, Google service account, SMTP/Smartlead
  creds) lives as a Railway environment variable, never in the code or the
  git repo.
- There's no inbound webhook endpoint at all, so there's nothing on the
  public internet for anyone to probe or spoof — status updates are pulled
  by your own script, not pushed in by anyone else.
- A Railway spending cap means even a runaway bug has a hard financial
  ceiling, not an open-ended one.

**What you actually own vs. what the tool owns.** You own: the Sheet (data),
the code (git repo), the prompts (§6), and the product facts. The sending
provider owns only deliverability infrastructure (warmup, DNS, IP reputation)
and can be swapped without touching your logic, because of the SendProvider
abstraction in §3. Nothing about your lead data or your pipeline logic is
locked into any vendor.
