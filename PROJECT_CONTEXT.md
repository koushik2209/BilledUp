# BilledUp — Project Context

> AI-powered GST billing for Indian retail shops via WhatsApp, using Claude API for natural language parsing.

---

## Architecture (High-Level)

```
WhatsApp (Meta Cloud API) → Flask Webhook → Claude Parser → Bill Calculator → PDF Generator → DB + WhatsApp Reply
                                                          ↓
                                                   GST Rate Lookup (hardcoded + fuzzy + Claude fallback)
```

Two entry points:
1. **WhatsApp webhook** (`whatsapp_webhook.py`) — production, Meta WhatsApp Cloud API
2. **Interactive CLI** (`main.py`) — demo/testing terminal loop

---

## Main Components

| Module | Purpose |
|---|---|
| `main.py` | Entry point: CLI billing loop, CRUD ops (save/query bills), session management, environment validation. Seeds demo shop "RAVI". |
| `config.py` | Loads `.env`, validates required keys, lazy Anthropic client singleton. Meta WhatsApp (`WHATSAPP_*`, `VERIFY_TOKEN`). |
| `whatsapp_webhook.py` | Flask app with Meta webhook (GET verify + POST, HMAC signature validation). Bill preview/confirmation flow routing. REST API endpoints with API key auth. Auto-switches to Gunicorn in production. |
| `ai/parser.py` | Sends shopkeeper messages to Claude API for item extraction. Rate limiting (100/60s sliding window), retry logic (429/529), sanitization, prompt injection detection. |
| `ai/sanitizer.py` | Input sanitation (control chars, prompt injection, 1000-char cap) + response validation. |
| `ai/regex_parser.py` | Rule-based fallback parser (9 patterns). Confidence capped at 0.6. |
| `api/whatsapp_client.py` | Meta Graph API: send text/template/document, parse webhook payload, retry on 429/5xx. |
| `api/formatters.py` | WhatsApp message templates (welcome, help, preview, activated, state menu). `_STATE_MENU` drives state selection. |
| `core/billing.py` | `calculate_bill`, `is_intra_state`, `number_to_words` (Indian lakh/crore). Bill of Supply aware. |
| `core/invoice.py` | `generate_invoice_number` with `CN-` prefix path for credit notes. |
| `core/returns.py` | 3-tier return/credit note detection (keyword regex → rapidfuzz → majority-negative). Whitelist (back cover, exchange offer, money back, etc.). |
| `core/reports.py` | GST report generation and date-range parsing. |
| `core/gst_rates.py` | 200+ hardcoded HSN/GST mappings. 6-step lookup: shop item master → exact → word-boundary → fuzzy (rapidfuzz) → JSON cache → Claude. Price-based slab adjust for clothing/footwear. |
| `core/entities/*` | Dataclasses: `BillItem`, `BillResult`, `ShopProfile`, `CustomerInfo`. |
| `db/models.py` | SQLAlchemy models: `Shop`, `Bill` (with `pdf_data` LargeBinary, `is_return`, `is_igst`), `Registration`, `InvoiceSequence`, `PendingBillRecord`, `ProcessedMessage` (dedup), `ShopItemMaster`, `ConversationLog`, `ReportPDF`, `SessionRecord`. |
| `db/session.py` | Engine, `db_session()` context manager, `init_database`, `ensure_schema`, `reset_database`, thread-safe `generate_next_sequence`. |
| `db/crud.py` | API key generation + validation. |
| `db/item_master.py` | Per-shop item GST memory (`get_item_master`, `save_item_master`, `update_item_gst`, `get_top_items`). |
| `db/dedup.py` | `try_claim_message` INSERT-FIRST webhook dedup. |
| `services/router.py` | Main message dispatch. Registration state machine (NEW → ASKED_NAME → ASKED_ADDRESS → ASKED_GSTIN → ASKED_STATE → ACTIVE → EXPIRED). Invalid states re-prompt (no fake code fallback). |
| `services/billing.py` | Preview/confirmation flow, `gst report`, `myitems`, `gst <item> <rate>` override, duplicate-bill guard. |
| `services/pdf_renderer.py` | ReportLab PDF generation: three layouts (CGST+SGST / IGST / Bill of Supply), XML-escaped text, credit-note negation. |
| `services/pending.py` | `PendingBill` dataclass + DB serialization (10-min expiry, gunicorn-safe). |
| `services/registration.py` | Registration CRUD, trial management, GSTIN validation, `INDIAN_STATES`, `resolve_state`. |

Root-level modules (`database.py`, `bill_generator.py`, `claude_parser.py`, `gst_rates.py`, `reports.py`, `return_detector.py`, `whatsapp_client.py`) are **backward-compatibility shims** that re-export from the packages above.

---

## End-to-End Flow

1. **Shopkeeper** sends WhatsApp message (e.g., "phone case 299 charger 499 customer Suresh")
2. **Meta** delivers inbound messages to Flask `/webhook`
3. **State machine** checks registration state; if ACTIVE, proceeds to billing
4. **Claude Parser** extracts customer name + items from natural language (English/Telugu/Hindi)
5. **Preview** shown with parsed items, customer name, and tax type (CGST+SGST or IGST)
6. **Confirmation** — shopkeeper replies YES to confirm, or modifies name/state/items first
7. **GST Rate Lookup** resolves HSN codes and GST rates per item
8. **Bill Calculator** computes subtotal, CGST/SGST or IGST, grand total
9. **PDF Generator** creates professional A4 invoice via ReportLab
10. **Database** stores bill record (SQLAlchemy → SQLite/PostgreSQL)
11. **WhatsApp reply** sends bill summary text + PDF attachment back to shopkeeper

---

## Technologies

- **Python 3.11+**
- **Claude API** (Anthropic) — message parsing + GST rate fallback (model: `claude-sonnet-4-20250514`)
- **SQLAlchemy** — ORM (SQLite local, PostgreSQL production)
- **ReportLab** — PDF generation
- **Flask** — webhook server
- **Meta WhatsApp Cloud API** — WhatsApp messaging (Graph API)
- **rapidfuzz** — fuzzy string matching for GST lookups
- **gunicorn** — production WSGI server (4 workers)
- **Deployed on Railway** (Procfile present)

---

## Key Entities

| Entity | Description |
|---|---|
| `Shop` | Registered shop with GSTIN, address, API key |
| `Bill` | Generated invoice with items JSON, totals, PDF path |
| `Registration` | WhatsApp self-registration state + trial tracking |
| `SessionRecord` | CLI session with bill count and total value |
| `InvoiceSequence` | Thread-safe per-shop-per-year invoice counter |
| `ConversationLog` | All WhatsApp messages (IN/OUT) for debugging |
| `ProcessedMessage` | WhatsApp message ID dedup table — prevents duplicate processing on webhook retries |

---

## External Integrations

| Service | Purpose |
|---|---|
| **Claude API** | NLP parsing of billing messages + GST rate lookup for unknown items |
| **Meta WhatsApp Cloud API** | WhatsApp message send/receive |
| **Railway** | Hosting (BASE_URL configured) |

---

## Important Notes

- **Invoice format**: `INV-{YEAR}-{SHOP_ID}-{SEQUENCE}` (e.g., `INV-2026-RAVI-00001`)
- **GST slabs**: Only 0%, 5%, 12%, 18%, 28% are valid — anything else corrects to 18%
- **TAX INVOICE vs BILL OF SUPPLY**: Determined by whether shop has real GSTIN (validated via regex) or placeholder. BILL OF SUPPLY hides CGST/SGST columns entirely.
- **IGST support**: If customer state_code differs from shop state_code → inter-state → IGST. If same or missing → intra-state → CGST+SGST. PDF layout, totals, and WhatsApp summaries adapt automatically.
- **Trial system**: 10-day free trial, then Rs.299/month (manual upgrade via support contact)
- **Prices are pre-GST**: Claude prompt explicitly states prices given are before GST
- **Bill confirmation flow**: After parsing, a preview is shown (items with GST rate per item, customer, tax type, full GST breakdown). User must reply YES to generate. Can modify customer name (`NAME Ravi`), state (`STATE`), override GST rate (`GST 1 12`), re-enter items (`EDIT`), or cancel. Pending bills expire after 10 minutes. Stored in DB via `PendingBillRecord` table (keyed by phone, safe across gunicorn workers). GST rates resolved at preview time and stored in pending items — ensures preview totals match final bill exactly.
- **GST rate source tracking**: `get_gst_rate_smart()` returns a `source` field: `exact`, `fuzzy`, `cache`, `claude`, `default`. Items with `fuzzy` or `default` source show a warning marker in preview. Users can override with `GST <item#> <rate>`.
- **Orphan command handling**: If user sends confirmation commands (YES, CANCEL, EDIT, NAME, STATE, GST override) with no pending bill, a helpful "no pending bill" message is shown instead of parsing as items.
- **GST Reports**: `gst report` command generates monthly/date-range GST summaries. Supports "gst report", "gst report last 7 days", "gst report last month", "gst report march". Returns WhatsApp text summary + PDF attachment. Indian number formatting (lakh/crore). Report PDFs stored in `ReportPDF` table (in-memory generation, no filesystem).
- **Regex fallback**: If Claude API fails, a rule-based regex parser handles item extraction (confidence capped at 0.6). Patterns are anchored to prevent greedy cross-item matching.
- **Rate limiting**: 100 calls/60s sliding window on Claude API calls
- **State defaults**: Telangana / state code 36 (Hyderabad-centric). Customer state defaults to shop state (intra-state) if not provided.
- **State selection validation**: During `ASKED_STATE` registration, if the user's input can't be resolved to a real GST state code (exact → partial → rapidfuzz WRatio ≥ 60), the bot re-prompts with the state menu instead of storing a fake code. A bogus state_code would silently break `is_intra_state()` and force IGST on every bill. The "Other" menu index is computed from `len(_STATE_MENU) + 1` in both `api/formatters.py` and `services/router.py` — no hardcoded `14`.
- **GST rate substring matching**: Uses word-boundary regex (`\bterm\b`) instead of raw substring to prevent false positives (e.g., "ac" no longer matches "bracelet")
- **Cache file**: Uses absolute path (`os.path.dirname(os.path.abspath(__file__))`) with thread-safe read-merge-write to handle concurrent gunicorn workers
- **PDF storage**: All PDFs (bills and reports) are generated in-memory via BytesIO and stored as `LargeBinary` in PostgreSQL. No filesystem PDF operations. `Bill.pdf_data` for invoices, `ReportPDF` table for GST reports. Served via `/bills/<invoice>.pdf` and `/reports/<filename>` endpoints reading from DB.
- **PDF safety**: All user-supplied text is XML-escaped before rendering in ReportLab Paragraphs
- **API key logging**: Truncated to first 8 chars to prevent plaintext credential exposure
- **Webhook dedup**: INSERT-FIRST pattern via `try_claim_message(message_id)` — attempts INSERT, returns True (new) or False (duplicate via UNIQUE constraint). No check-then-insert race condition. Empty/missing message_id skips dedup with a warning log. Cleanup throttled to every 100 webhook calls (not every request). Uses raw session to keep expected IntegrityError at DEBUG level. Fails open on non-integrity DB errors.
- **Schema validation at startup**: `ensure_schema()` runs after `init_database()`. Checks required tables and columns against `_REQUIRED_SCHEMA` dict in `db/session.py` using SQLAlchemy `inspect`. If `DEV_MODE=True` → auto-resets DB (drops all, recreates from models). If `DEV_MODE=False` (production) → logs warnings only, no destructive action. `reset_database()` also deletes the SQLite file for a clean slate. All schema logs use `[DB]` prefix.

---

## Update Rule

Whenever architecture or logic changes, **both `PROJECT_CONTEXT.md` and `memory.md` must be updated** to stay in sync with the codebase.
