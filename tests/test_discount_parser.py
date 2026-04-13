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
