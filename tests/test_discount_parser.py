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


def test_calculate_bill_override_target_above_natural_is_noop():
    """If target > natural, do not silently inflate — cap at natural."""
    items = [BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="override", bill_discount_value=9999,
    )
    # Natural grand = 1050. Target 9999 is bogus — keep natural.
    assert result.grand_total == 1050.0
    assert result.discount_total == 0.0
    _assert_bill_invariants(result, pre_subtotal_expected=1000.0)


def test_calculate_bill_override_zero_target_clamps_to_zero():
    """Override to 0 makes the whole bill zero."""
    items = [BillItem(name="rice", qty=1, price=1000, hsn="1006", gst_rate=5)]
    result = calculate_bill(
        items, shop_state_code="36", customer_state_code="36",
        bill_discount_type="override", bill_discount_value=0,
    )
    assert result.grand_total == 0.0
    assert result.taxable_amount == 0.0
    _assert_bill_invariants(result, pre_subtotal_expected=1000.0)


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
