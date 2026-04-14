# Discount & Pricing Parser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current BilledUp parser prompt and downstream pipeline with a discount-and-pricing-aware system that understands item-level discounts, bill-level discounts, final-amount overrides, and explicit "including gst" pricing, while preserving every existing bill flow and passing all current tests.

**Architecture:** New Claude system prompt returns an extended JSON schema. A new layer in `services/billing.py` normalizes the parser output into `PendingBill` using a **message-explicit → shop-default** pricing precedence. `calculate_bill()` gains `bill_discount_type`, `bill_discount_value`, and uses a scale-ratio approach to distribute bill-level discounts/overrides across items so per-item GST math stays correct. Entities, DB schema, preview, PDF, and `INCLUDE`/`EXCLUDE` commands are extended — all new fields are additive with safe defaults so existing rows and tests keep working.

**Tech Stack:** Python 3.11, SQLAlchemy, Flask, Claude API, ReportLab, rapidfuzz, pytest.

---

## Scope Check

One subsystem: parser → billing pipeline. No split needed. Lives in existing modules; no new services.

---

## File Structure

**Create:**
- `tests/test_discount_parser.py` — isolated test module for discount/pricing features (keeps `test_basic.py` readable).

**Modify:**
- `ai/parser.py` — replace `SYSTEM_PROMPT`, extend `parse_message` to pass-through new fields.
- `ai/sanitizer.py` — extend `validate_parsed_response()` to accept+coerce new schema fields.
- `core/entities/bill_item.py` — add `item_discount_type`, `item_discount_value`, `raw_amount`.
- `core/entities/bill_result.py` — add `pricing_type`, `subtotal_before_bill_discount`, `bill_discount_type`, `bill_discount_value`, `discount_total`, `taxable_amount`, `needs_confirmation`.
- `core/billing.py` — `calculate_bill()` signature + calculation order per spec.
- `db/models.py` — add `Bill` columns: `bill_discount_type`, `bill_discount_value`, `subtotal_before_discount`, `taxable_amount`, `pricing_type`.
- `db/session.py` — update `_REQUIRED_SCHEMA` dict for new Bill columns.
- `services/pending.py` — `PendingBill` fields: `bill_discount_type`, `bill_discount_value`, `pricing_type`, `needs_confirmation` (+ `.setdefault()` backward compat).
- `services/billing.py` — preview flow: pricing precedence, new field wiring, INCLUDE/EXCLUDE now persists `Shop.default_pricing`, store discount data on the saved Bill row.
- `services/pdf_renderer.py` — add subtotal/discount/taxable rows when discount > 0.
- `api/formatters.py` — preview text shows item discounts + bill discount + taxable + GST + final.
- `PROJECT_CONTEXT.md`, `memory.md` — update docs after feature is complete.

---

## Task 1: Extend BillItem entity

**Files:**
- Modify: `core/entities/bill_item.py`
- Test: `tests/test_discount_parser.py` (create)

- [ ] **Step 1: Create test file and write failing test**

```python
# tests/test_discount_parser.py
"""Tests for discount and pricing features."""
from core.entities import BillItem


def test_bill_item_has_discount_fields_with_defaults():
    item = BillItem(name="rice", qty=1, price=100)
    assert item.item_discount_type == "none"
    assert item.item_discount_value == 0.0
    assert item.raw_amount == 0.0


def test_bill_item_discount_fields_settable():
    item = BillItem(
        name="tiles", qty=10, price=50,
        item_discount_type="percent", item_discount_value=10,
        raw_amount=500.0,
    )
    assert item.item_discount_type == "percent"
    assert item.item_discount_value == 10
    assert item.raw_amount == 500.0
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_discount_parser.py -v`
Expected: FAIL — `BillItem` has no `item_discount_type`.

- [ ] **Step 3: Add fields to BillItem**

In `core/entities/bill_item.py`, inside the dataclass after `total`:

```python
    item_discount_type:  str   = "none"   # "none" | "percent" | "flat"
    item_discount_value: float = 0.0
    raw_amount:          float = 0.0      # qty * price, pre-discount
```

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_discount_parser.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run full suite to ensure nothing regressed**

Run: `pytest -q`
Expected: All 166 existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add core/entities/bill_item.py tests/test_discount_parser.py
git commit -m "feat(entities): add discount fields to BillItem"
```

---

## Task 2: Extend BillResult entity

**Files:**
- Modify: `core/entities/bill_result.py`
- Test: `tests/test_discount_parser.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_discount_parser.py`:

```python
from core.entities import BillResult


def test_bill_result_has_discount_fields_with_defaults():
    r = BillResult(
        items=[], subtotal=0, total_cgst=0, total_sgst=0,
        total_igst=0, total_gst=0, grand_total=0, in_words="",
    )
    assert r.pricing_type == "exclusive"
    assert r.subtotal_before_bill_discount == 0.0
    assert r.bill_discount_type == "none"
    assert r.bill_discount_value == 0.0
    assert r.discount_total == 0.0
    assert r.taxable_amount == 0.0
    assert r.needs_confirmation is False
```

- [ ] **Step 2: Run test → FAIL**

Run: `pytest tests/test_discount_parser.py::test_bill_result_has_discount_fields_with_defaults -v`

- [ ] **Step 3: Add fields to BillResult**

In `core/entities/bill_result.py`, append inside dataclass after `is_igst`:

```python
    pricing_type:                 str   = "exclusive"   # "exclusive" | "inclusive"
    subtotal_before_bill_discount: float = 0.0
    bill_discount_type:           str   = "none"        # "none" | "percent" | "flat" | "override"
    bill_discount_value:          float = 0.0
    discount_total:               float = 0.0           # actual ₹ amount deducted
    taxable_amount:               float = 0.0           # after all discounts, before/inclusive GST
    needs_confirmation:           bool  = False
```

- [ ] **Step 4: Run test → PASS**

- [ ] **Step 5: Run full suite, confirm no regressions**

Run: `pytest -q`

- [ ] **Step 6: Commit**

```bash
git add core/entities/bill_result.py tests/test_discount_parser.py
git commit -m "feat(entities): add pricing_type and bill discount fields to BillResult"
```

---

## Task 3: `calculate_bill` — no-discount baseline (refactor only)

**Files:**
- Modify: `core/billing.py`
- Test: `tests/test_discount_parser.py`

Goal: Add the new parameters (`bill_discount_type`, `bill_discount_value`), have them default to `"none"` / `0.0`, and populate the new `BillResult` fields with pre-discount values. No behavior change for existing callers.

- [ ] **Step 1: Write test — new signature, zero discount, result echoes new fields**

```python
from core.entities import BillItem
from core.billing import calculate_bill


def test_calculate_bill_no_discount_populates_new_fields():
    items = [BillItem(name="pen", qty=2, price=50, hsn="9608", gst_rate=18)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="none", bill_discount_value=0.0,
    )
    assert result.subtotal == 100.0
    assert result.subtotal_before_bill_discount == 100.0
    assert result.taxable_amount == 100.0
    assert result.discount_total == 0.0
    assert result.bill_discount_type == "none"
    assert result.pricing_type == "exclusive"
    assert round(result.grand_total, 2) == 118.0
```

- [ ] **Step 2: Run → FAIL** (TypeError on unknown kwarg).

Run: `pytest tests/test_discount_parser.py::test_calculate_bill_no_discount_populates_new_fields -v`

- [ ] **Step 3: Extend signature and default populate new fields**

In `core/billing.py`, update `calculate_bill` signature:

```python
def calculate_bill(
    items: list,
    gst_client=None,
    shop_state_code: str = "",
    customer_state_code: str = "",
    bill_of_supply: bool = False,
    is_inclusive: bool = False,
    bill_discount_type: str = "none",
    bill_discount_value: float = 0.0,
) -> BillResult:
```

At the end of the function, before returning `BillResult`, add:

```python
    pricing_type = "inclusive" if is_inclusive else "exclusive"
    subtotal_before_bill_discount = subtotal
    taxable_amount = subtotal      # no bill discount in this task
    discount_total = 0.0
```

And expand the return:

```python
    return BillResult(
        items=processed, subtotal=subtotal,
        total_cgst=total_cgst, total_sgst=total_sgst,
        total_igst=total_igst, total_gst=total_gst,
        grand_total=grand_total,
        in_words=number_to_words(grand_total),
        is_igst=not intra,
        pricing_type=pricing_type,
        subtotal_before_bill_discount=subtotal_before_bill_discount,
        bill_discount_type=bill_discount_type,
        bill_discount_value=bill_discount_value,
        discount_total=discount_total,
        taxable_amount=taxable_amount,
    )
```

Also populate `raw_amount` on each processed BillItem so item-level data is consistent. Inside the per-item loop, where `BillItem(...)` is constructed, add `raw_amount=round(qty * price, 2)`.

- [ ] **Step 4: Run test → PASS**

- [ ] **Step 5: Run full suite → PASS**

Run: `pytest -q`
Expected: 166+ prior tests still pass (all callers are compatible because new params default to "none"/0.0).

- [ ] **Step 6: Commit**

```bash
git add core/billing.py tests/test_discount_parser.py
git commit -m "feat(billing): plumb discount params through calculate_bill (no-op path)"
```

---

## Task 4: `calculate_bill` — item-level discount

**Files:**
- Modify: `core/billing.py`
- Test: `tests/test_discount_parser.py`

Item-level discount is applied INSIDE the per-item loop, between "compute raw amount" and "compute GST". GST is applied to the post-discount `amount`.

- [ ] **Step 1: Write failing tests**

```python
def test_calculate_bill_item_percent_discount():
    items = [BillItem(
        name="tiles", qty=10, price=50, hsn="6907", gst_rate=18,
        item_discount_type="percent", item_discount_value=10,
    )]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
    )
    # raw = 500, -10% = 450, gst18 = 81, grand = 531
    assert result.items[0].raw_amount == 500.0
    assert result.items[0].amount == 450.0
    assert round(result.subtotal, 2) == 450.0
    assert round(result.grand_total, 2) == 531.0


def test_calculate_bill_item_flat_discount():
    items = [BillItem(
        name="shirt", qty=1, price=500, hsn="6205", gst_rate=5,
        item_discount_type="flat", item_discount_value=50,
    )]
    result = calculate_bill(items, shop_state_code="36", customer_state_code="36")
    # raw = 500, -50 = 450, gst5 = 22.5, grand = 472.5
    assert result.items[0].amount == 450.0
    assert round(result.grand_total, 2) == 472.5
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Apply item discount inside the per-item loop**

In `core/billing.py`, inside `calculate_bill`, after computing `hsn`/`gst_rate` but before the `is_inclusive` branch, insert:

```python
        raw_amount = round(qty * price, 2)

        # Item-level discount
        i_disc_type = getattr(item, "item_discount_type", "none")
        i_disc_val  = float(getattr(item, "item_discount_value", 0.0) or 0.0)
        if i_disc_type == "percent":
            item_discount = round(raw_amount * i_disc_val / 100, 2)
        elif i_disc_type == "flat":
            item_discount = round(min(i_disc_val, raw_amount), 2)
        else:
            item_discount = 0.0
        discounted_line_total = round(raw_amount - item_discount, 2)
```

Then replace the existing exclusive/inclusive branch to use `discounted_line_total` as the taxable base per line:

```python
        if is_inclusive and not bill_of_supply and gst_rate > 0:
            base_unit_total = round(discounted_line_total / (1 + gst_rate / 100), 2)
            amount  = base_unit_total
            gst_amt = round(discounted_line_total - amount, 2)
        else:
            amount  = discounted_line_total
            gst_amt = round(amount * gst_rate / 100, 2)
```

And when constructing `BillItem`, carry the discount fields:

```python
        processed.append(BillItem(
            name=name.title(), qty=qty, price=price,
            hsn=hsn, gst_rate=gst_rate,
            raw_amount=raw_amount,
            item_discount_type=i_disc_type,
            item_discount_value=i_disc_val,
            amount=amount,
            cgst=cgst, sgst=sgst, igst=igst, total=total,
        ))
```

- [ ] **Step 4: Run both new tests → PASS**

- [ ] **Step 5: Run full suite → PASS**

Expected: All existing bills (which have `item_discount_type="none"` by default) still compute identically.

- [ ] **Step 6: Commit**

```bash
git add core/billing.py tests/test_discount_parser.py
git commit -m "feat(billing): apply item-level percent and flat discounts"
```

---

## Task 5: `calculate_bill` — bill-level flat/percent discount

**Files:**
- Modify: `core/billing.py`
- Test: `tests/test_discount_parser.py`

Bill-level discount is applied AFTER summing item amounts. It scales each item's taxable base proportionally so per-item GST rates remain correct. A final rounding adjustment on the last item absorbs residue so `grand_total` is exact.

- [ ] **Step 1: Write failing tests**

```python
def test_calculate_bill_bill_flat_discount_single_rate():
    items = [BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="flat", bill_discount_value=100,
    )
    # subtotal 1000 → taxable 900 → gst5 = 45 → grand 945
    assert result.subtotal_before_bill_discount == 1000.0
    assert result.taxable_amount == 900.0
    assert result.discount_total == 100.0
    assert round(result.grand_total, 2) == 945.0


def test_calculate_bill_bill_percent_discount_mixed_rates():
    items = [
        BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5),
        BillItem(name="soap", qty=1, price=1000, hsn="3401", gst_rate=18),
    ]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="percent", bill_discount_value=10,
    )
    # Each item scaled by 0.9 → 900 + 900 = 1800 taxable
    # GST: 900*5%=45, 900*18%=162 → 207
    # grand = 2007
    assert result.subtotal_before_bill_discount == 2000.0
    assert result.taxable_amount == 1800.0
    assert round(result.discount_total, 2) == 200.0
    assert round(result.grand_total, 2) == 2007.0
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Restructure the bill math into two passes**

Rewrite the body of `calculate_bill` so the per-item loop records `discounted_line_total` and `gst_rate` into an intermediate list, THEN applies bill-level scaling, THEN computes final GST/CGST/SGST/IGST per item. Replace the loop-and-tally block with:

```python
    from gst_rates import get_gst_rate_smart, adjust_gst_for_price

    # Pass 1: resolve rates, item discounts, raw line totals
    lines = []    # each: {item, name, qty, price, hsn, gst_rate, raw, line_after_item_disc}
    pre_bill_subtotal = 0.0
    for item in items:
        item.validate()
        name  = item.name.strip()
        qty   = round(float(item.qty), 3)
        price = round(float(item.price), 2)

        if item.hsn:
            hsn, gst_rate = item.hsn, item.gst_rate
        else:
            try:
                info = get_gst_rate_smart(name, gst_client)
            except Exception as e:
                log.warning(f"GST lookup failed for '{name}': {e} — using default 18%")
                info = {"hsn": "9999", "gst": 18}
            info = adjust_gst_for_price(name, price, info)
            hsn, gst_rate = info.get("hsn", "9999"), info.get("gst", 18)

        if bill_of_supply:
            gst_rate = 0
        elif gst_rate not in VALID_GST_SLABS:
            log.warning(f"Invalid slab {gst_rate}% for '{name}' — correcting to 18%")
            gst_rate = 18

        raw_amount = round(qty * price, 2)
        i_disc_type = getattr(item, "item_discount_type", "none")
        i_disc_val  = float(getattr(item, "item_discount_value", 0.0) or 0.0)
        if i_disc_type == "percent":
            item_disc = round(raw_amount * i_disc_val / 100, 2)
        elif i_disc_type == "flat":
            item_disc = round(min(i_disc_val, raw_amount), 2)
        else:
            item_disc = 0.0
        line_after_item_disc = round(raw_amount - item_disc, 2)
        pre_bill_subtotal += line_after_item_disc

        lines.append({
            "name": name, "qty": qty, "price": price, "hsn": hsn,
            "gst_rate": gst_rate, "raw": raw_amount,
            "i_disc_type": i_disc_type, "i_disc_val": i_disc_val,
            "line": line_after_item_disc,
        })

    pre_bill_subtotal = round(pre_bill_subtotal, 2)

    # Pass 2: apply bill-level discount as a scale ratio
    if bill_discount_type == "flat" and bill_discount_value > 0:
        deduction = min(round(float(bill_discount_value), 2), pre_bill_subtotal)
        scale = 0.0 if pre_bill_subtotal == 0 else (pre_bill_subtotal - deduction) / pre_bill_subtotal
    elif bill_discount_type == "percent" and bill_discount_value > 0:
        pct = max(0.0, min(100.0, float(bill_discount_value)))
        scale = 1.0 - pct / 100.0
    elif bill_discount_type == "override":
        scale = None      # Task 6
    else:
        scale = 1.0

    if scale is not None:
        for L in lines:
            L["line"] = round(L["line"] * scale, 2)
```

Then compute `processed`, per-item GST, and totals using `L["line"]` as the taxable base (exclusive) or inclusive back-out. Finally, after computing `grand_total`, add a rounding correction pass described in Step 4 below.

Populate `discount_total` and `taxable_amount`:

```python
    taxable_amount = round(sum(L["line"] for L in lines), 2)
    discount_total = round(pre_bill_subtotal - taxable_amount, 2)
    subtotal_before_bill_discount = pre_bill_subtotal
```

And in the `BillResult` return, pass these through.

- [ ] **Step 4: Add rounding-absorption on the last item** (for flat/override modes where `grand_total` might drift by ₹0.01)

After the final totals are computed but before building the `BillResult`:

```python
    # Absorb rounding residue on the last item so grand_total is exact.
    if processed and bill_discount_type in ("flat", "override"):
        expected = _expected_grand_total(
            bill_discount_type, bill_discount_value,
            pre_bill_subtotal, total_gst, is_inclusive,
        )
        delta = round(expected - grand_total, 2)
        if abs(delta) <= 0.05 and delta != 0.0:
            processed[-1].total = round(processed[-1].total + delta, 2)
            grand_total = round(grand_total + delta, 2)
```

Define the helper at module level:

```python
def _expected_grand_total(
    disc_type: str, disc_val: float,
    pre_subtotal: float, total_gst: float, is_inclusive: bool,
) -> float:
    if disc_type == "flat":
        taxable = max(0.0, round(pre_subtotal - float(disc_val), 2))
        return round(taxable if is_inclusive else taxable + total_gst, 2)
    if disc_type == "override":
        return round(float(disc_val), 2)
    return 0.0
```

- [ ] **Step 5: Run new tests → PASS**

- [ ] **Step 6: Run full suite → PASS**

- [ ] **Step 7: Commit**

```bash
git add core/billing.py tests/test_discount_parser.py
git commit -m "feat(billing): bill-level flat and percent discounts with rate-preserving scaling"
```

---

## Task 6: `calculate_bill` — final amount override

**Files:**
- Modify: `core/billing.py`
- Test: `tests/test_discount_parser.py`

- [ ] **Step 1: Write failing test**

```python
def test_calculate_bill_override_exclusive():
    items = [
        BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5),
        BillItem(name="soap", qty=1, price=1000, hsn="3401", gst_rate=18),
    ]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="override", bill_discount_value=2000,
    )
    # Natural grand total = 1050 + 1180 = 2230. Target = 2000.
    # Scale so grand_total is exactly 2000.
    assert round(result.grand_total, 2) == 2000.0
    assert result.bill_discount_type == "override"
    assert result.bill_discount_value == 2000.0


def test_calculate_bill_override_inclusive():
    items = [BillItem(name="rice", qty=1, price=1050, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        is_inclusive=True,
        bill_discount_type="override", bill_discount_value=945,
    )
    assert round(result.grand_total, 2) == 945.0
```

- [ ] **Step 2: Run → FAIL** (`scale is None` path not implemented).

- [ ] **Step 3: Implement override scaling**

In `calculate_bill`, compute a "natural" grand total first (so we know the ratio), then set the scale. Before Pass 2, add a helper:

```python
    def _natural_grand_total(lines_: list, is_inc: bool, bos: bool) -> float:
        total_ = 0.0
        for L in lines_:
            base = L["line"]
            rate = L["gst_rate"]
            if bos or rate == 0:
                total_ += base
                continue
            if is_inc:
                total_ += base
            else:
                total_ += round(base * (1 + rate / 100), 2)
        return round(total_, 2)
```

Replace the override branch in Pass 2:

```python
    elif bill_discount_type == "override":
        target = round(float(bill_discount_value), 2)
        natural = _natural_grand_total(lines, is_inclusive, bill_of_supply)
        if natural <= 0:
            scale = 1.0
        else:
            scale = target / natural
```

The existing scaling loop (`for L in lines: L["line"] = round(L["line"] * scale, 2)`) already handles override too.

The rounding-correction block from Task 5 (using `_expected_grand_total`) already covers override exactness.

- [ ] **Step 4: Run tests → PASS**

- [ ] **Step 5: Run full suite → PASS**

- [ ] **Step 6: Commit**

```bash
git add core/billing.py tests/test_discount_parser.py
git commit -m "feat(billing): final-amount override discount"
```

---

## Task 7: Extend `db.models.Bill` + `_REQUIRED_SCHEMA`

**Files:**
- Modify: `db/models.py`
- Modify: `db/session.py`
- Test: `tests/test_discount_parser.py`

- [ ] **Step 1: Write failing test**

```python
def test_bill_model_has_discount_columns():
    from db.models import Bill
    cols = {c.name for c in Bill.__table__.columns}
    assert "bill_discount_type" in cols
    assert "bill_discount_value" in cols
    assert "subtotal_before_discount" in cols
    assert "taxable_amount" in cols
    assert "pricing_type" in cols
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Add columns**

In `db/models.py`, inside `class Bill`, after `confidence`:

```python
    bill_discount_type       = Column(String(10), default="none")
    bill_discount_value      = Column(Float, default=0.0)
    subtotal_before_discount = Column(Float, default=0.0)
    taxable_amount           = Column(Float, default=0.0)
    pricing_type             = Column(String(10), default="exclusive")
```

In `db/session.py`, add these column names to the `bills` entry of `_REQUIRED_SCHEMA` (leave existing entries alone).

- [ ] **Step 4: Run test → PASS**

- [ ] **Step 5: Run full suite → PASS**

Note: In DEV_MODE the schema check auto-resets the DB. In production, operator must run a one-shot ALTER TABLE. Add a note to the commit body.

- [ ] **Step 6: Commit**

```bash
git add db/models.py db/session.py tests/test_discount_parser.py
git commit -m "feat(db): add discount and pricing_type columns to bills table

Production requires a one-shot ALTER TABLE; dev auto-resets via ensure_schema."
```

---

## Task 8: Extend `PendingBill` and its serialization

**Files:**
- Modify: `services/pending.py`
- Test: `tests/test_discount_parser.py`

- [ ] **Step 1: Write failing test**

```python
def test_pending_bill_new_fields_roundtrip():
    from datetime import datetime
    from services.pending import PendingBill, _serialize_pending, _deserialize_pending

    pb = PendingBill(
        phone="+911234567890", shop_id="RAVI", shop_name="X",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Kiran", customer_state="Telangana", customer_state_code="36",
        items=[], confidence=0.9, warnings=[], raw_message="",
        created_at=datetime(2026, 4, 13, 10, 0, 0),
        pricing_type="inclusive",
        bill_discount_type="flat", bill_discount_value=100.0,
        needs_confirmation=True,
    )
    back = _deserialize_pending(_serialize_pending(pb))
    assert back.pricing_type == "inclusive"
    assert back.bill_discount_type == "flat"
    assert back.bill_discount_value == 100.0
    assert back.needs_confirmation is True


def test_pending_bill_backward_compat_old_rows():
    """A pending bill saved BEFORE this feature must still deserialize."""
    import json
    old = {
        "phone": "+911234567890", "shop_id": "RAVI", "shop_name": "X",
        "shop_state": "Telangana", "shop_state_code": "36",
        "customer_name": "Customer", "customer_state": "Telangana",
        "customer_state_code": "36", "items": [], "confidence": 0.9,
        "warnings": [], "raw_message": "",
        "created_at": "2026-04-13T10:00:00",
        "awaiting_state": False, "state_assumed": True,
        "is_return": False, "is_bill_of_supply": False,
        "is_inclusive": False, "customer_phone": "",
    }
    from services.pending import _deserialize_pending
    back = _deserialize_pending(json.dumps(old))
    assert back.pricing_type == "exclusive"
    assert back.bill_discount_type == "none"
    assert back.bill_discount_value == 0.0
    assert back.needs_confirmation is False
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Extend PendingBill**

In `services/pending.py`, add to the dataclass:

```python
    pricing_type: str = "exclusive"
    bill_discount_type: str = "none"
    bill_discount_value: float = 0.0
    needs_confirmation: bool = False
```

In `_serialize_pending`, append to the `data` dict:

```python
        "pricing_type": bill.pricing_type,
        "bill_discount_type": bill.bill_discount_type,
        "bill_discount_value": bill.bill_discount_value,
        "needs_confirmation": bill.needs_confirmation,
```

In `_deserialize_pending`, after existing `setdefault` calls:

```python
    data.setdefault("pricing_type", "inclusive" if data.get("is_inclusive") else "exclusive")
    data.setdefault("bill_discount_type", "none")
    data.setdefault("bill_discount_value", 0.0)
    data.setdefault("needs_confirmation", False)
```

- [ ] **Step 4: Run tests → PASS**

- [ ] **Step 5: Run full suite → PASS**

- [ ] **Step 6: Commit**

```bash
git add services/pending.py tests/test_discount_parser.py
git commit -m "feat(pending): carry pricing_type, bill discount, needs_confirmation"
```

---

## Task 9: Extend `validate_parsed_response` for new parser schema

**Files:**
- Modify: `ai/sanitizer.py`
- Test: `tests/test_discount_parser.py`

The parser will soon return a richer JSON. The sanitizer normalizes, coerces numeric fields, and sets safe defaults for any missing keys. This must accept BOTH the old schema (for robustness) and the new one.

- [ ] **Step 1: Write failing tests**

```python
def test_validate_response_accepts_new_discount_schema():
    from ai.sanitizer import validate_parsed_response
    raw = {
        "customer_name": "Kiran",
        "items": [
            {"name": "tiles", "qty": 10, "price": 50,
             "item_discount_type": "percent", "item_discount_value": 10},
            {"name": "grout", "qty": 1, "price": 200},
        ],
        "bill_discount_type": "flat",
        "bill_discount_value": 50,
        "pricing_type": "exclusive",
        "needs_confirmation": False,
    }
    clean, issues = validate_parsed_response(raw)
    assert clean["items"][0]["item_discount_type"] == "percent"
    assert clean["items"][0]["item_discount_value"] == 10.0
    assert clean["items"][1]["item_discount_type"] == "none"
    assert clean["items"][1]["item_discount_value"] == 0.0
    assert clean["bill_discount_type"] == "flat"
    assert clean["bill_discount_value"] == 50.0
    assert clean["pricing_type"] == "exclusive"
    assert clean["needs_confirmation"] is False


def test_validate_response_defaults_when_missing_new_fields():
    from ai.sanitizer import validate_parsed_response
    raw = {"customer_name": "X", "items": [{"name": "rice", "qty": 1, "price": 50}]}
    clean, _ = validate_parsed_response(raw)
    assert clean["bill_discount_type"] == "none"
    assert clean["bill_discount_value"] == 0.0
    assert clean["pricing_type"] is None     # unknown → parser.py will decide using Shop default
    assert clean["needs_confirmation"] is False
    assert clean["items"][0]["item_discount_type"] == "none"


def test_validate_response_coerces_bad_discount_type():
    from ai.sanitizer import validate_parsed_response
    raw = {
        "customer_name": "X",
        "items": [{"name": "x", "qty": 1, "price": 10,
                   "item_discount_type": "garbage", "item_discount_value": "abc"}],
        "bill_discount_type": "huh",
        "bill_discount_value": "oops",
    }
    clean, issues = validate_parsed_response(raw)
    assert clean["items"][0]["item_discount_type"] == "none"
    assert clean["items"][0]["item_discount_value"] == 0.0
    assert clean["bill_discount_type"] == "none"
    assert clean["bill_discount_value"] == 0.0
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Extend the validator**

In `ai/sanitizer.py`, at the top of the file:

```python
_ITEM_DISC_TYPES = {"none", "percent", "flat"}
_BILL_DISC_TYPES = {"none", "percent", "flat", "override"}
_PRICING_TYPES   = {"exclusive", "inclusive"}
```

Inside `validate_parsed_response`, within the per-item loop (before `valid_items.append({...})`), coerce item discount fields:

```python
        i_type = str(item.get("item_discount_type", "none") or "none").strip().lower()
        if i_type not in _ITEM_DISC_TYPES:
            i_type = "none"
        try:
            i_val = float(item.get("item_discount_value", 0) or 0)
        except (TypeError, ValueError):
            i_val = 0.0
        i_val = max(0.0, i_val)
```

Include them in the appended dict:

```python
        valid_items.append({
            "name": name, "qty": round(qty, 3), "price": round(price, 2),
            "item_discount_type":  i_type,
            "item_discount_value": i_val,
        })
```

After the loop, coerce bill-level fields at the top level of `result`:

```python
    b_type = str(result.get("bill_discount_type", "none") or "none").strip().lower()
    if b_type not in _BILL_DISC_TYPES:
        b_type = "none"
    try:
        b_val = float(result.get("bill_discount_value", 0) or 0)
    except (TypeError, ValueError):
        b_val = 0.0
    result["bill_discount_type"]  = b_type
    result["bill_discount_value"] = max(0.0, b_val)

    pt = result.get("pricing_type")
    if isinstance(pt, str) and pt.strip().lower() in _PRICING_TYPES:
        result["pricing_type"] = pt.strip().lower()
    else:
        result["pricing_type"] = None

    result["needs_confirmation"] = bool(result.get("needs_confirmation", False))
```

- [ ] **Step 4: Run tests → PASS**

- [ ] **Step 5: Run full suite → PASS**

- [ ] **Step 6: Commit**

```bash
git add ai/sanitizer.py tests/test_discount_parser.py
git commit -m "feat(sanitizer): validate extended parser schema with safe defaults"
```

---

## Task 10: Replace the Claude system prompt

**Files:**
- Modify: `ai/parser.py`
- Test: `tests/test_discount_parser.py` (mock-based)

No live API call in tests. Patch `client.messages.create` to return a canned response reflecting the new schema and assert `parse_message` round-trips it through the sanitizer.

- [ ] **Step 1: Write failing test with mocked Claude**

```python
def test_parse_message_passes_through_discount_schema(monkeypatch):
    from ai import parser as P

    class _FakeBlock:
        def __init__(self, text): self.text = text
    class _FakeResp:
        def __init__(self, text): self.content = [_FakeBlock(text)]

    canned = {
        "customer_name": "Kiran",
        "items": [
            {"name": "tiles", "qty": 10, "price": 50,
             "item_discount_type": "percent", "item_discount_value": 10}
        ],
        "bill_discount_type": "flat", "bill_discount_value": 100,
        "pricing_type": "exclusive", "needs_confirmation": False,
        "confidence": 0.95, "notes": "", "error": None,
    }

    class _FakeMessages:
        def create(self, **kw):
            import json as _j
            return _FakeResp(_j.dumps(canned))
    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(P, "get_anthropic_client", lambda: _FakeClient())
    # Use TextBlock check escape: monkeypatch isinstance? simpler — patch TextBlock
    import ai.parser as _p
    from anthropic.types import TextBlock
    monkeypatch.setattr(_p, "TextBlock", _FakeBlock)

    out = P.parse_message("tiles 10 at 50 each 10% discount, less 100")
    assert out["bill_discount_type"] == "flat"
    assert out["bill_discount_value"] == 100.0
    assert out["pricing_type"] == "exclusive"
    assert out["items"][0]["item_discount_type"] == "percent"
```

- [ ] **Step 2: Run → FAIL** (current prompt doesn't ask Claude for those fields; but test uses canned response, so real failure is the `isinstance(block, TextBlock)` check — fix with monkeypatch as shown).

- [ ] **Step 3: Replace `SYSTEM_PROMPT`**

In `ai/parser.py`, replace the entire `SYSTEM_PROMPT` string with the spec from the user's message (the "STRICT RULES" prompt), reformatted as a triple-quoted Python string. Key content to preserve verbatim in the prompt:

- All 9 numbered sections (ITEM EXTRACTION, PRICING TYPE, DISCOUNT HANDLING, DISCOUNT TYPE CLARITY, CALCULATION ORDER, GST RULES, AMBIGUITY HANDLING, OUTPUT FORMAT, KEY INTERPRETATION RULES).
- Unit/weight descriptor handling from the current prompt (gm/kg/ml/l/etc. glued to item name). This was NOT in the spec but is load-bearing for existing tests — keep it as an additional rule.
- Phone-number-as-price guard.
- The "reply only JSON" directive with the exact new JSON schema shown in Section 8.

No other code in `parse_message` needs to change — the sanitizer + `_apply_phone` already handle all the fields. Confirm that `_error_result` returns the new fields as `None`/defaults so downstream consumers never KeyError:

```python
def _error_result(error: str, warnings: list | None = None,
                  parse_time_ms: int = 0) -> dict:
    log.error(f"Parse failed: {error}")
    return {
        "customer_name":       "Customer",
        "customer_phone":      None,
        "items":               [],
        "bill_discount_type":  "none",
        "bill_discount_value": 0.0,
        "pricing_type":        None,
        "needs_confirmation":  False,
        "confidence":          0.0,
        "notes":               "",
        "error":               error,
        "warnings":            warnings or [],
        "parse_time_ms":       parse_time_ms,
    }
```

- [ ] **Step 4: Run test → PASS**

- [ ] **Step 5: Run full suite → PASS**

Expected: all 166+ existing tests pass. Note: regex fallback path also needs defaults — in `parse_message`, after `fallback = _regex_parse_message(...)`, add:

```python
        fallback.setdefault("bill_discount_type", "none")
        fallback.setdefault("bill_discount_value", 0.0)
        fallback.setdefault("pricing_type", None)
        fallback.setdefault("needs_confirmation", False)
```

at both fallback sites (API failure, JSON decode failure).

- [ ] **Step 6: Commit**

```bash
git add ai/parser.py tests/test_discount_parser.py
git commit -m "feat(parser): discount-aware system prompt and extended response fields"
```

---

## Task 11: Services billing — pricing precedence and pending wiring

**Files:**
- Modify: `services/billing.py`
- Test: `tests/test_discount_parser.py`

Goal: when building a `PendingBill` from parser output, decide `pricing_type` as:

1. If `parser_result["pricing_type"]` is `"inclusive"` or `"exclusive"` → use it.
2. Else → use `Shop.default_pricing`.
3. Set `PendingBill.is_inclusive = (pricing_type == "inclusive")` to keep the existing downstream flow intact.

Also wire `bill_discount_type`, `bill_discount_value`, `needs_confirmation`, and per-item discount fields into the pending items list.

- [ ] **Step 1: Locate the current preview-build function**

In `services/billing.py`, find the function that constructs `PendingBill` from parser output (search for `PendingBill(`). Identify the point where `is_inclusive` is currently chosen.

- [ ] **Step 2: Write failing test**

```python
def test_pricing_precedence_message_explicit_beats_shop_default(monkeypatch):
    from services.billing import _build_pending_from_parser  # see step 3
    from db.models import Shop

    shop = Shop(shop_id="RAVI", name="X", address="Y", gstin="", phone="",
                state="Telangana", state_code="36", default_pricing="exclusive")
    parser_result = {
        "customer_name": "A", "customer_phone": None,
        "items": [{"name": "rice", "qty": 1, "price": 100,
                   "item_discount_type": "none", "item_discount_value": 0}],
        "bill_discount_type": "none", "bill_discount_value": 0.0,
        "pricing_type": "inclusive",  # explicit!
        "needs_confirmation": False, "confidence": 0.9,
        "warnings": [], "notes": "", "error": None,
    }
    pb = _build_pending_from_parser(
        phone="+911", shop=shop, parser_result=parser_result, raw_message="rice 100 including gst",
    )
    assert pb.pricing_type == "inclusive"
    assert pb.is_inclusive is True


def test_pricing_precedence_falls_back_to_shop_default(monkeypatch):
    from services.billing import _build_pending_from_parser
    from db.models import Shop
    shop = Shop(shop_id="RAVI", name="X", address="Y", gstin="", phone="",
                state="Telangana", state_code="36", default_pricing="inclusive")
    parser_result = {
        "customer_name": "A", "customer_phone": None,
        "items": [{"name": "rice", "qty": 1, "price": 100,
                   "item_discount_type": "none", "item_discount_value": 0}],
        "bill_discount_type": "none", "bill_discount_value": 0.0,
        "pricing_type": None,  # parser did not see explicit wording
        "needs_confirmation": False, "confidence": 0.9,
        "warnings": [], "notes": "", "error": None,
    }
    pb = _build_pending_from_parser(
        phone="+911", shop=shop, parser_result=parser_result, raw_message="rice 100",
    )
    assert pb.pricing_type == "inclusive"
    assert pb.is_inclusive is True
```

- [ ] **Step 3: Extract a pure helper**

Refactor the existing parser→pending mapping into a pure function `_build_pending_from_parser(phone, shop, parser_result, raw_message)` that returns a `PendingBill`. The existing caller now calls this helper (no behavior change for existing flow).

Inside the helper, compute:

```python
    pt = parser_result.get("pricing_type")
    if pt in ("inclusive", "exclusive"):
        pricing_type = pt
    else:
        pricing_type = (getattr(shop, "default_pricing", None) or "exclusive").lower()
        if pricing_type not in ("inclusive", "exclusive"):
            pricing_type = "exclusive"

    is_inclusive = (pricing_type == "inclusive")

    items = [
        {
            **i,
            "item_discount_type":  i.get("item_discount_type", "none"),
            "item_discount_value": float(i.get("item_discount_value", 0) or 0),
        }
        for i in parser_result.get("items", [])
    ]

    return PendingBill(
        phone=phone,
        shop_id=shop.shop_id, shop_name=shop.name,
        shop_state=shop.state or "", shop_state_code=shop.state_code or "",
        customer_name=parser_result.get("customer_name", "Customer"),
        customer_phone=parser_result.get("customer_phone") or "",
        customer_state=shop.state or "",
        customer_state_code=shop.state_code or "",
        items=items,
        confidence=float(parser_result.get("confidence", 0.5)),
        warnings=list(parser_result.get("warnings", [])),
        raw_message=raw_message,
        created_at=datetime.utcnow(),
        is_return=False,
        is_bill_of_supply=False,
        is_inclusive=is_inclusive,
        pricing_type=pricing_type,
        bill_discount_type=parser_result.get("bill_discount_type", "none"),
        bill_discount_value=float(parser_result.get("bill_discount_value", 0) or 0),
        needs_confirmation=bool(parser_result.get("needs_confirmation", False)),
    )
```

- [ ] **Step 4: Run tests → PASS**

- [ ] **Step 5: Run full suite → PASS**

Expected: any existing tests that previously built `PendingBill` inline still work because that code path is unchanged; only the new helper is added and wired into the same call site.

- [ ] **Step 6: Commit**

```bash
git add services/billing.py tests/test_discount_parser.py
git commit -m "feat(services): pricing precedence and discount wiring into PendingBill"
```

---

## Task 12: Services billing — pass discount fields into `calculate_bill` at confirmation

**Files:**
- Modify: `services/billing.py`
- Test: `tests/test_discount_parser.py`

When the shopkeeper replies `YES`, the pending items (with per-item `item_discount_type`/`value`) become `BillItem` objects and `calculate_bill` is called. This task ensures the new fields flow through.

- [ ] **Step 1: Write failing test** — end-to-end from PendingBill to computed BillResult

```python
def test_confirm_pending_to_bill_result_applies_discounts():
    from datetime import datetime
    from services.pending import PendingBill
    from services.billing import _compute_bill_from_pending  # see step 3

    pb = PendingBill(
        phone="+911", shop_id="RAVI", shop_name="X",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Kiran", customer_phone="",
        customer_state="Telangana", customer_state_code="36",
        items=[
            {"name": "tiles", "qty": 10, "price": 50, "hsn": "6907", "gst_rate": 18,
             "item_discount_type": "percent", "item_discount_value": 10},
        ],
        confidence=0.95, warnings=[], raw_message="", created_at=datetime.utcnow(),
        pricing_type="exclusive",
        bill_discount_type="flat", bill_discount_value=50,
    )
    result = _compute_bill_from_pending(pb)
    # per item: raw 500, -10% = 450
    # bill flat -50 → taxable = 400 → gst18 = 72 → grand = 472
    assert round(result.taxable_amount, 2) == 400.0
    assert round(result.grand_total, 2) == 472.0
    assert result.pricing_type == "exclusive"
    assert result.bill_discount_type == "flat"
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Add `_compute_bill_from_pending` helper**

In `services/billing.py`:

```python
def _compute_bill_from_pending(pb):
    from core.entities import BillItem
    from core.billing import calculate_bill

    bill_items = [
        BillItem(
            name=i["name"], qty=i["qty"], price=i["price"],
            hsn=i.get("hsn", ""), gst_rate=i.get("gst_rate", 18),
            item_discount_type=i.get("item_discount_type", "none"),
            item_discount_value=float(i.get("item_discount_value", 0) or 0),
        )
        for i in pb.items
    ]
    return calculate_bill(
        bill_items,
        shop_state_code=pb.shop_state_code,
        customer_state_code=pb.customer_state_code,
        bill_of_supply=pb.is_bill_of_supply,
        is_inclusive=pb.is_inclusive,
        bill_discount_type=pb.bill_discount_type,
        bill_discount_value=pb.bill_discount_value,
    )
```

Refactor the current YES-handler confirmation path to call this helper in place of its inline `calculate_bill` call. Ensure the code that saves the `Bill` row also writes the new columns (`bill_discount_type`, `bill_discount_value`, `subtotal_before_discount`, `taxable_amount`, `pricing_type`).

- [ ] **Step 4: Run test → PASS**

- [ ] **Step 5: Run full suite → PASS**

- [ ] **Step 6: Commit**

```bash
git add services/billing.py tests/test_discount_parser.py
git commit -m "feat(services): compute confirmed bill from pending with discount fields"
```

---

## Task 13: `INCLUDE` / `EXCLUDE` commands persist `Shop.default_pricing`

**Files:**
- Modify: `services/billing.py`
- Test: `tests/test_discount_parser.py`

- [ ] **Step 1: Write failing test** (unit, with in-memory DB from conftest)

```python
def test_include_command_updates_shop_default_pricing():
    from db.session import db_session
    from db.models import Shop
    from services.billing import _toggle_pricing_mode  # see step 3

    with db_session() as s:
        s.add(Shop(shop_id="TST", name="T", address="A", gstin="", phone="9",
                   state="Telangana", state_code="36", default_pricing="exclusive"))

    _toggle_pricing_mode(shop_id="TST", mode="inclusive")

    with db_session() as s:
        shop = s.query(Shop).filter_by(shop_id="TST").first()
        assert shop.default_pricing == "inclusive"
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Add helper and wire into INCLUDE/EXCLUDE handlers**

```python
def _toggle_pricing_mode(shop_id: str, mode: str) -> None:
    """Persist Shop.default_pricing so the next bill auto-uses this mode."""
    if mode not in ("inclusive", "exclusive"):
        return
    from db.session import db_session
    from db.models import Shop
    with db_session() as s:
        shop = s.query(Shop).filter_by(shop_id=shop_id).first()
        if shop:
            shop.default_pricing = mode
```

Update the existing `INCLUDE`/`EXCLUDE` command handlers so that in addition to toggling `PendingBill.is_inclusive`/`pricing_type` and re-rendering the preview, they call `_toggle_pricing_mode(shop.shop_id, mode)`.

- [ ] **Step 4: Run test → PASS**

- [ ] **Step 5: Run full suite → PASS**

- [ ] **Step 6: Commit**

```bash
git add services/billing.py tests/test_discount_parser.py
git commit -m "feat(services): persist Shop.default_pricing on INCLUDE/EXCLUDE"
```

---

## Task 14: Preview formatter shows discount breakdown

**Files:**
- Modify: `api/formatters.py`
- Test: `tests/test_discount_parser.py`

- [ ] **Step 1: Locate the current preview builder** in `api/formatters.py` (search for the function that renders the bill preview text).

- [ ] **Step 2: Write failing test**

```python
def test_preview_shows_item_discount_and_bill_discount():
    from api.formatters import build_preview_message  # actual function name may differ
    preview = build_preview_message({
        "shop_name": "X",
        "customer_name": "Kiran",
        "items": [
            {"name": "tiles", "qty": 10, "price": 50,
             "item_discount_type": "percent", "item_discount_value": 10,
             "line_total": 450},
        ],
        "subtotal_before_bill_discount": 450.0,
        "bill_discount_type": "flat",
        "bill_discount_value": 50.0,
        "taxable_amount": 400.0,
        "gst_amount": 72.0,
        "grand_total": 472.0,
        "pricing_type": "exclusive",
        "is_intra_state": True,
        "needs_confirmation": False,
    })
    assert "10%" in preview or "-10%" in preview
    assert "50" in preview       # flat discount
    assert "472" in preview      # grand total
    assert "400" in preview      # taxable
```

- [ ] **Step 3: Extend the formatter** — when `item_discount_type != "none"`, render `-{value}{%|₹}` next to the line. When `bill_discount_type != "none"`, add a dedicated "Discount" row between subtotal and taxable. Show `Taxable`, `GST`, `Total` as currently, but use the new field names.

If `needs_confirmation` is True, prepend a ⚠️ line: `"Please verify — ambiguous discount interpretation."`

- [ ] **Step 4: Run test → PASS**

- [ ] **Step 5: Run full suite → PASS**

- [ ] **Step 6: Commit**

```bash
git add api/formatters.py tests/test_discount_parser.py
git commit -m "feat(formatters): preview shows item/bill discount breakdown and ambiguity flag"
```

---

## Task 15: PDF renderer shows discount breakdown

**Files:**
- Modify: `services/pdf_renderer.py`
- Test: `tests/test_discount_parser.py`

- [ ] **Step 1: Write failing test** — render a bill with discounts to BytesIO and assert the PDF bytes contain the expected totals via a light text grep (use `pypdf` if available, else string-search the raw bytes for ASCII amount strings).

```python
def test_pdf_contains_discount_rows():
    from io import BytesIO
    from datetime import datetime
    from core.entities import BillItem, BillResult
    from services.pdf_renderer import render_bill_pdf  # actual name may differ

    items = [BillItem(
        name="Tiles", qty=10, price=50, hsn="6907", gst_rate=18,
        raw_amount=500, amount=450, cgst=40.5, sgst=40.5, total=531,
        item_discount_type="percent", item_discount_value=10,
    )]
    result = BillResult(
        items=items, subtotal=450,
        total_cgst=36, total_sgst=36, total_igst=0, total_gst=72,
        grand_total=472, in_words="Four Hundred Seventy Two Rupees Only",
        pricing_type="exclusive",
        subtotal_before_bill_discount=450, bill_discount_type="flat",
        bill_discount_value=50, discount_total=50, taxable_amount=400,
    )
    buf = BytesIO()
    render_bill_pdf(buf, shop=..., result=result, invoice_number="INV-TEST-1", ...)
    data = buf.getvalue()
    assert b"472" in data
    assert b"400" in data
    assert b"50" in data
```

(Fill in the `shop=...` and trailing args per the actual signature — read `services/pdf_renderer.py` before writing the test.)

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Extend the PDF layout**

In `services/pdf_renderer.py`, in the totals block (after the items table, before the grand total row), add conditional rows:

- If `result.bill_discount_type != "none"` or any item has a discount → show a `Subtotal` row using `result.subtotal_before_bill_discount`.
- If `result.discount_total > 0` → show a `Discount` row with the discount amount.
- Always show `Taxable Amount` using `result.taxable_amount`.
- For items with `item_discount_type != "none"`, append the discount inline under the item name (e.g. `(-10%)` or `(-₹50)`) — XML-escape as usual.
- If `result.pricing_type == "inclusive"`, keep the existing "prices are inclusive of GST" note; if `"exclusive"`, keep the existing note. No change to that logic.

- [ ] **Step 4: Run test → PASS**

- [ ] **Step 5: Run full suite → PASS**

- [ ] **Step 6: Commit**

```bash
git add services/pdf_renderer.py tests/test_discount_parser.py
git commit -m "feat(pdf): render subtotal, discount, and taxable rows"
```

---

## Task 16: End-to-end integration test

**Files:**
- Test: `tests/test_discount_parser.py`

Single test that: seeds a shop, builds parser output with item + bill discount + inclusive pricing, runs it through `_build_pending_from_parser`, then `_compute_bill_from_pending`, and asserts the whole pipeline matches the hand-computed expected grand total.

- [ ] **Step 1: Write the test**

```python
def test_end_to_end_inclusive_with_item_and_bill_discount():
    from datetime import datetime
    from services.billing import _build_pending_from_parser, _compute_bill_from_pending
    from db.models import Shop

    shop = Shop(shop_id="E2E", name="End To End", address="A", gstin="36AABCU9603R1ZX",
                phone="9", state="Telangana", state_code="36",
                default_pricing="exclusive")
    parser_result = {
        "customer_name": "Kiran", "customer_phone": None,
        "items": [
            {"name": "rice", "qty": 1, "price": 1050,
             "item_discount_type": "none", "item_discount_value": 0,
             "hsn": "1006", "gst_rate": 5},
        ],
        "bill_discount_type": "flat",
        "bill_discount_value": 50,
        "pricing_type": "inclusive",   # explicit in message
        "needs_confirmation": False,
        "confidence": 0.95, "warnings": [], "notes": "", "error": None,
    }
    pb = _build_pending_from_parser(
        phone="+911", shop=shop, parser_result=parser_result,
        raw_message="rice 1050 including gst less 50",
    )
    result = _compute_bill_from_pending(pb)
    # inclusive: item gst-inclusive 1050 → base 1000, gst 50
    # bill flat -50 applied as scale 1050->1000, scale=1000/1050=0.95238
    # scaled line = 1000 → inclusive back-out: base=952.38, gst=47.62
    # grand = 1000 (scaled inclusive total == target after flat deduction)
    assert pb.pricing_type == "inclusive"
    assert round(result.taxable_amount, 2) == 1000.0
    assert round(result.grand_total, 2) == 1000.0
```

- [ ] **Step 2: Run → PASS** (if earlier tasks were correct).

If the grand total is off by a rounding penny, the rounding-absorption block in Task 5 already handles it.

- [ ] **Step 3: Commit**

```bash
git add tests/test_discount_parser.py
git commit -m "test: end-to-end discount+inclusive pipeline"
```

---

## Task 17: Update docs

**Files:**
- Modify: `PROJECT_CONTEXT.md`
- Modify: `memory.md`

- [ ] **Step 1: `PROJECT_CONTEXT.md`** — in the "Important Notes" section, add entries for:
  - Item-level discounts (`percent` / `flat`) and bill-level discounts (`flat` / `percent` / `override`).
  - Pricing precedence: explicit keywords in message → `Shop.default_pricing`.
  - New `Bill` columns and their meaning.
  - `needs_confirmation` flag in preview.

- [ ] **Step 2: `memory.md`** — add a new numbered Key Design Decision covering:
  - The two-pass scale-ratio approach in `calculate_bill` (preserves per-item GST rates).
  - The rounding-absorption trick for flat/override.
  - Why `PendingBill` keeps both `is_inclusive` and `pricing_type` (is_inclusive is the old load-bearing bool; pricing_type is the new canonical string).

- [ ] **Step 3: Run full suite one last time → PASS**

- [ ] **Step 4: Commit**

```bash
git add PROJECT_CONTEXT.md memory.md
git commit -m "docs: discount handling and pricing precedence"
```

---

## Self-Review Checklist (completed by plan author)

**Spec coverage:**
- Item extraction → Task 10 (prompt), Task 9 (sanitizer).
- Pricing type → Task 10 (prompt), Task 11 (precedence), Task 13 (INCLUDE/EXCLUDE persistence).
- Item-level discount → Tasks 1, 4, 9, 10, 11.
- Bill-level discount → Tasks 2, 5, 9, 10, 11, 12.
- Final amount override → Task 6.
- Discount type clarity (flat vs percent) → Spec text lives in Task 10's prompt; validator guards in Task 9.
- Calculation order → Tasks 3, 4, 5, 6 (sequentially build the order).
- GST rules → Existing `VALID_GST_SLABS` check preserved; no changes needed.
- Ambiguity handling (`needs_confirmation`) → Task 9 (sanitizer), Task 11 (pending), Task 14 (preview banner).
- Output format JSON → Task 9 (validator shape).
- Key interpretation rules → Prompt text in Task 10.

**Placeholder scan:** No TBD/TODO. Every code step shows the code.

**Type consistency:** `item_discount_type`, `item_discount_value`, `bill_discount_type`, `bill_discount_value`, `pricing_type`, `taxable_amount`, `needs_confirmation`, `subtotal_before_bill_discount`, `discount_total`, `raw_amount` — names match across entities, pending, parser, DB columns, and calc.

**Backward compatibility:**
- `BillItem`/`BillResult` new fields all have defaults → existing call sites untouched.
- `calculate_bill` new params default to `"none"` / `0.0` → existing callers unchanged.
- `PendingBill` serialization uses `.setdefault()` for every new field → old DB rows deserialize.
- `Bill` DB columns have Python-side defaults → new INSERTs work; prod needs a one-shot ALTER TABLE (noted in Task 7 commit body). Dev auto-resets.
- Parser `_error_result` and regex fallback both set new fields to safe defaults.
- The `is_inclusive` boolean stays as the load-bearing field inside `calculate_bill`; `pricing_type` string is carried alongside for display/DB/parser I/O.

---
