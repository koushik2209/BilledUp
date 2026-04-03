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

| File | Purpose |
|---|---|
| `main.py` | Entry point: CLI billing loop, CRUD ops (save/query bills), session management, environment validation. Seeds demo shop "RAVI". |
| `database.py` | SQLAlchemy models + session helper. Models: `Shop`, `Bill` (with `is_return` flag + `pdf_data` LargeBinary), `SessionRecord`, `InvoiceSequence`, `Registration`, `ConversationLog`, `ReportPDF`. Thread-safe invoice sequence. |
| `bill_generator.py` | Dataclasses (`BillItem`, `ShopProfile`, `CustomerInfo`, `BillResult`), GST calculation, in-memory PDF generation via ReportLab (returns bytes, no filesystem). Handles TAX INVOICE, BILL OF SUPPLY, and CREDIT NOTE. |
| `return_detector.py` | Rule-based + fuzzy return/credit note intent detection. Keyword regex, rapidfuzz partial matching, negative price detection. `negate_items()` for credit note processing. No external API calls. |
| `claude_parser.py` | Sends shopkeeper messages to Claude API for item extraction. Includes rate limiting, retry logic (429/529), input sanitization, prompt injection detection, and a regex fallback parser. |
| `gst_rates.py` | 200+ hardcoded HSN/GST rate mappings. 5-step lookup: exact → substring → fuzzy (rapidfuzz) → JSON cache → Claude API fallback. |
| `reports.py` | GST report generation: `get_gst_report()` (DB aggregation), `parse_report_range()` (NL date parsing), `msg_gst_report()` (WhatsApp format), `export_gst_report_pdf()` (returns PDF bytes + filename, no filesystem). Indian number formatting. |
| `config.py` | Loads `.env`, validates required keys, lazy Anthropic client singleton. Meta WhatsApp (`WHATSAPP_*`, `VERIFY_TOKEN`). |
| `whatsapp_client.py` | Meta Graph API: send text, template, document by URL; parse webhook payload. |
| `whatsapp_webhook.py` | Flask app with Meta webhook (GET verify + POST). Full self-registration state machine (NEW → ASKED_NAME → ASKED_ADDRESS → ASKED_GSTIN → ACTIVE → EXPIRED). Bill preview/confirmation flow before PDF generation. Indian states dict for IGST state selection. REST API endpoints with API key auth. |

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
- **GST rate substring matching**: Uses word-boundary regex (`\bterm\b`) instead of raw substring to prevent false positives (e.g., "ac" no longer matches "bracelet")
- **Cache file**: Uses absolute path (`os.path.dirname(os.path.abspath(__file__))`) with thread-safe read-merge-write to handle concurrent gunicorn workers
- **PDF storage**: All PDFs (bills and reports) are generated in-memory via BytesIO and stored as `LargeBinary` in PostgreSQL. No filesystem PDF operations. `Bill.pdf_data` for invoices, `ReportPDF` table for GST reports. Served via `/bills/<invoice>.pdf` and `/reports/<filename>` endpoints reading from DB.
- **PDF safety**: All user-supplied text is XML-escaped before rendering in ReportLab Paragraphs
- **API key logging**: Truncated to first 8 chars to prevent plaintext credential exposure
- **Webhook dedup**: INSERT-FIRST pattern via `try_claim_message(message_id)` — attempts INSERT, returns True (new) or False (duplicate via UNIQUE constraint). No check-then-insert race condition. Empty/missing message_id skips dedup with a warning log. Cleanup throttled to every 100 webhook calls (not every request). Uses raw session to keep expected IntegrityError at DEBUG level. Fails open on non-integrity DB errors.

---

## Update Rule

Whenever architecture or logic changes, **both `PROJECT_CONTEXT.md` and `memory.md` must be updated** to stay in sync with the codebase.
