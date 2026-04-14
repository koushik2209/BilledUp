"""Tests for discount and pricing features."""

import pytest

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


# ─────────────────────────────────────────────
# SAFEGUARDS — invariants that must hold for every discount path
# ─────────────────────────────────────────────

def _assert_bill_invariants(result, *, pre_subtotal_expected=None):
    """Conservation + GST split checks. Run on every BillResult."""
    # 1. grand_total = subtotal + total_gst (rupees, not paise)
    assert result.grand_total == round(result.subtotal + result.total_gst, 2), (
        f"grand_total drift: {result.grand_total} vs {result.subtotal}+{result.total_gst}"
    )

    # 2. Total GST = CGST + SGST + IGST
    assert result.total_gst == round(
        result.total_cgst + result.total_sgst + result.total_igst, 2
    )

    # 3. Intra-state → no IGST; inter-state → no CGST/SGST.
    if result.is_igst:
        assert result.total_cgst == 0.0 and result.total_sgst == 0.0
    else:
        assert result.total_igst == 0.0

    # 4. Per-item splits sum to the totals (within 1 paisa per item for rounding).
    s_cgst = round(sum(i.cgst for i in result.items), 2)
    s_sgst = round(sum(i.sgst for i in result.items), 2)
    s_igst = round(sum(i.igst for i in result.items), 2)
    tol = 0.01 * max(len(result.items), 1)
    assert abs(s_cgst - result.total_cgst) <= tol
    assert abs(s_sgst - result.total_sgst) <= tol
    assert abs(s_igst - result.total_igst) <= tol

    # 5. sum(item.total) == grand_total (within 1 paisa per item).
    s_tot = round(sum(i.total for i in result.items), 2)
    assert abs(s_tot - result.grand_total) <= tol

    # 6. No negative totals from clamping paths.
    assert result.subtotal >= 0
    assert result.total_gst >= 0
    assert result.grand_total >= 0
    assert result.taxable_amount >= 0
    assert result.discount_total >= 0

    # 7. Discount bookkeeping: pre - discount == scaled-line sum.
    #    Exclusive → scaled-line sum == subtotal.
    #    Inclusive → scaled-line sum == taxable_amount (lump, GST still inside).
    if result.pricing_type == "inclusive":
        scaled_sum = result.taxable_amount
    else:
        scaled_sum = result.subtotal
    assert abs(
        (result.subtotal_before_bill_discount - result.discount_total) - scaled_sum
    ) <= 0.05, (
        f"discount bookkeeping drift: "
        f"{result.subtotal_before_bill_discount} - {result.discount_total} "
        f"!= {scaled_sum}"
    )

    # 8. Inclusive → grand_total == taxable_amount (GST already inside).
    #    Exclusive → grand_total == taxable_amount + total_gst.
    if result.pricing_type == "inclusive":
        assert abs(result.grand_total - result.taxable_amount) <= 0.05
    else:
        assert abs(
            result.grand_total - (result.taxable_amount + result.total_gst)
        ) <= 0.05

    # 9. Optional: caller supplies the pre-bill subtotal they expect.
    if pre_subtotal_expected is not None:
        assert result.subtotal_before_bill_discount == pre_subtotal_expected


def test_safeguard_conservation_across_all_discount_paths():
    """Conservation invariant holds across every discount combination."""
    scenarios = [
        # (label, items_kwargs, call_kwargs)
        ("no discount exclusive",
         [{"name": "pen", "qty": 2, "price": 50, "hsn": "9608", "gst_rate": 18}],
         {}),
        ("no discount inclusive",
         [{"name": "pen", "qty": 1, "price": 118, "hsn": "9608", "gst_rate": 18}],
         {"is_inclusive": True}),
        ("item percent",
         [{"name": "tiles", "qty": 10, "price": 50, "hsn": "6907", "gst_rate": 18,
           "item_discount_type": "percent", "item_discount_value": 10}],
         {}),
        ("item flat",
         [{"name": "shirt", "qty": 1, "price": 500, "hsn": "6205", "gst_rate": 5,
           "item_discount_type": "flat", "item_discount_value": 50}],
         {}),
        ("bill flat single rate",
         [{"name": "rice", "qty": 1, "price": 1000, "hsn": "1006", "gst_rate": 5}],
         {"bill_discount_type": "flat", "bill_discount_value": 100}),
        ("bill percent mixed rates",
         [{"name": "rice", "qty": 1, "price": 1000, "hsn": "1006", "gst_rate": 5},
          {"name": "soap", "qty": 1, "price": 1000, "hsn": "3401", "gst_rate": 18}],
         {"bill_discount_type": "percent", "bill_discount_value": 10}),
        ("stacked item + bill",
         [{"name": "tiles", "qty": 10, "price": 50, "hsn": "6907", "gst_rate": 18,
           "item_discount_type": "percent", "item_discount_value": 10},
          {"name": "grout", "qty": 1, "price": 200, "hsn": "3214", "gst_rate": 18}],
         {"bill_discount_type": "flat", "bill_discount_value": 50}),
        ("inclusive + bill flat",
         [{"name": "rice", "qty": 1, "price": 1050, "hsn": "1006", "gst_rate": 5}],
         {"is_inclusive": True, "bill_discount_type": "flat",
          "bill_discount_value": 50}),
        ("inclusive + item percent",
         [{"name": "rice", "qty": 1, "price": 1050, "hsn": "1006", "gst_rate": 5,
           "item_discount_type": "percent", "item_discount_value": 10}],
         {"is_inclusive": True}),
        ("inter-state percent",
         [{"name": "rice", "qty": 1, "price": 1000, "hsn": "1006", "gst_rate": 5}],
         {"customer_state_code": "29", "bill_discount_type": "percent",
          "bill_discount_value": 10}),
    ]
    for label, items_kw, call_kw in scenarios:
        items = [BillItem(**kw) for kw in items_kw]
        call_kw.setdefault("shop_state_code", "36")
        call_kw.setdefault("customer_state_code", "36")
        result = calculate_bill(items, **call_kw)
        try:
            _assert_bill_invariants(result)
        except AssertionError as e:
            raise AssertionError(f"[{label}] {e}") from e


def test_safeguard_gst_split_per_item_consistency():
    """Each item's cgst+sgst+igst matches the tax-type rule exactly."""
    items = [
        BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5),
        BillItem(name="soap", qty=1, price=500, hsn="3401", gst_rate=18),
        BillItem(name="tiles", qty=10, price=50, hsn="6907", gst_rate=18,
                 item_discount_type="percent", item_discount_value=10),
    ]
    # Intra
    r_intra = calculate_bill(items, shop_state_code="36", customer_state_code="36",
                             bill_discount_type="flat", bill_discount_value=75)
    for it in r_intra.items:
        assert it.igst == 0.0
        # CGST ≈ SGST (halved; off by ≤ 1 paisa from rounding)
        assert abs(it.cgst - it.sgst) <= 0.01
        # cgst + sgst == gst on (qty*price) post-all-discounts (approx)
        expected = round(it.amount * it.gst_rate / 100, 2)
        assert abs((it.cgst + it.sgst) - expected) <= 0.02
    _assert_bill_invariants(r_intra)

    # Inter
    r_inter = calculate_bill(items, shop_state_code="36", customer_state_code="29",
                             bill_discount_type="flat", bill_discount_value=75)
    for it in r_inter.items:
        assert it.cgst == 0.0
        assert it.sgst == 0.0
        expected = round(it.amount * it.gst_rate / 100, 2)
        assert abs(it.igst - expected) <= 0.02
    _assert_bill_invariants(r_inter)

    # Intra and inter have identical grand totals on same items.
    assert r_intra.grand_total == r_inter.grand_total
    assert r_intra.total_gst == r_inter.total_gst


def test_safeguard_over_discount_flat_clamps_and_invariants_hold():
    """Flat discount > subtotal clamps to subtotal; invariants still hold."""
    items = [BillItem(name="rice", qty=1, price=100, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="flat", bill_discount_value=500,
    )
    assert result.subtotal_before_bill_discount == 100.0
    assert result.discount_total == 100.0      # clamped to pre-subtotal
    assert result.taxable_amount == 0.0
    assert result.grand_total == 0.0
    _assert_bill_invariants(result, pre_subtotal_expected=100.0)


def test_safeguard_over_discount_percent_clamps_and_invariants_hold():
    """Percent > 100 clamps to 100; invariants still hold."""
    items = [BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="percent", bill_discount_value=250,
    )
    assert result.taxable_amount == 0.0
    assert result.grand_total == 0.0
    _assert_bill_invariants(result, pre_subtotal_expected=1000.0)


# ─────────────────────────────────────────────
# Task 6 — final amount override
# ─────────────────────────────────────────────

def test_calculate_bill_override_exclusive_mixed_rates():
    """Override: scale scaled-line-total so grand_total == target exactly."""
    items = [
        BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5),
        BillItem(name="soap", qty=1, price=1000, hsn="3401", gst_rate=18),
    ]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="override", bill_discount_value=2000,
    )
    # Natural grand total = 1050 + 1180 = 2230. Target = 2000.
    assert round(result.grand_total, 2) == 2000.0
    assert result.bill_discount_type == "override"
    assert result.bill_discount_value == 2000.0
    assert result.subtotal_before_bill_discount == 2000.0  # pre-override
    # discount_total is the rupees deducted from the pre-bill subtotal
    # (scaled-line reduction), not the (natural - target) headline.
    assert result.discount_total > 0
    _assert_bill_invariants(result, pre_subtotal_expected=2000.0)


def test_calculate_bill_override_inclusive():
    """Override in inclusive mode: scaled lump sums to the target."""
    items = [BillItem(name="rice", qty=1, price=1050, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        is_inclusive=True,
        bill_discount_type="override", bill_discount_value=945,
    )
    assert round(result.grand_total, 2) == 945.0
    assert result.taxable_amount == 945.0  # inclusive: lump == grand
    _assert_bill_invariants(result, pre_subtotal_expected=1050.0)


def test_calculate_bill_override_single_item():
    """Override on a single item still hits the target exactly."""
    items = [BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="override", bill_discount_value=900,
    )
    # Natural grand = 1050. Target = 900.
    assert round(result.grand_total, 2) == 900.0
    _assert_bill_invariants(result, pre_subtotal_expected=1000.0)


def test_calculate_bill_override_target_above_natural_flags_confirmation():
    """Target > natural: do NOT silently inflate. Keep natural totals
    AND set needs_confirmation so the shopkeeper can re-check."""
    items = [BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="override", bill_discount_value=9999,
    )
    # Natural grand = 1050. Target 9999 is bogus — keep natural.
    assert result.grand_total == 1050.0
    assert result.discount_total == 0.0
    assert result.needs_confirmation is True
    _assert_bill_invariants(result, pre_subtotal_expected=1000.0)


def test_calculate_bill_override_zero_target_flags_confirmation():
    """Override to 0 is allowed AND flagged for confirmation —
    a free bill is unusual enough to deserve a second look."""
    items = [BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="override", bill_discount_value=0,
    )
    assert result.grand_total == 0.0
    assert result.taxable_amount == 0.0
    assert result.needs_confirmation is True
    _assert_bill_invariants(result, pre_subtotal_expected=1000.0)


def test_calculate_bill_override_normal_no_confirmation_flag():
    """A normal in-range override is NOT flagged for confirmation."""
    items = [BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="override", bill_discount_value=900,
    )
    assert result.grand_total == 900.0
    assert result.needs_confirmation is False


def test_calculate_bill_override_inter_state():
    """Override under IGST path still hits the exact target."""
    items = [
        BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5),
        BillItem(name="soap", qty=1, price=1000, hsn="3401", gst_rate=18),
    ]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="29",
        bill_discount_type="override", bill_discount_value=2000,
    )
    assert result.is_igst is True
    assert round(result.grand_total, 2) == 2000.0
    assert result.total_cgst == 0.0
    assert result.total_sgst == 0.0
    _assert_bill_invariants(result, pre_subtotal_expected=2000.0)


def test_calculate_bill_override_preserves_rate_ratio():
    """Items at different GST rates remain in the same value ratio after override."""
    items = [
        BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5),
        BillItem(name="soap", qty=1, price=1000, hsn="3401", gst_rate=18),
    ]
    natural = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
    )
    override = calculate_bill(
        [BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5),
         BillItem(name="soap", qty=1, price=1000, hsn="3401", gst_rate=18)],
        shop_state_code="36", customer_state_code="36",
        bill_discount_type="override", bill_discount_value=2000,
    )
    # Natural: rice total 1050, soap total 1180, grand 2230.
    # Override scales both lines by 2000/2230 uniformly. So the ratio
    # of item.total between the two items should be preserved.
    ratio_natural = natural.items[0].total / natural.items[1].total
    ratio_override = override.items[0].total / override.items[1].total
    assert abs(ratio_natural - ratio_override) <= 0.001


def test_bill_model_has_discount_columns():
    """Task 7: Bill ORM model exposes discount & pricing columns."""
    from db.models import Bill
    cols = {c.name for c in Bill.__table__.columns}
    assert "bill_discount_type"      in cols
    assert "bill_discount_value"     in cols
    assert "subtotal_before_discount" in cols
    assert "taxable_amount"          in cols
    assert "pricing_type"            in cols


def test_required_schema_lists_new_bill_columns():
    """Task 7: _REQUIRED_SCHEMA references the new Bill columns
    so startup validation catches drift."""
    from db.session import _REQUIRED_SCHEMA
    required = set(_REQUIRED_SCHEMA.get("bills", []))
    for col in (
        "bill_discount_type", "bill_discount_value",
        "subtotal_before_discount", "taxable_amount", "pricing_type",
    ):
        assert col in required, f"missing {col} in _REQUIRED_SCHEMA['bills']"


def test_pending_bill_roundtrips_discount_fields():
    """Task 8: PendingBill carries pricing_type + bill-level discount +
    needs_confirmation, and serialize/deserialize preserves them."""
    from datetime import datetime as _dt
    from services.pending import (
        PendingBill, _serialize_pending, _deserialize_pending,
    )
    p = PendingBill(
        phone="+91999", shop_id="s1", shop_name="S", shop_state="TG",
        shop_state_code="36", customer_name="C", customer_state="TG",
        customer_state_code="36", items=[{"name": "rice", "qty": 1, "price": 100}],
        confidence=0.9, warnings=[], raw_message="bill rice 100 less 10",
        created_at=_dt(2026, 4, 13, 12, 0, 0),
        pricing_type="inclusive",
        bill_discount_type="flat",
        bill_discount_value=10.0,
        needs_confirmation=True,
    )
    assert p.pricing_type == "inclusive"
    assert p.bill_discount_type == "flat"
    assert p.bill_discount_value == 10.0
    assert p.needs_confirmation is True
    restored = _deserialize_pending(_serialize_pending(p))
    assert restored.pricing_type == "inclusive"
    assert restored.bill_discount_type == "flat"
    assert restored.bill_discount_value == 10.0
    assert restored.needs_confirmation is True


def test_pending_bill_backwards_compat_old_json():
    """Old pending_bills rows without new fields still deserialize."""
    import json as _json
    from services.pending import _deserialize_pending
    old = _json.dumps({
        "phone": "+91999", "shop_id": "s1", "shop_name": "S",
        "shop_state": "TG", "shop_state_code": "36",
        "customer_name": "C", "customer_state": "TG",
        "customer_state_code": "36",
        "items": [], "confidence": 1.0, "warnings": [],
        "raw_message": "x", "created_at": "2026-04-13T12:00:00",
    })
    restored = _deserialize_pending(old)
    assert restored.pricing_type == "exclusive"
    assert restored.bill_discount_type == "none"
    assert restored.bill_discount_value == 0.0
    assert restored.needs_confirmation is False


def test_sanitizer_normalizes_pricing_and_discount_fields():
    """Task 9: validate_parsed_response accepts new schema fields
    and coerces them to safe defaults."""
    from ai.sanitizer import validate_parsed_response
    raw = {
        "customer_name": "Ravi",
        "pricing_type": "INCLUSIVE",
        "bill_discount_type": "flat",
        "bill_discount_value": "50",
        "items": [
            {"name": "rice", "qty": 1, "price": 100,
             "item_discount_type": "percent", "item_discount_value": "10"},
            {"name": "soap", "qty": 2, "price": 50,
             "item_discount_type": "flat", "item_discount_value": 5},
        ],
    }
    result, issues = validate_parsed_response(raw)
    assert result["pricing_type"] == "inclusive"
    assert result["bill_discount_type"] == "flat"
    assert result["bill_discount_value"] == 50.0
    assert result["items"][0]["item_discount_type"] == "percent"
    assert result["items"][0]["item_discount_value"] == 10.0
    assert result["items"][1]["item_discount_type"] == "flat"
    assert result["items"][1]["item_discount_value"] == 5.0


def test_sanitizer_rejects_bad_discount_type_and_negative_values():
    """Bad types coerce to 'none'; negative values coerce to 0."""
    from ai.sanitizer import validate_parsed_response
    raw = {
        "customer_name": "Ravi",
        "pricing_type": "bogus",
        "bill_discount_type": "weird",
        "bill_discount_value": -25,
        "items": [
            {"name": "rice", "qty": 1, "price": 100,
             "item_discount_type": "junk", "item_discount_value": -5},
        ],
    }
    result, _ = validate_parsed_response(raw)
    assert result["pricing_type"] == "exclusive"       # default
    assert result["bill_discount_type"] == "none"
    assert result["bill_discount_value"] == 0.0
    assert result["items"][0]["item_discount_type"] == "none"
    assert result["items"][0]["item_discount_value"] == 0.0


def test_sanitizer_defaults_missing_fields():
    """Old-format parser output (no new fields) still validates fine."""
    from ai.sanitizer import validate_parsed_response
    raw = {
        "customer_name": "Ravi",
        "items": [{"name": "rice", "qty": 1, "price": 100}],
    }
    result, _ = validate_parsed_response(raw)
    assert result["pricing_type"] == "exclusive"
    assert result["bill_discount_type"] == "none"
    assert result["bill_discount_value"] == 0.0
    assert result["items"][0]["item_discount_type"] == "none"
    assert result["items"][0]["item_discount_value"] == 0.0


def test_parse_message_passes_through_discount_schema(monkeypatch):
    """Task 10: parse_message round-trips the new parser schema fields
    through the sanitizer when Claude returns them."""
    import json as _json
    from ai import parser as P

    class _FakeBlock:
        def __init__(self, text): self.text = text
    class _FakeResp:
        def __init__(self, text): self.content = [_FakeBlock(text)]

    canned = {
        "customer_name": "Kiran",
        "items": [
            {"name": "tiles", "qty": 10, "price": 50,
             "item_discount_type": "percent", "item_discount_value": 10},
        ],
        "bill_discount_type": "flat", "bill_discount_value": 100,
        "pricing_type": "exclusive", "needs_confirmation": False,
        "confidence": 0.95, "notes": "", "error": None,
    }

    class _FakeMessages:
        def create(self, **kw):
            return _FakeResp(_json.dumps(canned))
    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(P, "get_anthropic_client", lambda: _FakeClient())
    monkeypatch.setattr(P, "TextBlock", _FakeBlock)

    out = P.parse_message("tiles 10 at 50 each 10% discount, less 100")
    assert out["bill_discount_type"] == "flat"
    assert out["bill_discount_value"] == 100.0
    assert out["pricing_type"] == "exclusive"
    assert out["items"][0]["item_discount_type"] == "percent"
    assert out["items"][0]["item_discount_value"] == 10.0


def test_parse_message_error_result_has_discount_defaults():
    """Error path still returns the new fields with safe defaults."""
    from ai import parser as P
    out = P._error_result("test err")
    assert out["bill_discount_type"] == "none"
    assert out["bill_discount_value"] == 0.0
    assert out["pricing_type"] is None
    assert out["needs_confirmation"] is False


@pytest.mark.parametrize("scenario,canned,expected_total", [
    # 1) Clean item + bill flat discount
    (
        "tiles 10 at 50 each, less 100",
        {
            "customer_name": "Kiran",
            "items": [{"name": "tiles", "qty": 10, "price": 50,
                       "item_discount_type": "none", "item_discount_value": 0}],
            "bill_discount_type": "flat", "bill_discount_value": 100,
            "pricing_type": "exclusive", "needs_confirmation": False,
            "confidence": 0.95, "notes": "", "error": None,
        },
        # subtotal 500 − 100 = 400 × 1.18 = 472.00
        472.00,
    ),
    # 2) Item-level percent + bill percent mixed
    (
        "shirt 500 10% off, total discount 5%",
        {
            "customer_name": "Customer",
            "items": [{"name": "shirt", "qty": 1, "price": 500,
                       "item_discount_type": "percent", "item_discount_value": 10}],
            "bill_discount_type": "percent", "bill_discount_value": 5,
            "pricing_type": "exclusive", "needs_confirmation": False,
            "confidence": 0.9, "notes": "", "error": None,
        },
        # 500 − 50 = 450, 5% off = 427.5, + 5% GST (clothing) = 448.88
        448.88,
    ),
    # 3) Override: "make it 9000"
    (
        "rice 5 bags at 1000 each make it 5000",
        {
            "customer_name": "Ramesh",
            "items": [{"name": "rice", "qty": 5, "price": 1000,
                       "item_discount_type": "none", "item_discount_value": 0}],
            "bill_discount_type": "override", "bill_discount_value": 5000,
            "pricing_type": "exclusive", "needs_confirmation": False,
            "confidence": 0.9, "notes": "", "error": None,
        },
        5000.00,
    ),
    # 4) Inclusive pricing with flat bill discount
    (
        "soap 200 including gst less 20",
        {
            "customer_name": "Customer",
            "items": [{"name": "soap", "qty": 1, "price": 200,
                       "item_discount_type": "none", "item_discount_value": 0}],
            "bill_discount_type": "flat", "bill_discount_value": 20,
            "pricing_type": "inclusive", "needs_confirmation": False,
            "confidence": 0.85, "notes": "", "error": None,
        },
        180.00,   # inclusive: final = taxable_after_discount
    ),
    # 5) "50 make 45" → new unit price, no item discount
    (
        "tiles 10 at 50 make 45",
        {
            "customer_name": "Customer",
            "items": [{"name": "tiles", "qty": 10, "price": 45,
                       "item_discount_type": "none", "item_discount_value": 0}],
            "bill_discount_type": "none", "bill_discount_value": 0,
            "pricing_type": "exclusive", "needs_confirmation": False,
            "confidence": 0.9, "notes": "", "error": None,
        },
        531.00,  # 450 × 1.18
    ),
])
def test_parser_end_to_end_real_world_messy(monkeypatch, scenario, canned, expected_total):
    """Task 10 safeguard: realistic messages produce bills whose totals match
    hand-computed expectations AND satisfy all bill invariants."""
    import json as _json
    from ai import parser as P
    from core.entities import BillItem
    from core.billing import calculate_bill

    class _FakeBlock:
        def __init__(self, text): self.text = text
    class _FakeResp:
        def __init__(self, text): self.content = [_FakeBlock(text)]
    class _FakeMessages:
        def create(self, **kw):
            return _FakeResp(_json.dumps(canned))
    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(P, "get_anthropic_client", lambda: _FakeClient())
    monkeypatch.setattr(P, "TextBlock", _FakeBlock)

    parsed = P.parse_message(scenario)
    assert parsed.get("error") is None, f"parser error: {parsed.get('error')}"

    items = [
        BillItem(
            name=it["name"], qty=it["qty"], price=it["price"],
            item_discount_type=it.get("item_discount_type", "none"),
            item_discount_value=it.get("item_discount_value", 0.0),
        )
        for it in parsed["items"]
    ]
    result = calculate_bill(
        items,
        shop_state_code="36", customer_state_code="36",
        is_inclusive=(parsed["pricing_type"] == "inclusive"),
        bill_discount_type=parsed["bill_discount_type"],
        bill_discount_value=parsed["bill_discount_value"],
    )
    assert abs(result.grand_total - expected_total) <= 0.50, (
        f"{scenario!r}: grand_total={result.grand_total} expected≈{expected_total}"
    )
    _assert_bill_invariants(result)


def test_build_pending_explicit_pricing_beats_shop_default():
    """Task 11: explicit parser pricing_type wins over shop default."""
    from services.billing import _build_pending_from_parser
    from db.models import Shop
    shop = Shop(shop_id="RAVI", name="X", address="Y", gstin="", phone="",
                state="Telangana", state_code="36", default_pricing="exclusive")
    parser_result = {
        "customer_name": "A", "customer_phone": None,
        "items": [{"name": "rice", "qty": 1, "price": 100,
                   "item_discount_type": "none", "item_discount_value": 0}],
        "bill_discount_type": "none", "bill_discount_value": 0.0,
        "pricing_type": "inclusive", "needs_confirmation": False,
        "confidence": 0.9, "warnings": [], "notes": "", "error": None,
    }
    pb = _build_pending_from_parser(
        phone="+911", shop=shop, parser_result=parser_result,
        raw_message="rice 100 including gst",
    )
    assert pb.pricing_type == "inclusive"
    assert pb.is_inclusive is True


def test_build_pending_falls_back_to_shop_default():
    """Task 11: when parser is silent, shop default wins."""
    from services.billing import _build_pending_from_parser
    from db.models import Shop
    shop = Shop(shop_id="RAVI", name="X", address="Y", gstin="", phone="",
                state="Telangana", state_code="36", default_pricing="inclusive")
    parser_result = {
        "customer_name": "A", "customer_phone": None,
        "items": [{"name": "rice", "qty": 1, "price": 100,
                   "item_discount_type": "none", "item_discount_value": 0}],
        "bill_discount_type": "none", "bill_discount_value": 0.0,
        "pricing_type": None, "needs_confirmation": False,
        "confidence": 0.9, "warnings": [], "notes": "", "error": None,
    }
    pb = _build_pending_from_parser(
        phone="+911", shop=shop, parser_result=parser_result,
        raw_message="rice 100",
    )
    assert pb.pricing_type == "inclusive"
    assert pb.is_inclusive is True


def test_build_pending_wires_discount_fields():
    """Task 11: discount + needs_confirmation flow through the helper."""
    from services.billing import _build_pending_from_parser
    from db.models import Shop
    shop = Shop(shop_id="RAVI", name="X", address="Y", gstin="", phone="",
                state="TG", state_code="36", default_pricing="exclusive")
    parser_result = {
        "customer_name": "A", "customer_phone": None,
        "items": [{"name": "rice", "qty": 1, "price": 100,
                   "item_discount_type": "percent", "item_discount_value": 10}],
        "bill_discount_type": "flat", "bill_discount_value": 20.0,
        "pricing_type": "exclusive", "needs_confirmation": True,
        "confidence": 0.9, "warnings": [], "notes": "", "error": None,
    }
    pb = _build_pending_from_parser(
        phone="+911", shop=shop, parser_result=parser_result,
        raw_message="rice 100 10% off, less 20",
    )
    assert pb.bill_discount_type == "flat"
    assert pb.bill_discount_value == 20.0
    assert pb.needs_confirmation is True
    assert pb.items[0]["item_discount_type"] == "percent"
    assert pb.items[0]["item_discount_value"] == 10.0


def test_compute_bill_from_pending_applies_discounts():
    """Task 12: confirming a pending bill runs item + bill discounts
    through calculate_bill and returns a correct BillResult."""
    from datetime import datetime as _dt
    from services.pending import PendingBill
    from services.billing import _compute_bill_from_pending

    pb = PendingBill(
        phone="+911", shop_id="RAVI", shop_name="X",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Kiran", customer_phone="",
        customer_state="Telangana", customer_state_code="36",
        items=[{
            "name": "tiles", "qty": 10, "price": 50,
            "hsn": "6907", "gst_rate": 18,
            "item_discount_type": "percent", "item_discount_value": 10,
        }],
        confidence=0.95, warnings=[], raw_message="",
        created_at=_dt.utcnow(),
        pricing_type="exclusive",
        bill_discount_type="flat", bill_discount_value=50,
    )
    result = _compute_bill_from_pending(pb)
    # raw 500 → item -10% → 450 → bill flat -50 → taxable 400
    # gst 18% → 72 → grand 472
    assert round(result.taxable_amount, 2) == 400.0
    assert round(result.grand_total, 2) == 472.0
    assert result.pricing_type == "exclusive"
    assert result.bill_discount_type == "flat"


def test_toggle_pricing_mode_persists_shop_default():
    """Task 13: INCLUDE/EXCLUDE writes Shop.default_pricing immediately."""
    from db.session import db_session
    from db.models import Shop
    from services.billing import _toggle_pricing_mode

    with db_session() as s:
        s.query(Shop).filter_by(shop_id="TST13").delete()
        s.add(Shop(
            shop_id="TST13", name="T", address="A", gstin="", phone="9",
            state="Telangana", state_code="36", default_pricing="exclusive",
        ))

    _toggle_pricing_mode(shop_id="TST13", mode="inclusive")

    with db_session() as s:
        shop = s.query(Shop).filter_by(shop_id="TST13").first()
        try:
            assert shop.default_pricing == "inclusive"
        finally:
            s.delete(shop)


def _make_pending_for_preview(**overrides):
    from datetime import datetime as _dt
    from services.pending import PendingBill
    defaults = dict(
        phone="+911", shop_id="RAVI", shop_name="S",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Kiran", customer_phone="",
        customer_state="Telangana", customer_state_code="36",
        items=[{
            "name": "tiles", "qty": 10, "price": 50,
            "hsn": "6907", "gst_rate": 18, "gst_confidence": "high",
            "item_discount_type": "percent", "item_discount_value": 10,
        }],
        confidence=0.95, warnings=[], raw_message="", created_at=_dt.utcnow(),
        pricing_type="exclusive",
        bill_discount_type="flat", bill_discount_value=50,
    )
    defaults.update(overrides)
    return PendingBill(**defaults)


def test_preview_shows_discount_breakdown():
    """Task 14: preview text surfaces subtotal, discount, and taxable
    base when bill-level discounts are present."""
    from services.billing import msg_preview
    pb = _make_pending_for_preview()
    text = msg_preview(pb)
    # Subtotal 500 − 10% item = 450 (raw subtotal_before_bill_discount)
    # Flat 50 off → taxable 400
    assert "Discount" in text or "discount" in text
    assert "400" in text  # taxable/post-discount figure
    assert "472" in text  # grand total


def test_preview_shows_needs_confirmation_banner():
    """Task 14: needs_confirmation pending bills get a visible banner."""
    from services.billing import msg_preview
    pb = _make_pending_for_preview(needs_confirmation=True)
    text = msg_preview(pb)
    assert "confirm" in text.lower()
    # Banner should be something distinct — look for a warning glyph near the top
    assert "⚠" in text or "❗" in text


def test_preview_without_discount_unchanged():
    """Regression: no bill discount → no discount row."""
    from services.billing import msg_preview
    pb = _make_pending_for_preview(
        bill_discount_type="none", bill_discount_value=0.0,
        items=[{
            "name": "tiles", "qty": 10, "price": 50,
            "hsn": "6907", "gst_rate": 18, "gst_confidence": "high",
            "item_discount_type": "none", "item_discount_value": 0,
        }],
    )
    text = msg_preview(pb)
    # grand 590 (500 + 18% gst)
    assert "590" in text


def test_safeguard_negative_percent_is_noop():
    """Negative percent is rejected (treated as no discount)."""
    items = [BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="percent", bill_discount_value=-10,
    )
    # No-op: grand_total is the natural 1050
    assert result.discount_total == 0.0
    assert result.taxable_amount == 1000.0
    assert result.grand_total == 1050.0
    _assert_bill_invariants(result, pre_subtotal_expected=1000.0)
