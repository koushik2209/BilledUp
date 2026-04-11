# BilledUp

AI-powered GST billing for Indian shops via WhatsApp.
Bill smarter. Grow faster.

## What it does
Shopkeeper types a message in Telugu/Hindi/English on WhatsApp.
BilledUp parses it with Claude, looks up HSN/GST rates, generates a legally
valid GST bill PDF (Tax Invoice, Bill of Supply, or Credit Note), and sends
it back — all in under 10 seconds.

## Setup

1. Clone this repo
2. Install dependencies:
   `pip install -r requirements.txt`
3. Copy environment file:
   `copy .env.example .env`
4. Fill in `.env`:
   - `ANTHROPIC_API_KEY` — Claude API key
   - `DATABASE_URL` — `sqlite:///billedup.db` for local, `postgresql://...` for prod
   - `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`
   - `BASE_URL` — public HTTPS URL of your webhook
5. Run locally (CLI billing loop):
   `python main.py`
6. Run webhook server:
   `python whatsapp_webhook.py` (dev) — production auto-switches to Gunicorn

## Package layout

- `main.py` — CLI billing loop + seed/demo helpers
- `whatsapp_webhook.py` — Flask app, Meta Graph API webhook, admin + REST endpoints
- `config.py` — env loading, Anthropic client singleton
- `ai/` — Claude-backed parser, sanitizer, regex fallback
- `api/` — Meta WhatsApp client, message formatters
- `core/` — billing math, invoice numbering, return detection, GST rate table, reports
- `db/` — SQLAlchemy models, session, CRUD, item master, dedup
- `services/` — router (state machine), billing flow, PDF renderer, pending bills, registration
- `tests/` — pytest suite

Backward-compatibility shims at the repo root (`database.py`, `claude_parser.py`,
`bill_generator.py`, `gst_rates.py`, `reports.py`, `return_detector.py`,
`whatsapp_client.py`) re-export from the new packages.

## Tech stack
- Python 3.11
- Claude API (Anthropic) — `claude-sonnet-4-*`
- Flask + Gunicorn (gthread, 4 workers × 2 threads)
- SQLAlchemy 2.x — SQLite for dev, PostgreSQL for prod
- ReportLab — in-memory PDF generation (stored as `LargeBinary`)
- rapidfuzz — fuzzy matching for GST rates, states, return intent
- Meta WhatsApp Cloud API (Graph API v22)

## Docs
See `PROJECT_CONTEXT.md` for the full architecture map and `memory.md` for
design decisions, assumptions, and gotchas.

## Disclaimer
HSN codes and GST rates are best-effort. Always verify with a CA before
filing GST returns.
