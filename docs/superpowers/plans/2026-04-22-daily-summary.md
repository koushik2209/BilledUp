# Daily Summary Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send every active shopkeeper a formatted WhatsApp daily summary at 9 PM IST, also available on-demand via the `summary` command.

**Architecture:** Pure formatter (`core/daily_summary.py`) + DB service (`services/daily_summary_service.py`) + cron HTTP endpoint in `whatsapp_webhook.py` protected by `CRON_SECRET`. The `summary` WhatsApp command in `conversation/manager.py` calls the same two functions. IST timezone used throughout via `pytz`.

**Tech Stack:** Python, Flask, SQLAlchemy, pytz, pytest, cron-job.org (external trigger)

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Create | `core/daily_summary.py` | Pure formatter: `_fmt()`, `format_daily_summary()` |
| Create | `services/daily_summary_service.py` | DB queries: `get_daily_summary_data()` |
| Create | `tests/test_daily_summary.py` | Unit tests for formatter |
| Create | `tests/test_daily_summary_service.py` | Unit tests for service |
| Modify | `requirements.txt` | Add `pytz>=2024.1` |
| Modify | `db/models.py` | Add 2 columns to `Shop` |
| Modify | `db/session.py` | Add columns to `_REQUIRED_SCHEMA["shops"]` |
| Modify | `conversation/manager.py` | Split `summary` out of the `today` command group |
| Modify | `whatsapp_webhook.py` | Add `POST /api/cron/daily-summary` endpoint |

---

## Task 1: Add pytz dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add pytz to requirements.txt**

Open `requirements.txt` and add this line:
```
pytz>=2024.1
```

The file should look like:
```
anthropic>=0.40
python-dotenv>=1.0.0
reportlab>=4.0.0
pytest>=8.0.0
flask>=3.0.0
gunicorn>=22.0.0
rapidfuzz>=3.0.0
sqlalchemy>=2.0.0
psycopg2-binary>=2.9.0
pytz>=2024.1
```

- [ ] **Step 2: Install it**

```bash
pip install pytz>=2024.1
```

Expected: `Successfully installed pytz-...` (or `Requirement already satisfied`)

- [ ] **Step 3: Verify import works**

```bash
python -c "import pytz; print(pytz.timezone('Asia/Kolkata'))"
```

Expected: `Asia/Kolkata`

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "chore: add pytz dependency for IST timezone handling"
```

---

## Task 2: Schema — add columns to Shop model

**Files:**
- Modify: `db/models.py:18-35`
- Modify: `db/session.py:74-75`

- [ ] **Step 1: Add columns to Shop model in `db/models.py`**

Find the `Shop` class (starts at line 18). Add two columns after `default_bill_type`:

```python
    default_bill_type = Column(String(15), nullable=True)
    last_summary_sent_at = Column(DateTime, nullable=True)
    summary_opt_out      = Column(Boolean, default=False)
    api_key    = Column(String(64), unique=True, nullable=True, index=True)
```

- [ ] **Step 2: Update `_REQUIRED_SCHEMA` in `db/session.py`**

Find the `"shops"` entry in `_REQUIRED_SCHEMA` (around line 74). Add the two new column names:

```python
    "shops":              ["api_key", "state", "state_code", "upi", "default_pricing",
                          "default_bill_type", "last_summary_sent_at", "summary_opt_out"],
```

- [ ] **Step 3: Write a test to verify both columns exist**

Create `tests/test_daily_summary_service.py` with just this test for now:

```python
"""Tests for daily summary service and schema."""
import main


def test_shop_schema_has_new_columns():
    """Verify last_summary_sent_at and summary_opt_out exist on shops table."""
    from sqlalchemy import inspect as sa_inspect
    from db.session import engine

    main.init_database()

    inspector = sa_inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("shops")}
    assert "last_summary_sent_at" in cols, "missing column: last_summary_sent_at"
    assert "summary_opt_out" in cols, "missing column: summary_opt_out"
```

- [ ] **Step 4: Run the test**

```bash
pytest tests/test_daily_summary_service.py::test_shop_schema_has_new_columns -v
```

Expected: PASS — `init_database()` creates a fresh SQLite DB from the current model definition, which already includes the two new columns from Steps 1–2. If it fails, the model edits in Step 1 were not saved correctly.

- [ ] **Step 5: Commit**

```bash
git add db/models.py db/session.py tests/test_daily_summary_service.py
git commit -m "feat: add last_summary_sent_at and summary_opt_out columns to shops table"
```

---

## Task 3: Core formatter — `core/daily_summary.py`

**Files:**
- Create: `core/daily_summary.py`
- Create: `tests/test_daily_summary.py`

### Step group A: Indian number formatter `_fmt`

- [ ] **Step 1: Write failing tests for `_fmt`**

Create `tests/test_daily_summary.py`:

```python
"""Unit tests for core.daily_summary formatter. No DB, no network."""
import pytest
from core.daily_summary import _fmt, format_daily_summary


# ── _fmt: Indian number formatting ────────────────────────────────


def test_fmt_below_1000():
    assert _fmt(0)    == "₹0"
    assert _fmt(850)  == "₹850"
    assert _fmt(999)  == "₹999"


def test_fmt_thousands():
    assert _fmt(1000)  == "₹1,000"
    assert _fmt(34200) == "₹34,200"
    assert _fmt(99999) == "₹99,999"


def test_fmt_lakhs_basic():
    assert _fmt(100000) == "₹1.0L"
    assert _fmt(420000) == "₹4.2L"


def test_fmt_lakhs_rounding():
    # Traditional rounding (not banker's rounding)
    assert _fmt(104000) == "₹1.0L"   # 1.04 → rounds down → 1.0
    assert _fmt(105000) == "₹1.1L"   # 1.05 → rounds up  → 1.1


def test_fmt_rounds_before_format():
    # 34200.99 rounds to 34201 before formatting
    assert _fmt(34200.99) == "₹34,201"


def test_fmt_negative_becomes_zero():
    assert _fmt(-500) == "₹0"


def test_fmt_none_becomes_zero():
    assert _fmt(None) == "₹0"
```

- [ ] **Step 2: Run — expect ImportError (file doesn't exist yet)**

```bash
pytest tests/test_daily_summary.py -v
```

Expected: `ModuleNotFoundError: No module named 'core.daily_summary'`

- [ ] **Step 3: Create `core/daily_summary.py` with `_fmt` only**

```python
"""
core.daily_summary — Daily WhatsApp Summary Formatter
-------------------------------------------------------
Pure function: dict → WhatsApp message string.
No database access, no network calls.
"""
import math


def _safe(data, *keys, default=0):
    """Navigate nested dict safely; return default for missing/None."""
    val = data
    for key in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(key)
    return val if val is not None else default


def _fmt(amount) -> str:
    """
    Indian number formatting — never shows decimals.

    < 1,000         → ₹850
    1,000 – 99,999  → ₹34,200  (Indian comma grouping)
    ≥ 1,00,000      → ₹4.2L    (traditional round to 1 decimal)
    """
    amount = max(0, round(float(amount or 0)))
    if amount < 1_000:
        return f"₹{int(amount)}"
    if amount < 1_00_000:
        s = str(int(amount))
        last3 = s[-3:]
        rest  = s[:-3]
        parts = []
        while rest:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        parts.append(last3)
        return "₹" + ",".join(parts)
    # Traditional rounding to 1 decimal (avoids Python banker's rounding on .5)
    tenths = math.floor(amount / 10_000 + 0.5) / 10
    return f"₹{tenths:.1f}L"


def format_daily_summary(data: dict) -> str:
    """Placeholder — implemented in Task 3 Step 7."""
    return ""
```

- [ ] **Step 4: Run `_fmt` tests — expect PASS**

```bash
pytest tests/test_daily_summary.py -k "fmt" -v
```

Expected: 7 tests PASS.

### Step group B: `format_daily_summary`

- [ ] **Step 5: Add formatter tests to `tests/test_daily_summary.py`**

Append to the existing test file:

```python
# ── format_daily_summary ─────────────────────────────────────────

def _sample_data(
    has_gstin=True,
    total_bills=18,
    grand_total=34200.0,
    sale_amount=29000.0,
    total_gst=5200.0,
    returns_count=0,
    returns_amount=0.0,
    month_returns_count=0,
    month_returns_amount=0.0,
):
    """Build a minimal valid data dict."""
    return {
        "shop_name": "Ravi Mobile Accessories",
        "has_gstin": has_gstin,
        "date": "22 Apr 2026",
        "today": {
            "total_bills":    total_bills,
            "grand_total":    grand_total,
            "sale_amount":    sale_amount,
            "total_gst":      total_gst,
            "returns_count":  returns_count,
            "returns_amount": returns_amount,
        },
        "month": {
            "name":           "April",
            "total_bills":    312,
            "grand_total":    420000.0,
            "sale_amount":    356000.0,
            "total_gst":      64000.0,
            "returns_count":  month_returns_count,
            "returns_amount": month_returns_amount,
        },
    }


def test_gst_variant_contains_required_lines():
    result = format_daily_summary(_sample_data(has_gstin=True))
    assert "📊 *Today's Summary — 22 Apr 2026*" in result
    assert "🧾 Bills: 18" in result
    assert "💰 Sale Amount: ₹29,000" in result
    assert "🏛 GST Collected: ₹5,200" in result
    assert "✅ Grand Total: ₹34,200" in result
    assert "📅 *April So Far*" in result
    assert "✅ Total: ₹4.2L" in result


def test_gst_variant_no_returns_omits_returns_lines():
    result = format_daily_summary(_sample_data(has_gstin=True, returns_count=0))
    assert "↩️" not in result
    assert "🏁" not in result


def test_gst_variant_with_returns_shows_net_total():
    result = format_daily_summary(_sample_data(
        has_gstin=True, returns_count=1, returns_amount=850.0
    ))
    assert "↩️ Returns: 1 bill(s) — ₹850" in result
    assert "🏁 Net Total: ₹33,350" in result


def test_no_gstin_variant_omits_gst_lines():
    result = format_daily_summary(_sample_data(has_gstin=False))
    assert "💰 Total Billed:" in result
    assert "🏛 GST Collected" not in result
    assert "💰 Sale Amount" not in result


def test_zero_bills_shows_motivational_message():
    result = format_daily_summary(_sample_data(total_bills=0))
    assert "No bills today" in result
    assert "stronger tomorrow" in result
    assert "📅 *April So Far*" in result


def test_line_limit_trims_month_returns_first():
    # Worst case: GST + today returns + month returns = 19 lines
    data = _sample_data(
        has_gstin=True,
        returns_count=1,
        returns_amount=850.0,
        month_returns_count=4,
        month_returns_amount=3200.0,
    )
    result = format_daily_summary(data)
    lines = result.split("\n")
    assert len(lines) <= 18
    assert "↩️ Returns: 4 bill(s)" not in result   # month returns trimmed
    assert "↩️ Returns: 1 bill(s)" in result        # today returns kept


def test_missing_fields_default_to_zero():
    result = format_daily_summary({
        "shop_name": "X",
        "has_gstin": True,
        "date": "22 Apr 2026",
        "today": {},
        "month": {"name": "April"},
    })
    # Zero bills → motivational message
    assert "No bills today" in result


def test_net_total_never_negative():
    # returns_amount > grand_total → net = 0
    result = format_daily_summary(_sample_data(
        returns_count=1, returns_amount=99999.0
    ))
    assert "🏁 Net Total: ₹0" in result
```

- [ ] **Step 6: Run — expect FAIL (format_daily_summary returns "")**

```bash
pytest tests/test_daily_summary.py -v
```

Expected: `_fmt` tests still PASS; formatter tests FAIL with assertion errors.

- [ ] **Step 7: Implement `format_daily_summary` in `core/daily_summary.py`**

Replace the placeholder `format_daily_summary` with the full implementation:

```python
def _month_section(
    month: dict,
    has_gstin: bool,
    include_sale: bool = True,
    include_gst: bool = True,
    skip_returns: bool = False,
) -> list:
    """Build month section as a list of lines (including leading blank)."""
    name           = str(month.get("name") or "Month")
    total_bills    = int(_safe(month, "total_bills"))
    grand_total    = float(_safe(month, "grand_total"))
    sale_amount    = float(_safe(month, "sale_amount"))
    total_gst      = float(_safe(month, "total_gst"))
    returns_count  = int(_safe(month, "returns_count"))
    returns_amount = float(_safe(month, "returns_amount"))

    lines = ["", f"📅 *{name} So Far*", ""]
    if has_gstin:
        lines.append(f"🧾 Bills: {total_bills}")
        if include_sale:
            lines.append(f"💰 Sale Amount: {_fmt(sale_amount)}")
        if include_gst:
            lines.append(f"🏛 GST Collected: {_fmt(total_gst)}")
        lines.append(f"✅ Total: {_fmt(grand_total)}")
    else:
        lines += [
            f"🧾 Bills: {total_bills}",
            f"💰 Total: {_fmt(grand_total)}",
        ]
    if returns_count > 0 and not skip_returns:
        lines += ["", f"↩️ Returns: {returns_count} bill(s) — {_fmt(returns_amount)}"]
    return lines


def format_daily_summary(data: dict) -> str:
    """
    Format a daily billing summary for WhatsApp delivery.

    Input shape matches get_daily_summary_data() output.
    Rules:
    - has_gstin=True  → show sale_amount + GST + grand_total
    - has_gstin=False → show grand_total only (no GST)
    - total_bills=0   → motivational zero-bills message
    - returns_count>0 → show returns + net total lines
    - Max 18 lines; trim month_returns → month_gst → month_sale if over
    """
    if not isinstance(data, dict):
        return "Could not generate summary."

    has_gstin = bool(_safe(data, "has_gstin", default=False))
    date_str  = str(_safe(data, "date", default=""))
    today     = data.get("today") or {}
    month     = data.get("month")

    total_bills = int(_safe(today, "total_bills"))

    # Zero-bills path
    if total_bills == 0:
        lines = [
            f"📊 *Today's Summary — {date_str}*",
            "",
            "No bills today — happens sometimes.",
            "Get ready for a stronger tomorrow. 💪",
        ]
        if month:
            lines += _month_section(
                month, has_gstin=False, include_sale=False, include_gst=False
            )
        return "\n".join(lines)

    grand_total    = float(_safe(today, "grand_total"))
    sale_amount    = float(_safe(today, "sale_amount"))
    total_gst      = float(_safe(today, "total_gst"))
    returns_count  = int(_safe(today, "returns_count"))
    returns_amount = float(_safe(today, "returns_amount"))

    lines = [f"📊 *Today's Summary — {date_str}*", ""]
    if has_gstin:
        lines += [
            f"🧾 Bills: {total_bills}",
            f"💰 Sale Amount: {_fmt(sale_amount)}",
            f"🏛 GST Collected: {_fmt(total_gst)}",
            f"✅ Grand Total: {_fmt(grand_total)}",
        ]
    else:
        lines += [
            f"🧾 Bills: {total_bills}",
            f"💰 Total Billed: {_fmt(grand_total)}",
        ]

    if returns_count > 0:
        net = max(0.0, grand_total - returns_amount)
        lines += [
            "",
            f"↩️ Returns: {returns_count} bill(s) — {_fmt(returns_amount)}",
            "",
            f"🏁 Net Total: {_fmt(net)}",
        ]

    if month:
        # Try trimming until ≤ 18 lines (trim order per spec)
        skip_returns = False
        skip_gst     = False
        skip_sale    = False
        for _ in range(3):
            month_lines = _month_section(
                month, has_gstin,
                include_sale=not skip_sale,
                include_gst=not skip_gst,
                skip_returns=skip_returns,
            )
            if len(lines + month_lines) <= 18:
                break
            if not skip_returns:
                skip_returns = True
            elif not skip_gst:
                skip_gst = True
            else:
                skip_sale = True
        lines += month_lines

    return "\n".join(lines)
```

- [ ] **Step 8: Run all formatter tests — expect all PASS**

```bash
pytest tests/test_daily_summary.py -v
```

Expected: All tests PASS.

- [ ] **Step 9: Commit**

```bash
git add core/daily_summary.py tests/test_daily_summary.py
git commit -m "feat: add format_daily_summary with Indian number formatting and line trimming"
```

---

## Task 4: Service layer — `services/daily_summary_service.py`

**Files:**
- Create: `services/daily_summary_service.py`
- Modify: `tests/test_daily_summary_service.py`

- [ ] **Step 1: Add service tests to `tests/test_daily_summary_service.py`**

Append to the file (which currently has only the schema test):

```python
from datetime import date, datetime
import pytest
from db.session import db_session
from db.models import Shop, Bill


# ── DB fixture helpers ────────────────────────────────────────────

_counter = 0


def _next_inv() -> str:
    global _counter
    _counter += 1
    return f"TEST-{_counter:05d}"


def _shop(session, shop_id="S99999999", gstin="36AABCU9603R1ZX"):
    s = Shop(
        shop_id=shop_id,
        name="Test Shop",
        address="123 Test St",
        gstin=gstin,
        phone="9999999999",
        state="Telangana",
        state_code="36",
    )
    session.add(s)
    session.flush()
    return s


def _bill(session, shop_id, grand_total, subtotal, total_gst,
          created_at, is_return=False):
    b = Bill(
        invoice_number=_next_inv(),
        shop_id=shop_id,
        customer_name="Customer",
        items_json="[]",
        subtotal=subtotal,
        total_cgst=total_gst / 2,
        total_sgst=total_gst / 2,
        total_igst=0.0,
        total_gst=total_gst,
        grand_total=grand_total,
        is_return=is_return,
        pdf_path="test.pdf",
        created_at=created_at,
    )
    session.add(b)


# ── Tests ─────────────────────────────────────────────────────────

def test_get_daily_summary_data_basic():
    main.init_database()
    from services.daily_summary_service import get_daily_summary_data

    target = date(2026, 4, 22)

    with db_session() as session:
        _shop(session, "S10000001")
        _bill(session, "S10000001",
              grand_total=1180.0, subtotal=1000.0, total_gst=180.0,
              created_at=datetime(2026, 4, 22, 10, 0, 0))

    result = get_daily_summary_data("S10000001", target)

    assert result["today"]["total_bills"]   == 1
    assert result["today"]["grand_total"]   == 1180.0
    assert result["today"]["sale_amount"]   == 1000.0
    assert result["today"]["total_gst"]     == 180.0
    assert result["today"]["returns_count"] == 0
    assert result["has_gstin"] is True
    assert result["date"] == "22 Apr 2026"
    assert result["month"]["name"] == "April"


def test_get_daily_summary_data_separates_returns():
    main.init_database()
    from services.daily_summary_service import get_daily_summary_data

    target = date(2026, 4, 22)

    with db_session() as session:
        _shop(session, "S10000002")
        _bill(session, "S10000002",
              grand_total=5000.0, subtotal=4500.0, total_gst=500.0,
              created_at=datetime(2026, 4, 22, 9, 0, 0))
        _bill(session, "S10000002",
              grand_total=1000.0, subtotal=900.0, total_gst=100.0,
              created_at=datetime(2026, 4, 22, 11, 0, 0),
              is_return=True)

    result = get_daily_summary_data("S10000002", target)

    assert result["today"]["total_bills"]    == 2          # 1 sale + 1 return
    assert result["today"]["grand_total"]    == 5000.0     # sales only
    assert result["today"]["returns_count"]  == 1
    assert result["today"]["returns_amount"] == 1000.0     # abs value


def test_get_daily_summary_data_month_aggregation():
    main.init_database()
    from services.daily_summary_service import get_daily_summary_data

    target = date(2026, 4, 22)

    with db_session() as session:
        _shop(session, "S10000003")
        # 3 bills earlier in the month
        for day in [1, 10, 15]:
            _bill(session, "S10000003",
                  grand_total=1000.0, subtotal=900.0, total_gst=100.0,
                  created_at=datetime(2026, 4, day, 10, 0, 0))
        # 1 bill today
        _bill(session, "S10000003",
              grand_total=2000.0, subtotal=1800.0, total_gst=200.0,
              created_at=datetime(2026, 4, 22, 10, 0, 0))

    result = get_daily_summary_data("S10000003", target)

    assert result["today"]["total_bills"]  == 1
    assert result["month"]["total_bills"]  == 4
    assert result["month"]["grand_total"]  == 5000.0


def test_get_daily_summary_data_no_gstin():
    main.init_database()
    from services.daily_summary_service import get_daily_summary_data

    with db_session() as session:
        _shop(session, "S10000004", gstin="GSTIN00000000000")  # placeholder
        _bill(session, "S10000004",
              grand_total=500.0, subtotal=500.0, total_gst=0.0,
              created_at=datetime(2026, 4, 22, 10, 0, 0))

    result = get_daily_summary_data("S10000004", date(2026, 4, 22))
    assert result["has_gstin"] is False


def test_get_daily_summary_data_raises_for_unknown_shop():
    main.init_database()
    from services.daily_summary_service import get_daily_summary_data
    import pytest
    with pytest.raises(ValueError, match="Shop not found"):
        get_daily_summary_data("NOSUCHSHOP", date(2026, 4, 22))
```

- [ ] **Step 2: Run tests — expect FAIL (module doesn't exist yet)**

```bash
pytest tests/test_daily_summary_service.py -v
```

Expected: `ModuleNotFoundError: No module named 'services.daily_summary_service'`

- [ ] **Step 3: Create `services/daily_summary_service.py`**

```python
"""
services.daily_summary_service — Daily Summary Data Layer
----------------------------------------------------------
Queries the bills table for today + month aggregates per shop.
Returns a dict shaped for format_daily_summary().
"""
import re
import logging
from datetime import date

from sqlalchemy import func

from db.session import db_session
from db.models import Bill, Shop

log = logging.getLogger("billedup.daily_summary_service")

_GSTIN_RE       = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
_PLACEHOLDER    = "GSTIN00000000000"


def _has_valid_gstin(gstin: str) -> bool:
    g = (gstin or "").strip().upper()
    return bool(g and g != _PLACEHOLDER and _GSTIN_RE.match(g))


def get_daily_summary_data(shop_id: str, target_date: date) -> dict:
    """
    Return billing aggregates for today and the current month.

    grand_total / sale_amount / total_gst cover sales only (is_return=False).
    returns_amount covers returns only (is_return=True), always positive.
    """
    month_start = target_date.replace(day=1)
    sid = shop_id.upper()

    with db_session() as session:
        shop = session.query(Shop).filter_by(shop_id=sid).first()
        if not shop:
            raise ValueError(f"Shop not found: {shop_id}")

        shop_name = shop.name
        has_gstin = _has_valid_gstin(shop.gstin)

        def _sales_agg(date_filters):
            return session.query(
                func.count(Bill.id).label("count"),
                func.coalesce(func.sum(Bill.grand_total), 0).label("grand_total"),
                func.coalesce(func.sum(Bill.subtotal),    0).label("sale_amount"),
                func.coalesce(func.sum(Bill.total_gst),   0).label("total_gst"),
            ).filter(Bill.shop_id == sid, *date_filters,
                     Bill.is_return.is_(False)).first()

        def _returns_agg(date_filters):
            return session.query(
                func.count(Bill.id).label("count"),
                func.coalesce(func.sum(Bill.grand_total), 0).label("returns_amount"),
            ).filter(Bill.shop_id == sid, *date_filters,
                     Bill.is_return.is_(True)).first()

        today_f = [func.date(Bill.created_at) == target_date]
        month_f = [
            func.date(Bill.created_at) >= month_start,
            func.date(Bill.created_at) <= target_date,
        ]

        ts = _sales_agg(today_f)
        tr = _returns_agg(today_f)
        ms = _sales_agg(month_f)
        mr = _returns_agg(month_f)

    date_str = f"{target_date.day} {target_date.strftime('%b %Y')}"

    return {
        "shop_name": shop_name,
        "has_gstin": has_gstin,
        "date":      date_str,
        "today": {
            "total_bills":    (ts.count or 0) + (tr.count or 0),
            "grand_total":    round(float(ts.grand_total  or 0), 2),
            "sale_amount":    round(float(ts.sale_amount  or 0), 2),
            "total_gst":      round(float(ts.total_gst    or 0), 2),
            "returns_count":  tr.count or 0,
            "returns_amount": round(abs(float(tr.returns_amount or 0)), 2),
        },
        "month": {
            "name":          target_date.strftime("%B"),
            "total_bills":    (ms.count or 0) + (mr.count or 0),
            "grand_total":    round(float(ms.grand_total  or 0), 2),
            "sale_amount":    round(float(ms.sale_amount  or 0), 2),
            "total_gst":      round(float(ms.total_gst    or 0), 2),
            "returns_count":  mr.count or 0,
            "returns_amount": round(abs(float(mr.returns_amount or 0)), 2),
        },
    }
```

- [ ] **Step 4: Run all service tests — expect PASS**

```bash
pytest tests/test_daily_summary_service.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add services/daily_summary_service.py tests/test_daily_summary_service.py
git commit -m "feat: add get_daily_summary_data service with today + month aggregation"
```

---

## Task 5: Wire `summary` command in `conversation/manager.py`

**Files:**
- Modify: `conversation/manager.py:238`

The `summary` keyword currently sits in the `today` group at line 238 and calls `msg_today_summary`. We need to split it out to call the new formatter.

- [ ] **Step 1: Write a test for the new `summary` command behaviour**

Add to `tests/test_daily_summary_service.py`:

```python
def test_summary_command_uses_new_formatter(monkeypatch):
    """summary command must call format_daily_summary, not msg_today_summary."""
    main.init_database()
    from conversation import manager as mgr

    # Provide a shop with a bill so total_bills > 0
    with db_session() as session:
        from db.models import Registration
        from datetime import timedelta

        _shop(session, "S10000099", gstin="36AABCU9603R1ZX")

        reg = Registration(
            phone="919999999099",
            shop_name="Test Shop",
            address="Test",
            gstin="36AABCU9603R1ZX",
            state="ACTIVE",
            active=True,
            trial_start=datetime.utcnow(),
            trial_end=datetime.utcnow() + timedelta(days=10),
        )
        session.add(reg)

        _bill(session, "S10000099",
              grand_total=500.0, subtotal=450.0, total_gst=50.0,
              created_at=datetime.utcnow())

    captured = {}

    def _mock_get_data(shop_id, target_date):
        captured["called"] = True
        return {
            "shop_name": "Test Shop",
            "has_gstin": True,
            "date": "22 Apr 2026",
            "today": {"total_bills": 1, "grand_total": 500.0,
                      "sale_amount": 450.0, "total_gst": 50.0,
                      "returns_count": 0, "returns_amount": 0.0},
            "month": {"name": "April", "total_bills": 1,
                      "grand_total": 500.0, "sale_amount": 450.0,
                      "total_gst": 50.0, "returns_count": 0,
                      "returns_amount": 0.0},
        }

    monkeypatch.setattr(
        "services.daily_summary_service.get_daily_summary_data",
        _mock_get_data,
    )

    reply = mgr.handle_message("919999999099", "summary")
    assert captured.get("called"), "get_daily_summary_data was not called"
    assert "📊" in reply
```

- [ ] **Step 2: Run — expect FAIL (summary still calls old formatter)**

```bash
pytest tests/test_daily_summary_service.py::test_summary_command_uses_new_formatter -v
```

Expected: FAIL — `captured["called"]` is False.

- [ ] **Step 3: Update `_check_hard_command` in `conversation/manager.py`**

Find the block around line 237–241:

```python
    # ── Today summary ─────────────────────────────
    if t in ("today", "aaj", "summary", "today's sales", "aaj ka"):
        shop_id = _derive_shop_id(phone)
        return msg_today_summary(shop_id, ctx.shop_name, ctx.trial_days_left)
```

Replace it with:

```python
    # ── Daily summary (new format) ────────────────
    if t == "summary":
        import pytz
        from services.daily_summary_service import get_daily_summary_data
        from core.daily_summary import format_daily_summary
        shop_id  = _derive_shop_id(phone)
        IST      = pytz.timezone("Asia/Kolkata")
        data     = get_daily_summary_data(shop_id, datetime.now(IST).date())
        return format_daily_summary(data)

    # ── Today summary (legacy format) ────────────
    if t in ("today", "aaj", "today's sales", "aaj ka"):
        shop_id = _derive_shop_id(phone)
        return msg_today_summary(shop_id, ctx.shop_name, ctx.trial_days_left)
```

- [ ] **Step 4: Run test — expect PASS**

```bash
pytest tests/test_daily_summary_service.py::test_summary_command_uses_new_formatter -v
```

Expected: PASS.

- [ ] **Step 5: Run full test suite to verify no regressions**

```bash
pytest -v
```

Expected: All previously passing tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add conversation/manager.py
git commit -m "feat: wire summary command to new format_daily_summary formatter"
```

---

## Task 6: Cron endpoint — `POST /api/cron/daily-summary`

**Files:**
- Modify: `whatsapp_webhook.py`

- [ ] **Step 1: Write cron endpoint tests**

Create `tests/test_cron_daily_summary.py`:

```python
"""Tests for POST /api/cron/daily-summary endpoint."""
import os
import pytest
import main


@pytest.fixture()
def client():
    main.init_database()
    from whatsapp_webhook import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_cron_endpoint_returns_401_without_auth(client):
    resp = client.post("/api/cron/daily-summary")
    assert resp.status_code == 401


def test_cron_endpoint_returns_401_wrong_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "correct")
    resp = client.post(
        "/api/cron/daily-summary",
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


def test_cron_endpoint_returns_401_when_no_secret_configured(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "")
    resp = client.post(
        "/api/cron/daily-summary",
        headers={"Authorization": "Bearer anything"},
    )
    assert resp.status_code == 401


def test_cron_endpoint_returns_200_with_correct_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "test-secret-xyz")
    resp = client.post(
        "/api/cron/daily-summary",
        headers={"Authorization": "Bearer test-secret-xyz"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert "sent" in data
    assert "skipped" in data
    assert "failed" in data


def test_cron_endpoint_skips_already_sent_shop(client, monkeypatch):
    """Shop with last_summary_sent_at = today (IST) must be skipped."""
    import pytz
    from datetime import datetime, timedelta
    from db.session import db_session
    from db.models import Shop, Registration

    main.init_database()

    IST = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(IST)

    with db_session() as session:
        shop = Shop(
            shop_id="S77777777",
            name="Skip Shop",
            address="Addr",
            gstin="36AABCU9603R1ZX",
            phone="917777777777",
            state="Telangana",
            state_code="36",
            last_summary_sent_at=datetime.utcnow(),  # already sent today
        )
        session.add(shop)
        reg = Registration(
            phone="917777777777",
            shop_name="Skip Shop",
            address="Addr",
            gstin="36AABCU9603R1ZX",
            state="ACTIVE",
            active=True,
            trial_start=datetime.utcnow(),
            trial_end=datetime.utcnow() + timedelta(days=10),
        )
        session.add(reg)

    monkeypatch.setenv("CRON_SECRET", "test-secret-xyz")
    resp = client.post(
        "/api/cron/daily-summary",
        headers={"Authorization": "Bearer test-secret-xyz"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sent"] == 0
    assert data["skipped"] >= 1
```

- [ ] **Step 2: Run — expect FAIL (endpoint doesn't exist yet)**

```bash
pytest tests/test_cron_daily_summary.py -v
```

Expected: 404 responses (endpoint not registered yet).

- [ ] **Step 3: Add the cron endpoint to `whatsapp_webhook.py`**

Find a suitable location in `whatsapp_webhook.py` — add this after the existing `/api/today` endpoint (around line 468). Add the import of `pytz` at the top of the file alongside the other imports:

At the top of `whatsapp_webhook.py`, add to the existing imports:
```python
import time
import pytz
```

Then add the endpoint:

```python
@app.route("/api/cron/daily-summary", methods=["POST"])
def api_cron_daily_summary():
    """POST /api/cron/daily-summary — Triggered by cron-job.org at 9 PM IST.

    Auth: Authorization: Bearer <CRON_SECRET>
    Returns: {"sent": N, "skipped": N, "failed": N}
    """
    from services.daily_summary_service import get_daily_summary_data
    from core.daily_summary import format_daily_summary
    from services.registration import get_shop_id as _get_shop_id

    cron_secret = os.environ.get("CRON_SECRET", "")
    auth_header = request.headers.get("Authorization", "")
    if not cron_secret or auth_header != f"Bearer {cron_secret}":
        return {"error": "Unauthorized"}, 401

    IST       = pytz.timezone("Asia/Kolkata")
    today_ist = datetime.now(IST).date()
    now_utc   = datetime.utcnow()

    sent = skipped = failed = 0

    with db_session() as session:
        active_regs = session.query(Registration).filter(
            Registration.active.is_(True),
            Registration.trial_end > now_utc,
        ).all()
        tasks = []
        for reg in active_regs:
            shop_id  = _get_shop_id(reg.phone)
            shop_row = session.query(Shop).filter_by(shop_id=shop_id).first()
            if not shop_row:
                continue
            tasks.append({
                "phone":     reg.phone,
                "shop_id":   shop_row.shop_id,
                "opt_out":   bool(shop_row.summary_opt_out),
                "last_sent": shop_row.last_summary_sent_at,
            })

    for task in tasks:
        phone   = task["phone"]
        shop_id = task["shop_id"]

        if task["opt_out"]:
            skipped += 1
            continue

        last_sent = task["last_sent"]
        if last_sent is not None:
            aware = (
                pytz.utc.localize(last_sent)
                if last_sent.tzinfo is None
                else last_sent
            )
            if aware.astimezone(IST).date() == today_ist:
                skipped += 1
                continue

        try:
            data = get_daily_summary_data(shop_id, today_ist)
            if data["today"]["total_bills"] == 0:
                skipped += 1
                continue

            message = format_daily_summary(data)
            send_text_message(phone, message)

            with db_session() as upd:
                row = upd.query(Shop).filter_by(shop_id=shop_id).first()
                if row:
                    row.last_summary_sent_at = datetime.utcnow()

            sent += 1

        except Exception as e:
            log.error(
                f"[SUMMARY FAILED] shop={shop_id}, phone={phone}, error={e}"
            )
            failed += 1

        time.sleep(0.15)

    return {"sent": sent, "skipped": skipped, "failed": failed}, 200
```

- [ ] **Step 4: Run cron endpoint tests — expect PASS**

```bash
pytest tests/test_cron_daily_summary.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Run full test suite**

```bash
pytest -v
```

Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add whatsapp_webhook.py tests/test_cron_daily_summary.py
git commit -m "feat: add POST /api/cron/daily-summary endpoint with CRON_SECRET auth"
```

---

## Task 7: Full suite + final verification

- [ ] **Step 1: Run entire test suite**

```bash
pytest -v
```

Expected: All tests PASS. No warnings about missing columns.

- [ ] **Step 2: Manually verify formatter output matches spec sample**

Run this quick check in a Python shell:

```bash
python -c "
from core.daily_summary import format_daily_summary
data = {
    'shop_name': 'Ravi Mobile Accessories',
    'has_gstin': True,
    'date': '22 Apr 2026',
    'today': {
        'total_bills': 18, 'grand_total': 34200.0,
        'sale_amount': 29000.0, 'total_gst': 5200.0,
        'returns_count': 1, 'returns_amount': 850.0,
    },
    'month': {
        'name': 'April', 'total_bills': 312,
        'grand_total': 420000.0, 'sale_amount': 356000.0,
        'total_gst': 64000.0, 'returns_count': 4,
        'returns_amount': 3200.0,
    },
}
msg = format_daily_summary(data)
print(msg)
print()
print(f'Lines: {len(msg.split(chr(10)))}')
"
```

Expected output (≤ 18 lines, month returns trimmed):
```
📊 *Today's Summary — 22 Apr 2026*

🧾 Bills: 18
💰 Sale Amount: ₹29,000
🏛 GST Collected: ₹5,200
✅ Grand Total: ₹34,200

↩️ Returns: 1 bill(s) — ₹850

🏁 Net Total: ₹33,350

📅 *April So Far*

🧾 Bills: 312
💰 Sale Amount: ₹3.6L
🏛 GST Collected: ₹64,000
✅ Total: ₹4.2L

Lines: 18
```

- [ ] **Step 3: Final commit**

```bash
git add .
git commit -m "feat: daily summary feature complete — formatter, service, cron endpoint, summary command"
```

---

## Post-Implementation: External Setup (manual, not code)

After deploying to Railway:

1. **Set `CRON_SECRET` env var** in Railway dashboard → Settings → Variables. Use a random 32+ char string.
2. **Configure cron-job.org:**
   - URL: `https://<your-railway-domain>/api/cron/daily-summary`
   - Method: POST
   - Schedule: `30 15 * * *` (15:30 UTC = 9:00 PM IST)
   - Header: `Authorization: Bearer <your CRON_SECRET>`
3. **Add DB columns in production** (one-time, since SQLAlchemy won't auto-add columns):
   ```sql
   ALTER TABLE shops ADD COLUMN last_summary_sent_at TIMESTAMP;
   ALTER TABLE shops ADD COLUMN summary_opt_out BOOLEAN DEFAULT FALSE;
   ```
   Or set `DEV_MODE=True` temporarily (WARNING: drops all data — only on a fresh prod DB).
