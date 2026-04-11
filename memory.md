# BilledUp — Long-Term Memory

## Key Design Decisions

1. **Claude for NLP, not rules**: The core value prop is that shopkeepers type in natural language (including Telugu/Hindi). Claude extracts structured item data. A regex fallback exists but is secondary (confidence capped at 0.6).

2. **5-step GST rate resolution + price slabs + confidence**: Exact match → word-boundary → fuzzy (rapidfuzz, threshold 75) → JSON cache → Claude API. Each step returns `source` + `confidence` (high/medium/low). After lookup, `adjust_gst_for_price()` applies price-based slabs: clothing ≤₹1000 → 5%, >₹1000 → 12%; footwear ≤₹1000 → 5%, >₹1000 → 18%. Manual overrides never overridden. Preview shows: high=clean, medium=`~` marker, low=`⚠️` + "GST assumed" warning. CLOTHING_KEYWORDS and FOOTWEAR_KEYWORDS expanded (hoodie, top, skirt, blazer, jogger, shoe, sandals, loafer, etc.).

3. **DB-backed invoice sequences**: Invoice numbers use `InvoiceSequence` table with thread lock + `WITH FOR UPDATE` row lock. This replaced a JSON file approach to survive redeploys.

   3b. **Startup schema validation**: `ensure_schema(dev_mode)` runs after `init_database()` at module-level startup. Uses SQLAlchemy `inspect` to verify required tables/columns exist (defined in `_REQUIRED_SCHEMA` dict). `DEV_MODE=True` → auto-reset (drop all + delete SQLite file + recreate). `DEV_MODE=False` → log warnings only. `reset_database()` available for manual use. Root cause: `create_all()` creates missing tables but does NOT add missing columns to existing tables. No Alembic — this is the MVP workaround.

4. **SQLAlchemy dual-database**: SQLite locally, PostgreSQL in production. `DATABASE_URL` env var switches between them. Tests use a temp SQLite file (see `conftest.py`).

5. **Lazy Anthropic client**: Singleton created on first use (`get_anthropic_client()`). WhatsApp sends go through `whatsapp_client.py` (Meta Graph API) using env `WHATSAPP_*`.

6. **Conversation state machine**: Registration is a multi-step flow (NEW → ASKED_NAME → ASKED_ADDRESS → ASKED_GSTIN → ASKED_STATE → ACTIVE → EXPIRED). State stored in `Registration.state` column. Lives in `services/router.py`. The `ASKED_STATE` step resolves user input via exact code → exact/partial name → rapidfuzz WRatio (≥60). **Unresolvable input re-prompts** — it does not fall back to a placeholder state_code, because a bogus code would silently break `is_intra_state()` and force IGST on every bill. The "Other states" menu index is derived from `len(_STATE_MENU) + 1` (in `api/formatters.py` and `services/router.py`), not hardcoded.

7. **GST Report system**: `reports.py` handles monthly and date-range GST summaries. WhatsApp command `gst report [range]` triggers DB aggregation → WhatsApp text + PDF. Supports: empty (current month), "last N days", "last month", "this month", month names. PDF generated via ReportLab into BytesIO, stored as `LargeBinary` in `ReportPDF` table. Indian number formatting (lakh/crore system). `GSTReport` dataclass holds all fields.

8. **Bill preview/confirmation flow**: After parsing, bills are NOT generated immediately. A preview is shown with items, customer name, and tax type. User must reply YES to confirm. Can modify name (`NAME Ravi`), state (`STATE` → state selection sub-flow), re-enter items (`EDIT`), or `CANCEL`. Pending bills stored in DB (`PendingBillRecord` table, keyed by phone, 10-minute expiry) — safe across multiple gunicorn workers. Commands like `help`, `today`, `history` still work during confirmation mode. **Natural correction**: if user sends a new item-like message while a pending bill exists, it's auto-parsed and replaces the pending bill (no need to EDIT first). Credit note previews show a minimal command list (YES/EDIT/CANCEL only).

8. **TAX INVOICE vs BILL OF SUPPLY**: If shop has valid GSTIN (regex-validated) → TAX INVOICE with full CGST/SGST breakdown. If placeholder/empty/invalid → BILL OF SUPPLY with no tax columns in PDF. Controlled by `ShopProfile.has_gstin` property.

9. **Return / Credit Note support**: `return_detector.py` detects return intent via 3-tier rule-based logic (keyword regex → rapidfuzz partial match → majority-negative prices). No external API calls. Credit notes use `CN-` prefixed invoice numbers with separate DB sequence (avoids gaps in regular invoices). `PendingBill.is_return` flag flows through preview, confirmation, PDF generation, and DB storage. `Bill.is_return` column in database. PDF shows "CREDIT NOTE" header. All amounts negated after `calculate_bill` (which always works with positive values internally). Preview and summary show "REFUND" label and negative amounts.

---

## Important Assumptions

- **IGST support**: If `customer.state_code` differs from `shop.state_code` → inter-state → full GST applied as IGST (no CGST/SGST). If same or missing → intra-state → CGST+SGST. Determination via `is_intra_state()` in `core/billing.py`. Default state is Telangana (code 36).
- **Prices are pre-GST**: The Claude prompt instructs that all prices are before GST. GST is calculated on top.
- **Quantity defaults to 1**: If not mentioned in the message, qty = 1.
- **Customer defaults to "Customer"**: If no name is found in the message.
- **Phone number detection**: Prices that look like 10-digit phone numbers (9B–9.99B range) are skipped. This check runs before the MAX_PRICE check to avoid false triggers.
- **GSTIN placeholder**: `GSTIN00000000000` is used for unregistered shops — treated as "no GSTIN".
- **MIN_PRICE enforcement**: Items with price < 0.01 (MIN_PRICE) are rejected, not just price <= 0.
- **Datetime consistency**: `main.py` uses `datetime.utcnow()` (naive) consistently with model defaults. Session date filters use UTC.

---

## Critical Logic Explanations

### Bill Calculation (`core/billing.py:calculate_bill`)
- Accepts `shop_state_code` and `customer_state_code` params to determine tax type
- For each item: `amount = qty × price`, `gst_amount = amount × rate / 100`
  - **Intra-state**: `cgst = gst_amount / 2`, `sgst = gst_amount - cgst`, `igst = 0`
  - **Inter-state**: `cgst = 0`, `sgst = 0`, `igst = gst_amount`
- `total = amount + gst_amount` (same regardless of tax type)
- Grand total = subtotal + total_gst (sum of CGST + SGST + IGST)
- All amounts rounded to 2 decimal places at each step
- `BillResult.is_igst` flag drives PDF layout and WhatsApp message formatting

### Message Parsing (`ai/parser.py:parse_message`)
- Sanitizes input (control chars, prompt injection patterns, length limit 1000 chars)
- Checks rate limiter (100 calls/60s sliding window)
- Sends to Claude with structured system prompt expecting JSON output
- Retries on 429 (rate limit) and 529 (overloaded) with delays [2, 5, 10, 20]s
- Falls back to regex parser if Claude fails or returns invalid JSON
- Validates response: cleans names, clamps confidence 0–1, skips invalid items

### Regex Fallback Parser (`ai/regex_parser.py:_regex_parse_message`)
- Handles patterns: `item price`, `item-price`, `item x2 price`, `item 3x price`, `3 item price`
- Uses span tracking to prevent double-matching
- Extracts customer name from "bill for X" / "for X" / "to X" prefixes
- Deduplicates items by (name, price) key

### GST Smart Lookup (`core/gst_rates.py:get_gst_rate_smart`)
- 200+ hardcoded items with HSN codes across 12 categories
- Fuzzy matching uses rapidfuzz `WRatio` scorer (combines multiple strategies)
- Claude fallback asks for HSN + GST rate, validates against 5 legal slabs, caches result
- Returns `source` field: `exact`, `fuzzy`, `cache`, `claude`, `default` — used downstream to flag uncertain rates in preview

### Preview/Final Bill Rate Consistency
- GST rates resolved at preview time via `get_gst_rate_smart` with full Claude client
- Stored as `hsn`, `gst_rate`, `gst_source` in each item dict within `PendingBill.items`
- `calculate_bill()` skips GST lookup if `BillItem.hsn` is pre-filled — uses stored rates
- This ensures preview totals match final bill/PDF exactly
- User can override any item's GST rate: `GST <item#> <rate>` (e.g., `GST 1 12`)

### Orphan Confirmation Command Handling
- `_is_confirmation_command()` detects YES/CANCEL/EDIT/NAME/STATE/GST commands
- If sent with no pending bill, returns helpful "no pending bill" message
- Prevents confusing parse errors when user sends "YES" after pending expires

---

## Known Limitations

1. **No authentication on admin endpoints**: `/admin/registrations` is publicly accessible
2. **Demo shop hardcoded**: "Ravi Mobile Accessories" with GSTIN `36AABCU9603R1ZX` auto-seeded
3. **No payment integration**: Trial expiry requires manual upgrade via WhatsApp support
4. **PDF storage is in-database**: All PDFs (bills and reports) are generated in-memory via BytesIO and stored as `LargeBinary` in PostgreSQL. `Bill.pdf_data` for invoices, `ReportPDF` table for GST reports. Served via `/bills/<invoice>.pdf` and `/reports/<filename>` endpoints reading from DB. Meta document messages still need `BASE_URL` configured so these endpoints are publicly reachable.
5. **No multi-language output**: Bills are always in English. Only input supports Telugu/Hindi.
6. **HSN codes are best-effort**: Disclaimer in README — verify with CA before filing.
7. **Meta webhook verification**: GET `/webhook` uses `VERIFY_TOKEN` vs `hub.verify_token`. Optional: validate `X-Hub-Signature-256` on POST in production.
8. **`config.py` raises on import**: If `ANTHROPIC_API_KEY` is missing, import fails immediately. Tests work around this with `conftest.py` setting env vars before import.

---

## Things to Remember for Future Development

- **Adding new GST items**: Add to the `GST_RATES` dict in `core/gst_rates.py`. Format: `"item_name": {"hsn": "XXXX", "gst": N}`. More-specific compound keys must be declared *before* their short prefixes (e.g. `"tile adhesive"` before `"tile"`) because Step 1b word-boundary matching iterates in insertion order. Items found by Claude are auto-cached in `gst_cache.json`.
- **Changing Claude model**: Model is hardcoded as `claude-sonnet-4-20250514` in `ai/parser.py` and `core/gst_rates.py`. Grep for the model ID string and update both call sites.
- **Database migrations**: No Alembic. Schema changes are caught at startup by `validate_schema()`. In dev (`DEV_MODE=True`), DB auto-resets. In prod, add columns manually or set `DEV_MODE=True` once (WARNING: drops all data). `_REQUIRED_SCHEMA` dict in `db/session.py` must be updated when adding new tables/columns.
- **Invoice number format**: `{BILL_PREFIX}-{YEAR}-{SHOP_KEY}-{SEQUENCE:05d}`. Changing format requires updating `generate_invoice_number()` in `core/invoice.py`.
- **Test strategy**: `conftest.py` sets up a temp SQLite DB and fake API key before any imports. Tests avoid live API calls. Run with `pytest`.
- **Deployment**: Railway via Procfile — `gunicorn whatsapp_webhook:app` with 4 workers. Port from `$PORT` env var.
- **WhatsApp webhook URL**: Configure in Meta Developer → WhatsApp → Configuration: callback `{BASE_URL}/webhook`, verify token = `VERIFY_TOKEN`.

---

## Gotchas & Tricky Parts

1. **Import order matters**: `config.py` validates on import. If env vars aren't set, everything that imports `config` will crash. `conftest.py` must set env vars BEFORE any project imports.

2. **`db_session()` context manager auto-commits**: Any query inside `with db_session()` will auto-commit on exit. Reads and writes share the same pattern.

3. **`whatsapp_webhook.py` runs init on import**: Lines 877–878 call `init_database()` and `init_registration_tables()` at module level. This means importing the module triggers DB setup.

4. **Bill items are not mutated in-place**: `calculate_bill()` creates new `BillItem` objects with computed fields. The original input items remain unchanged (verified by test).

5. **Confidence threshold**: Bills with confidence < 0.8 show a warning in the preview. Below 0.3, a stronger warning is added. The regex fallback caps at 0.6.

6. **Pending bill is DB-backed**: `PendingBillRecord` table stores pending bills (serialized as JSON). Safe across multiple gunicorn workers. Expired entries cleaned up on each incoming message.

6. **Thread safety**: Invoice sequence uses both a Python `threading.Lock` and SQL `WITH FOR UPDATE`. The Python lock is needed because SQLite doesn't support row-level locking. GST cache also uses a threading lock for concurrent worker safety.

6b. **Webhook message dedup**: INSERT-FIRST pattern via `try_claim_message(message_id)` — attempts INSERT into `ProcessedMessage` table, returns True (new, process it) or False (duplicate via UNIQUE constraint, skip). Eliminates check-then-insert race condition. Empty/missing `message_id` logs warning and skips dedup (message still processed). Cleanup via `maybe_cleanup_processed_messages()` — throttled to every 100 webhook calls using a thread-safe counter, not every request. `try_claim_message` uses raw `SessionLocal()` (not `db_session()`) to keep expected IntegrityError at DEBUG level instead of ERROR. Fails open on non-integrity DB errors. `parse_meta_webhook_payload()` returns `message_id` field (from `messages[].id`).

7. **Meta WhatsApp sends**: `send_text_message` / `send_document_by_link` need `WHATSAPP_PHONE_NUMBER_ID` and `WHATSAPP_ACCESS_TOKEN`; missing values surface as API errors when sending.

8. **`number_to_words` uses Indian numbering**: Lakh (100,000) and Crore (10,000,000) system, not million/billion. Negative amounts return empty string (guarded).

9. **Regex parser anchoring**: The `qty_name_price` pattern is anchored with `(?:^|[,\n])` to prevent greedy matching across items (e.g., "rice 50 soap 25" previously misread "50" as quantity for soap).

10. **Claude API empty response**: Both `ai/parser.py` and `core/gst_rates.py` guard against `response.content` being empty before accessing `[0]`.

11. **Fuzzy cache bounded**: `_fuzzy_cache` in `core/gst_rates.py` is capped at 10,000 entries to prevent unbounded memory growth.

12. **No `logging.basicConfig` in modules**: Only `main.py` / `whatsapp_webhook.py` should call `basicConfig`. Module-level calls override the root logger level.

13. **`create_all()` won't add columns**: SQLAlchemy's `Base.metadata.create_all()` only creates NEW tables. It does NOT alter existing tables to add missing columns. This is why `validate_schema()` + `reset_database()` exist — to detect and fix the mismatch.

14. **`rapidfuzz` is a hard dependency**: Top-level `from rapidfuzz import fuzz, process, utils` in `core/gst_rates.py` means the app cannot boot without it. Don't wrap `rapidfuzz` imports in `try/except ImportError` anywhere — that's dead code and masks real import errors. It's pinned in `requirements.txt` (`rapidfuzz>=3.0.0`).

15. **Package reorganization**: The codebase is split into `ai/`, `api/`, `core/`, `db/`, `services/`. Root-level files like `database.py`, `bill_generator.py`, `claude_parser.py`, `gst_rates.py`, `reports.py`, `return_detector.py`, `whatsapp_client.py` are **backward-compatibility shims** that re-export from the new packages. When editing, prefer the canonical location in the subpackage; the shim just pulls symbols forward. See `PROJECT_CONTEXT.md` for the full file table.

16. **`gst <item> <rate>` command** (`services/billing.py:_handle_gst_update`): Updates an existing `ShopItemMaster` row if found, otherwise inserts a new confirmed row (HSN taken from `get_gst_rate(item_name)`, defaulting to `"9999"`). Because `get_gst_rate_smart` checks the item master as Step 0, all subsequent bills use the new rate — no restart or cache clear needed.

---

## Update Rule

Whenever architecture or logic changes, **both `PROJECT_CONTEXT.md` and `memory.md` must be updated** to stay in sync with the codebase.
