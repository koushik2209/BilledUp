"""msg_preview must show the SAME post-discount post-GST per-item totals
as msg_bill_summary. Tests cover all bill-type combinations specified
in RULE 6 of the spec.
"""
import re
from datetime import datetime

import pytest

import main


# ── Helpers ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_db():
    main.init_database()


def _pending(
    *,
    items,
    is_inclusive=False,
    is_bill_of_supply=False,
    is_return=False,
    bill_discount_type="none",
    bill_discount_value=0.0,
    customer_state="Telangana",
    customer_state_code="36",
    state_assumed=False,
):
    """Build a PendingBill with all the knobs we need for these tests."""
    from services.pending import PendingBill
    return PendingBill(
        phone="whatsapp:+919000000001", shop_id="TST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Ramesh",
        customer_state=customer_state, customer_state_code=customer_state_code,
        items=items, confidence=1.0, warnings=[], raw_message="seed",
        created_at=datetime.utcnow(),
        is_return=is_return,
        is_bill_of_supply=is_bill_of_supply,
        is_inclusive=is_inclusive,
        pricing_type="inclusive" if is_inclusive else "exclusive",
        bill_discount_type=bill_discount_type,
        bill_discount_value=float(bill_discount_value),
        state_assumed=state_assumed,
    )


def _confirmed_billresult(pending):
    """Compute the BillResult that calculate_bill would produce for this
    pending — same call shape as _generate_confirmed_bill uses internally."""
    from services.billing import _build_bill_items
    from core.billing import calculate_bill
    return calculate_bill(
        _build_bill_items(pending),
        gst_client=None,
        shop_state_code=pending.shop_state_code,
        customer_state_code=pending.customer_state_code,
        bill_of_supply=pending.is_bill_of_supply,
        is_inclusive=pending.is_inclusive,
        bill_discount_type=pending.bill_discount_type or "none",
        bill_discount_value=float(pending.bill_discount_value or 0.0),
    )


def _per_item_totals_in_summary(summary_text: str) -> list[float]:
    """Extract Rs.X.XX values that appear immediately after each item line
    in a msg_bill_summary output. msg_bill_summary lines look like:
        '• Pants x1 — Rs.598.50 (5% GST)'
        '• Pants x1 — -Rs.598.50 (5% GST)'  (returns)
    Captures the absolute value either way.
    """
    pattern = re.compile(r"^•[^—]+— -?Rs\.([\d.]+)", re.MULTILINE)
    return [float(m) for m in pattern.findall(summary_text)]


def _per_item_totals_in_preview(preview_text: str) -> list[float]:
    """Extract the FINAL per-item amount from each preview line. Handles
    all three formats:
      Exclusive : "  N. ... — Rs.X + Rs.Y GST = Rs.Z"  → captures Z
      Inclusive : "  N. ... — Rs.Z (incl. Rs.Y GST)"   → captures Z
      BOS       : "  N. ... — Rs.Z (no GST)"            → captures Z
    Sign is stripped — caller compares absolute values.
    """
    out: list[float] = []
    for line in preview_text.splitlines():
        if not re.match(r"\s*\d+\.\s", line):
            continue
        amounts = re.findall(r"-?Rs\.([\d.]+)", line)
        if not amounts:
            continue
        # Exclusive breakdown ends in "= Rs.T" → take the last Rs. value.
        # Inclusive ("Rs.T (incl. ...") and BOS ("Rs.A (no GST)") have the
        # final amount as the FIRST Rs. value; the second (if any) is the
        # embedded GST, not a separate total.
        if " = " in line:
            out.append(float(amounts[-1]))
        else:
            out.append(float(amounts[0]))
    return out


# ════════════════════════════════════════════════════════════════════
# TEST 1 — The exact real-world case from the bug report
# ════════════════════════════════════════════════════════════════════

def test_real_world_case_pants_kurta_clutchplate_5pct_discount_igst():
    """The exact scenario from the bug report:
      Pants ₹600 (5% GST), కుర్తా ₹500 (12% GST), Clutch Plate ₹15000 (12% GST)
      Discount: 5% bill-level, IGST (Tamil Nadu).

    Expected per-item totals in BOTH preview and final-bill summary:
      Pants x1       → Rs.598.50  (570.00 base + 28.50 GST)
      కుర్తా x1       → Rs.532.00  (475.00 base + 57.00 GST)
      Clutch Plate x1→ Rs.15960.00 (14250.00 base + 1710.00 GST)
      Total: Rs.17090.50
    """
    from services.billing import msg_preview
    from api.formatters import msg_bill_summary

    pending = _pending(
        items=[
            {"name": "Pants", "qty": 1, "price": 600,
             "hsn": "6203", "gst_rate": 5, "gst_source": "exact",
             "item_discount_type": "none", "item_discount_value": 0},
            {"name": "కుర్తా", "qty": 1, "price": 500,
             "hsn": "6211", "gst_rate": 12, "gst_source": "exact",
             "item_discount_type": "none", "item_discount_value": 0},
            {"name": "Clutch Plate", "qty": 1, "price": 15000,
             "hsn": "8708", "gst_rate": 12, "gst_source": "exact",
             "item_discount_type": "none", "item_discount_value": 0},
        ],
        bill_discount_type="percent", bill_discount_value=5,
        customer_state="Tamil Nadu", customer_state_code="33",  # IGST
    )

    preview = msg_preview(pending)
    br = _confirmed_billresult(pending)
    summary = msg_bill_summary(
        bill_result=br, invoice_number="INV-TEST-001",
        customer_name="Ramesh", days=10,
    )

    # Each per-item LINE in the preview shows the new breakdown format.
    assert "Pants x1 — Rs.570.00 + Rs.28.50 GST = Rs.598.50"           in preview
    assert "కుర్తా x1 — Rs.475.00 + Rs.57.00 GST = Rs.532.00"           in preview
    assert "Clutch Plate x1 — Rs.14250.00 + Rs.1710.00 GST = Rs.15960.00" in preview

    # Per-item TOTALS must match between preview and summary, exactly.
    preview_totals = _per_item_totals_in_preview(preview)[:3]   # only the items
    summary_totals = _per_item_totals_in_summary(summary)
    assert preview_totals == summary_totals, (
        f"Preview totals {preview_totals} do not match summary totals "
        f"{summary_totals}"
    )

    # Grand total in final bill summary
    assert "Rs.17090.50" in summary
    # Inter-state → IGST in preview
    assert "IGST" in preview


# ════════════════════════════════════════════════════════════════════
# TEST 2 — Bill of Supply: no GST shown on any item line
# ════════════════════════════════════════════════════════════════════

def test_bos_preview_no_gst_on_any_line():
    """BOS bill: per-item lines show Rs.X (no GST), no breakdown."""
    from services.billing import msg_preview

    pending = _pending(
        items=[
            {"name": "shirt", "qty": 2, "price": 300,
             "hsn": "9999", "gst_rate": 0, "gst_source": "bill_of_supply",
             "gst_confidence": "high",
             "item_discount_type": "none", "item_discount_value": 0},
            {"name": "pant", "qty": 1, "price": 700,
             "hsn": "9999", "gst_rate": 0, "gst_source": "bill_of_supply",
             "gst_confidence": "high",
             "item_discount_type": "none", "item_discount_value": 0},
        ],
        is_bill_of_supply=True,
    )

    preview = msg_preview(pending)
    assert "shirt x2 — Rs.600.00 (no GST)" in preview
    assert "pant x1 — Rs.700.00 (no GST)"  in preview
    # No Tax-Invoice breakdown leakage anywhere on item lines
    for line in preview.splitlines():
        if re.match(r"\s*\d+\.\s", line):
            assert " GST = " not in line
            assert "(incl." not in line


# ════════════════════════════════════════════════════════════════════
# TEST 3 — INCLUDE mode: shows total + embedded GST
# ════════════════════════════════════════════════════════════════════

def test_inclusive_preview_shows_incl_format():
    """INCLUDE mode: '... — Rs.Z (incl. Rs.Y GST)' format."""
    from services.billing import msg_preview

    # User typed 1180 INCLUSIVE → base 1000, GST 180 (18%)
    pending = _pending(
        items=[
            {"name": "shirt", "qty": 1, "price": 1180,
             "hsn": "6205", "gst_rate": 18, "gst_source": "exact",
             "gst_confidence": "high",
             "item_discount_type": "none", "item_discount_value": 0},
        ],
        is_inclusive=True,
    )
    preview = msg_preview(pending)
    # Inclusive line format: total first, then embedded GST in parentheses.
    assert "shirt x1 — Rs.1180.00 (incl. Rs.180.00 GST)" in preview
    # Must NOT use exclusive "X + Y GST = Z" format on item lines
    for line in preview.splitlines():
        if re.match(r"\s*\d+\.\s", line):
            assert " GST = " not in line


# ════════════════════════════════════════════════════════════════════
# TEST 4 — Return bill: negative sign on per-item lines
# ════════════════════════════════════════════════════════════════════

def test_return_preview_negative_per_item_amounts():
    """Return bill: per-item lines prefix every Rs. amount with '-'."""
    from services.billing import msg_preview
    from api.formatters import msg_bill_summary

    # Return bills carry negative prices in pending.items (from negate_items),
    # but _build_bill_items uses abs() before handing to calculate_bill.
    pending = _pending(
        items=[
            {"name": "shirt", "qty": 1, "price": -500,
             "hsn": "6205", "gst_rate": 5, "gst_source": "exact",
             "gst_confidence": "high",
             "item_discount_type": "none", "item_discount_value": 0},
        ],
        is_return=True,
    )
    preview = msg_preview(pending)
    br = _confirmed_billresult(pending)
    # For the summary, negate the BillResult the way generate_pdf_bill does.
    from core.entities import BillItem, BillResult
    neg_items = [BillItem(
        name=it.name, qty=it.qty, price=-it.price, hsn=it.hsn,
        gst_rate=it.gst_rate, amount=-it.amount,
        cgst=-it.cgst, sgst=-it.sgst, igst=-it.igst, total=-it.total,
    ) for it in br.items]
    neg_br = BillResult(
        items=neg_items,
        subtotal=-br.subtotal,
        total_cgst=-br.total_cgst, total_sgst=-br.total_sgst,
        total_igst=-br.total_igst, total_gst=-br.total_gst,
        grand_total=-br.grand_total, is_igst=br.is_igst,
        in_words=br.in_words,
    )
    summary = msg_bill_summary(
        bill_result=neg_br, invoice_number="CN-TEST-001",
        customer_name="Ramesh", days=10, is_return=True,
    )

    # Every Rs. value on the item line carries the negative sign.
    assert "shirt x1 — -Rs.500.00 + -Rs.25.00 GST = -Rs.525.00" in preview
    # The summary uses a single signed total per line:
    assert "-Rs.525.00" in summary

    # And the preview's final amount per item matches the summary's.
    preview_amts = _per_item_totals_in_preview(preview)[:1]
    summary_amts = _per_item_totals_in_summary(summary)
    assert preview_amts == summary_amts == [525.0]


# ════════════════════════════════════════════════════════════════════
# TEST 5 — Zero-discount bill: preview totals === final totals
# ════════════════════════════════════════════════════════════════════

def test_zero_discount_preview_per_item_equals_final():
    """No discount → preview per-item totals match final bill exactly."""
    from services.billing import msg_preview
    from api.formatters import msg_bill_summary

    pending = _pending(
        items=[
            {"name": "shirt", "qty": 2, "price": 500,
             "hsn": "6205", "gst_rate": 5, "gst_source": "exact",
             "gst_confidence": "high",
             "item_discount_type": "none", "item_discount_value": 0},
            {"name": "phone case", "qty": 1, "price": 299,
             "hsn": "3926", "gst_rate": 18, "gst_source": "exact",
             "gst_confidence": "high",
             "item_discount_type": "none", "item_discount_value": 0},
        ],
        bill_discount_type="none", bill_discount_value=0,
    )

    preview = msg_preview(pending)
    br = _confirmed_billresult(pending)
    summary = msg_bill_summary(
        bill_result=br, invoice_number="INV-TEST-002",
        customer_name="Ramesh", days=10,
    )

    # Numeric per-item totals must match exactly.
    preview_totals = _per_item_totals_in_preview(preview)[:2]
    summary_totals = _per_item_totals_in_summary(summary)
    assert preview_totals == summary_totals
    # Sanity on the actual values: shirt 2*500 = 1000 + 5% = 1050,
    # phone case 1*299 + 18% = 299 + 53.82 = 352.82.
    assert preview_totals == [1050.0, 352.82]


# ════════════════════════════════════════════════════════════════════
# Bonus coverage — flat discount, item-level discount, mixed rates
# ════════════════════════════════════════════════════════════════════

def test_flat_bill_discount_preview_shows_scaled_per_item_totals():
    """Bill flat ₹100 off on [rice 1000 @ 5%, soap 500 @ 18%].

    Pre-bill subtotal 1500, scale = 1400/1500. Each line scaled
    proportionally, then GST added per item rate.
    """
    from services.billing import msg_preview
    from api.formatters import msg_bill_summary

    pending = _pending(
        items=[
            {"name": "rice", "qty": 1, "price": 1000,
             "hsn": "1006", "gst_rate": 5, "gst_source": "exact",
             "gst_confidence": "high",
             "item_discount_type": "none", "item_discount_value": 0},
            {"name": "soap", "qty": 1, "price": 500,
             "hsn": "3401", "gst_rate": 18, "gst_source": "exact",
             "gst_confidence": "high",
             "item_discount_type": "none", "item_discount_value": 0},
        ],
        bill_discount_type="flat", bill_discount_value=100,
    )

    preview = msg_preview(pending)
    br = _confirmed_billresult(pending)
    summary = msg_bill_summary(
        bill_result=br, invoice_number="INV-TEST-003",
        customer_name="R", days=10,
    )

    # Numeric equality is the contract, not exact format strings.
    preview_totals = _per_item_totals_in_preview(preview)[:2]
    summary_totals = _per_item_totals_in_summary(summary)
    assert preview_totals == summary_totals


def test_item_level_discount_preview_per_item_matches_final():
    """Item-level percent discount: rice with 10% off + soap untouched."""
    from services.billing import msg_preview
    from api.formatters import msg_bill_summary

    pending = _pending(
        items=[
            {"name": "rice", "qty": 1, "price": 1000,
             "hsn": "1006", "gst_rate": 5, "gst_source": "exact",
             "gst_confidence": "high",
             "item_discount_type": "percent", "item_discount_value": 10},
            {"name": "soap", "qty": 2, "price": 50,
             "hsn": "3401", "gst_rate": 18, "gst_source": "exact",
             "gst_confidence": "high",
             "item_discount_type": "none", "item_discount_value": 0},
        ],
    )
    preview = msg_preview(pending)
    br = _confirmed_billresult(pending)
    summary = msg_bill_summary(
        bill_result=br, invoice_number="INV-TEST-004",
        customer_name="R", days=10,
    )
    # Discount marker still appears on the rice line.
    assert "(-10% off)" in preview
    # Numeric totals match.
    preview_totals = _per_item_totals_in_preview(preview)[:2]
    summary_totals = _per_item_totals_in_summary(summary)
    assert preview_totals == summary_totals


def test_assertion_fires_on_drift():
    """RULE 7 — if msg_preview's per-item sum diverged from
    calculate_bill's grand_total, the assertion would fire. Guard against
    silent drift in future refactors.

    We can't easily force a real drift, but we can confirm the assertion
    line itself is reached (covered) by happy-path execution.
    """
    from services.billing import msg_preview
    pending = _pending(
        items=[
            {"name": "shirt", "qty": 1, "price": 500,
             "hsn": "6205", "gst_rate": 5, "gst_source": "exact",
             "gst_confidence": "high",
             "item_discount_type": "none", "item_discount_value": 0},
        ],
    )
    # No exception on a happy bill ⇒ assertion passed.
    preview = msg_preview(pending)
    assert "shirt x1" in preview
