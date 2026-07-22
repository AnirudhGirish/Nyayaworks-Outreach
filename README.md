# NyayaWorks Outreach Pipeline

A stateless, fully-automated cold-email outreach engine for NyayaOS. It
researches legal professionals (Anthropic web search), drafts hyper-personalized
emails, and delivers them via raw SSL SMTP/IMAP on GoDaddy Professional Email
(Titan). A Railway Cron Job runs `run.py` every 30 minutes; the Google Sheet is
the single source of truth, so a crashed run simply retries next tick — nothing
is lost or double-sent.

## Architecture

```
Railway Cron Job (every 30 min, 9am–6pm IST, Mon–Sat)
  → run.py (single stateless invocation, exits when done)
       1. check control.is_locked (exit if locked < 15 min old)
       2. sync_status()  — IMAP poll for bounces/replies (RFC 3464)
       3. warmup_phase == active  → ONE warmup step, no cold leads
          warmup_phase == complete → daily cap check → ONE lead transition
       4. clear control.is_locked
```

| File | Responsibility |
|---|---|
| `run.py` | Stateless entrypoint: lock → sync → warmup-or-lead → unlock |
| `sheets.py` | Batched gspread I/O for `leads` / `control` / `warmup_peers` |
| `providers.py` | `SendProvider` ABC + `SMTPProvider` (SSL-only SMTP/IMAP) |
| `bounce_detection.py` | RFC 3464 MIME-aware bounce / reply classification |
| `guardrails.py` | Deterministic send gates (§7) as pure functions |
| `research.py` / `draft.py` | Anthropic research + drafting (native web search) |
| `warmup.py` | Gated two-way warmup conversation with peer mailboxes |
| `state_machine.py` | One-stage-per-run transitions + suppression handling |
| `config.py` | Env vars, spam list, email template, product facts |

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in every value
pytest -q              # full suite, no network (mocks)
python run.py --dry-run   # exercises the whole flow; skips the actual send
```

## Google Sheet schema

Three tabs must exist (headers in row 1):

- **leads**: `row_id, name, firm_name, type, website, email, source, state,
  research_json, draft_subject, draft_body, provider_message_id, sent_at,
  last_updated_at, attempts, error_log, do_not_contact`
- **control**: `daily_cap, sent_today, date_reset_at, send_window_start,
  send_window_end, is_locked, warmup_phase, warmup_started_at,
  warmup_daily_target` — header in row 1, values in row 2.
- **warmup_peers**: `peer_email, imap_host, app_password_env_var,
  last_sent_at, last_received_at`

Share the Sheet with the service-account email (Editor) and your own account.

## Railway deployment

1. Create the GitHub repo and connect it to a new Railway project.
2. **Settings → Cron Schedule**: `0,30 3-12 * * 1-6`
   (Railway cron is UTC; this covers 9am–6pm IST, Mon–Sat).
3. **Settings → Start Command**: `python run.py`
4. **Variables**: paste every value from `.env` (grouped below). Never commit
   secrets — the repo's `.gitignore` excludes `.env` and `*.json`.
5. **Billing → Spending Cap**: turn it **ON**. This is the hard ceiling that
   stops any runaway bug from over-billing.

### Required environment variables (grouped by module)

**Google Sheets**
- `GOOGLE_SERVICE_ACCOUNT_JSON` — the service-account JSON as a one-line string
- `SHEET_ID` — the master Sheet ID

**SMTP / IMAP (Titan)**
- `SMTP_HOST`, `SMTP_PORT`, `IMAP_HOST`, `IMAP_PORT`
- `EMAIL_USER`, `EMAIL_PASSWORD` (app password if 2FA is on)
- `FROM_ADDRESS`, `FROM_NAME`

**Anthropic**
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_MODEL` (optional; resolved live against the model list)

**Warmup peers**
- `WARMUP_PEER_1_APP_PASSWORD`, `WARMUP_PEER_2_APP_PASSWORD` (app passwords)

**Tuning (optional)**
- `LOCK_TTL_MINUTES`, `BODY_WORD_CAP`, `MAX_ATTEMPTS`

## Raising the daily cap safely (§10)

`control.daily_cap` starts at 5. Only raise it (edit the cell) after a week of
sending during which the bounce rate stayed under ~2–3% and inbox placement
looked healthy. Because the cap lives in the Sheet, even a misconfigured cron
cannot exceed it — the gate is enforced in data, not code.

## Non-negotiables honored

- No silent failures: every error is written to `error_log`.
- No fabricated content reaches a send — the guardrail engine blocks it.
- All socket connections use `ssl.create_default_context()`; no plaintext.
- The lock is enforced so two overlapping runs can never double-send.
- No cold lead is processed while `warmup_phase == active`.
- No secrets are committed to the repo.
