"""Tests for discount and pricing features."""

from core.entities import BillItem, BillResult


# ─────────────────────────────────────────────
# Task 1 — BillItem discount fields
# ─────────────────────────────────────────────

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


from core.billing import calculate_bill


# ─────────────────────────────────────────────
# Task 2 — BillResult pricing+discount fields
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# Task 3 — calculate_bill new params, no-op path
# ─────────────────────────────────────────────

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
    # raw_amount populated on processed items
    assert result.items[0].raw_amount == 100.0


def test_calculate_bill_no_discount_inclusive_sets_pricing_type():
    items = [BillItem(name="pen", qty=1, price=118, hsn="9608", gst_rate=18)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        is_inclusive=True,
    )
    assert result.pricing_type == "inclusive"
    assert round(result.grand_total, 2) == 118.0
    assert round(result.items[0].amount, 2) == 100.0


# ─────────────────────────────────────────────
# Task 4 — item-level percent/flat discount
# ─────────────────────────────────────────────

def test_calculate_bill_item_percent_discount():
    items = [BillItem(
        name="tiles", qty=10, price=50, hsn="6907", gst_rate=18,
        item_discount_type="percent", item_discount_value=10,
    )]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
    )
    # raw 500 → -10% = 450 → gst18 = 81 → grand 531
    assert result.items[0].raw_amount == 500.0
    assert result.items[0].amount == 450.0
    assert result.items[0].item_discount_type == "percent"
    assert result.items[0].item_discount_value == 10
    assert round(result.subtotal, 2) == 450.0
    assert round(result.grand_total, 2) == 531.0


def test_calculate_bill_item_flat_discount():
    items = [BillItem(
        name="shirt", qty=1, price=500, hsn="6205", gst_rate=5,
        item_discount_type="flat", item_discount_value=50,
    )]
    result = calculate_bill(items, shop_state_code="36", customer_state_code="36")
    # raw 500 → -50 = 450 → gst5 = 22.5 → grand 472.5
    assert result.items[0].amount == 450.0
    assert round(result.grand_total, 2) == 472.5


def test_calculate_bill_item_flat_discount_clamps_to_raw():
    """A flat discount larger than the line total clamps to the line."""
    items = [BillItem(
        name="cheap", qty=1, price=100, hsn="9999", gst_rate=18,
        item_discount_type="flat", item_discount_value=500,
    )]
    result = calculate_bill(items, shop_state_code="36", customer_state_code="36")
    assert result.items[0].amount == 0.0
    assert round(result.grand_total, 2) == 0.0


def test_calculate_bill_item_discount_inclusive():
    """Item-level percent discount in inclusive mode."""
    items = [BillItem(
        name="rice", qty=1, price=1050, hsn="1006", gst_rate=5,
        item_discount_type="percent", item_discount_value=10,
    )]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        is_inclusive=True,
    )
    # raw 1050 → -10% = 945 (this is GST-inclusive) → base 900, gst 45, grand 945
    assert round(result.items[0].amount, 2) == 900.0
    assert round(result.items[0].total, 2) == 945.0
    assert round(result.grand_total, 2) == 945.0


# ─────────────────────────────────────────────
# Task 5 — bill-level flat/percent discount
# ─────────────────────────────────────────────

def test_calculate_bill_bill_flat_discount_single_rate():
    items = [BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="flat", bill_discount_value=100,
    )
    # subtotal 1000 → taxable 900 → gst 45 → grand 945
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
    # Each item scaled to 900 → taxable 1800
    # GST: 900*5% + 900*18% = 45 + 162 = 207
    # grand = 1800 + 207 = 2007
    assert result.subtotal_before_bill_discount == 2000.0
    assert result.taxable_amount == 1800.0
    assert round(result.discount_total, 2) == 200.0
    assert round(result.grand_total, 2) == 2007.0


def test_calculate_bill_bill_discount_stacked_with_item_discount():
    """Item discount applies first, then bill discount on the post-item subtotal."""
    items = [
        BillItem(
            name="tiles", qty=10, price=50, hsn="6907", gst_rate=18,
            item_discount_type="percent", item_discount_value=10,
        ),
        BillItem(name="grout", qty=1, price=200, hsn="3214", gst_rate=18),
    ]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="flat", bill_discount_value=50,
    )
    # tiles: 500 - 10% = 450
    # grout: 200
    # subtotal_before_bill_discount = 650
    # bill flat 50 → scale = 600/650
    # tiles scaled = 450 * 600/650 ≈ 415.38; grout scaled = 200 * 600/650 ≈ 184.62
    # sum ≈ 600 (taxable); gst18 = 108; grand = 708
    assert result.subtotal_before_bill_discount == 650.0
    assert round(result.taxable_amount, 2) == 600.0
    assert round(result.discount_total, 2) == 50.0
    assert round(result.grand_total, 2) == 708.0


def test_calculate_bill_bill_flat_discount_inclusive():
    """Inclusive mode with bill-level flat discount."""
    items = [BillItem(name="rice", qty=1, price=1050, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        is_inclusive=True,
        bill_discount_type="flat", bill_discount_value=50,
    )
    # pre_bill_subtotal (inclusive) = 1050
    # scale = 1000/1050 → scaled_line = 1000 (GST-inclusive lump)
    # base = 1000/1.05 ≈ 952.38; gst ≈ 47.62; grand = 1000
    assert result.subtotal_before_bill_discount == 1050.0
    assert round(result.taxable_amount, 2) == 1000.0
    assert round(result.discount_total, 2) == 50.0
    assert round(result.grand_total, 2) == 1000.0


def test_calculate_bill_bill_flat_discount_clamps_to_subtotal():
    """Flat discount > subtotal clamps; taxable becomes 0."""
    items = [BillItem(name="rice", qty=1, price=100, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="flat", bill_discount_value=500,
    )
    assert result.subtotal_before_bill_discount == 100.0
    assert result.taxable_amount == 0.0
    assert result.discount_total == 100.0
    assert result.grand_total == 0.0


def test_calculate_bill_bill_percent_discount_clamps_to_100():
    """Percent > 100 clamps to 100 (treated as full discount)."""
    items = [BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="percent", bill_discount_value=150,
    )
    assert result.taxable_amount == 0.0
    assert result.grand_total == 0.0
