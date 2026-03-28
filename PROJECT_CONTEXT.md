# BilledUp — Project Context

> AI-powered GST billing for Indian retail shops via WhatsApp, using Claude API for natural language parsing.

---

## Architecture (High-Level)

```
WhatsApp (Twilio) → Flask Webhook → Claude Parser → Bill Calculator → PDF Generator → DB + WhatsApp Reply
                                                          ↓
                                                   GST Rate Lookup (hardcoded + fuzzy + Claude fallback)
```

Two entry points:
1. **WhatsApp webhook** (`whatsapp_webhook.py`) — production, Twilio-based
2. **Interactive CLI** (`main.py`) — demo/testing terminal loop

---

## Main Components

| File | Purpose |
|---|---|
| `main.py` | Entry point: CLI billing loop, CRUD ops (save/query bills), session management, environment validation. Seeds demo shop "RAVI". |
| `database.py` | SQLAlchemy models + session helper. Models: `Shop`, `Bill`, `SessionRecord`, `InvoiceSequence`, `Registration`, `ConversationLog`. Thread-safe invoice sequence. |
| `bill_generator.py` | Dataclasses (`BillItem`, `ShopProfile`, `CustomerInfo`, `BillResult`), GST calculation, PDF generation via ReportLab. Handles TAX INVOICE vs BILL OF SUPPLY. |
| `claude_parser.py` | Sends shopkeeper messages to Claude API for item extraction. Includes rate limiting, retry logic (429/529), input sanitization, prompt injection detection, and a regex fallback parser. |
| `gst_rates.py` | 200+ hardcoded HSN/GST rate mappings. 5-step lookup: exact → substring → fuzzy (rapidfuzz) → JSON cache → Claude API fallback. |
| `config.py` | Loads `.env`, validates required keys, lazy Anthropic client singleton. Also holds Twilio config. |
| `whatsapp_webhook.py` | Flask app with Twilio webhook. Full self-registration state machine (NEW → ASKED_NAME → ASKED_ADDRESS → ASKED_GSTIN → ACTIVE → EXPIRED). Bill preview/confirmation flow before PDF generation. Indian states dict for IGST state selection. REST API endpoints with API key auth. |

---

## End-to-End Flow

1. **Shopkeeper** sends WhatsApp message (e.g., "phone case 299 charger 499 customer Suresh")
2. **Twilio** forwards to Flask `/webhook`
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
- **Twilio** — WhatsApp messaging
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

---

## External Integrations

| Service | Purpose |
|---|---|
| **Claude API** | NLP parsing of billing messages + GST rate lookup for unknown items |
| **Twilio** | WhatsApp message send/receive |
| **Railway** | Hosting (BASE_URL configured) |

---

## Important Notes

- **Invoice format**: `INV-{YEAR}-{SHOP_ID}-{SEQUENCE}` (e.g., `INV-2026-RAVI-00001`)
- **GST slabs**: Only 0%, 5%, 12%, 18%, 28% are valid — anything else corrects to 18%
- **TAX INVOICE vs BILL OF SUPPLY**: Determined by whether shop has real GSTIN (validated via regex) or placeholder. BILL OF SUPPLY hides CGST/SGST columns entirely.
- **IGST support**: If customer state_code differs from shop state_code → inter-state → IGST. If same or missing → intra-state → CGST+SGST. PDF layout, totals, and WhatsApp summaries adapt automatically.
- **Trial system**: 10-day free trial, then Rs.299/month (manual upgrade via support contact)
- **Prices are pre-GST**: Claude prompt explicitly states prices given are before GST
- **Bill confirmation flow**: After parsing, a preview is shown (items, customer, tax type). User must reply YES to generate. Can modify customer name (`NAME Ravi`), state (`STATE`), re-enter items (`EDIT`), or cancel. Pending bills expire after 10 minutes. Stored in-memory per worker (keyed by phone).
- **Regex fallback**: If Claude API fails, a rule-based regex parser handles item extraction (confidence capped at 0.6). Patterns are anchored to prevent greedy cross-item matching.
- **Rate limiting**: 100 calls/60s sliding window on Claude API calls
- **State defaults**: Telangana / state code 36 (Hyderabad-centric). Customer state defaults to shop state (intra-state) if not provided.
- **GST rate substring matching**: Uses word-boundary regex (`\bterm\b`) instead of raw substring to prevent false positives (e.g., "ac" no longer matches "bracelet")
- **Cache file**: Uses absolute path (`os.path.dirname(os.path.abspath(__file__))`) with thread-safe read-merge-write to handle concurrent gunicorn workers
- **PDF safety**: All user-supplied text is XML-escaped before rendering in ReportLab Paragraphs
- **API key logging**: Truncated to first 8 chars to prevent plaintext credential exposure

---

## Update Rule

Whenever architecture or logic changes, **both `PROJECT_CONTEXT.md` and `memory.md` must be updated** to stay in sync with the codebase.
