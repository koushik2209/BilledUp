# Daily Summary Feature — Design Spec
**Date:** 2026-04-22
**Project:** BilledUp

---

## Overview

Send every active shopkeeper a WhatsApp message summarising their day's billing at 9 PM IST. The same summary is also available on-demand via the `summary` WhatsApp command.

---

## Components

### 1. `core/daily_summary.py` — pure formatter (no DB, no network)

**`format_daily_summary(data: dict) -> str`**

Implements the full message spec:
- GST variant (has_gstin=True): shows sale amount + GST collected + grand total
- No-GST variant (has_gstin=False): shows grand total only
- Zero-bills variant: short motivational message, still shows month section
- Indian number formatting: <1,000 → `₹850`, 1,000–99,999 → `₹34,200`, ≥1,00,000 → lakhs with 1 decimal (e.g. `₹4.2L`), rounded not truncated
- Emoji placement per spec (📊 🧾 💰 🏛 ✅ ↩️ 🏁 📅) — no others
- Returns line shown only when returns_count > 0; Net Total shown only when returns_count > 0
- Line limit: max 18 lines; if exceeded, trim in order: month returns → month GST → month sale amount
- Net Total = max(0, grand_total - returns_amount)
- All numeric fields: missing/null treated as 0

### 2. `services/daily_summary_service.py` — data layer

**`get_daily_summary_data(shop_id: str, target_date: date) -> dict`**

Returns a dict matching the `format_daily_summary` input shape:

```python
{
    "shop_name": str,
    "has_gstin": bool,
    "date": str,           # "22 Apr 2026"
    "today": {
        "total_bills": int,       # sales + returns combined
        "grand_total": float,     # sum of grand_total for sales only
        "sale_amount": float,     # sum of subtotal for sales only (pre-GST)
        "total_gst": float,       # sum of total_gst for sales only
        "returns_count": int,
        "returns_amount": float,  # sum of abs(grand_total) for return bills
    },
    "month": {
        "name": str,              # "April"
        "total_bills": int,
        "grand_total": float,
        "sale_amount": float,
        "total_gst": float,
        "returns_count": int,
        "returns_amount": float,
    },
}
```

Queries the `bills` table using `Bill.is_return` to separate sales from returns. Month range = first day of `target_date`'s month through `target_date`.

**Returns logic:** `grand_total` and `sale_amount` include only sales bills (`is_return=False`). Returns are never subtracted from these fields. `returns_amount` is reported separately. Net Total (grand_total − returns_amount) represents actual earnings after refunds and is computed only in the formatter, not stored.

### 3. `whatsapp_webhook.py` — two additions

**Cron endpoint: `POST /api/cron/daily-summary`**

- Auth: `Authorization: Bearer <CRON_SECRET>` header; returns 401 if missing/wrong
- Queries all shops where `Registration.active = True` AND `Registration.trial_end > now`
- Date comparisons use IST throughout:
  ```python
  from datetime import datetime
  import pytz
  IST = pytz.timezone("Asia/Kolkata")
  today_ist = datetime.now(IST).date()
  ```
- Skips shops where `Shop.summary_opt_out = True`
- Skips shops where `Shop.last_summary_sent_at.astimezone(IST).date() == today_ist` (idempotent retries)
- For each qualifying shop:
  1. Call `get_daily_summary_data(shop_id, today_ist)`
  2. Skip if `today_data["total_bills"] == 0` (no activity today — do not send)
  3. Call `format_daily_summary(data)`
  4. Call `send_text_message(phone, message)` using `Registration.phone`
  5. **Only on success:** update `Shop.last_summary_sent_at = datetime.now(IST)`
  6. On failure: `log.error(f"[SUMMARY FAILED] shop={shop_id}, phone={phone}, error={e}")`, increment failed count, continue to next shop
- Adds ~150ms delay between sends to avoid Meta rate limits
- Returns 200 with a JSON count of `{ "sent": N, "skipped": N, "failed": N }`

**`summary` command in message router**

- Detected in `services/router.py` alongside existing commands (`today`, `history`, etc.)
- Calls `get_daily_summary_data(shop_id, datetime.now(IST).date())` then `format_daily_summary`
- Replies immediately — does **not** update `last_summary_sent_at` (manual ≠ scheduled)

---

## Schema Changes

Two new columns on the `shops` table:

| Column | Type | Default | Purpose |
|---|---|---|---|
| `last_summary_sent_at` | DateTime, nullable | NULL | Dedup: skip if already sent today |
| `summary_opt_out` | Boolean | False | Skip scheduled send for this shop when True |

- Add both to `_REQUIRED_SCHEMA` in `db/session.py`
- No migration needed in dev (DEV_MODE auto-resets); in prod, add columns manually or set DEV_MODE=True once

---

## Data Flow — Scheduled Send

```
cron-job.org (9 PM IST / 15:30 UTC)
    → POST /api/cron/daily-summary
        → verify CRON_SECRET
        → query active, trial-valid shops from Registration + Shop
        → today_ist = datetime.now(IST).date()
        → for each shop:
            skip if summary_opt_out
            skip if last_summary_sent_at.astimezone(IST).date() == today_ist
            get_daily_summary_data(shop_id, today_ist)
            skip if today_data["total_bills"] == 0
            format_daily_summary(data)
            send_text_message(phone, message)   ← Registration.phone, not Shop.phone
            on success → update last_summary_sent_at   ← always send before updating
            on failure → log.error("[SUMMARY FAILED] shop=..., phone=..., error=...")
                         increment failed count, continue
        → return { sent, skipped, failed }
```

## Data Flow — Manual Command

```
shopkeeper sends "summary"
    → services/router.py detects command
        → get_daily_summary_data(shop_id, datetime.now(IST).date())
        → format_daily_summary(data)
        → send reply
        (no DB write)
```

---

## Security

- `CRON_SECRET` env var — add to Railway environment variables and cron-job.org request headers
- Endpoint returns 401 (not 403) so scanners can't distinguish between "wrong secret" and "endpoint doesn't exist"
- No unauthenticated access to shop data

---

## Environment Variables (new)

| Variable | Description |
|---|---|
| `CRON_SECRET` | Bearer token checked by `/api/cron/daily-summary` |

## New Dependency

`pytz` — for IST timezone handling. Add `pytz>=2024.1` to `requirements.txt`.

---

## Testing

- Unit tests for `format_daily_summary`: GST variant, no-GST variant, zero-bills, returns present, returns absent, line-limit trimming, lakh formatting edge cases
- Unit tests for `get_daily_summary_data`: mock DB session, verify correct aggregation of sales vs returns, month boundary
- Integration test for cron endpoint: valid secret → 200, invalid secret → 401, already-sent shop skipped

---

## Out of Scope (MVP)

- Opt-out command wiring (no WhatsApp command to set `summary_opt_out` yet — must be done via DB directly)
- Per-shop send time customisation
- Retry queue for failed sends
- Weekly/monthly scheduled summaries
