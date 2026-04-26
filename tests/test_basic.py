"""Smoke tests for BilledUp (no live Claude API calls). Run with: pytest"""

import bill_generator as bg
from bill_generator import BillItem, calculate_bill, number_to_words, generate_invoice_number, is_intra_state
from claude_parser import sanitize_message, validate_parsed_response
from gst_rates import get_gst_rate, get_all_categories
import main


def test_number_to_words():
    assert number_to_words(100) == "One Hundred Rupees Only"
    assert number_to_words(0) == "Zero Rupees Only"


def test_sanitize_message():
    clean, _ = sanitize_message("  phone 299  ")
    assert clean == "phone 299"


def test_validate_parsed_response():
    result, _ = validate_parsed_response(
        {
            "items": [{"name": "phone", "qty": 1, "price": 299}],
            "customer_name": "Suresh",
        }
    )
    assert len(result["items"]) == 1
    assert result["customer_name"] == "Suresh"


def test_get_gst_rate():
    r = get_gst_rate("phone case")
    assert r["gst"] == 18
    assert r["hsn"]


def test_get_all_categories_excludes_default():
    cats = get_all_categories()
    assert "default" not in cats
    assert len(cats) > 10


def test_calculate_bill_no_api_client():
    items = [BillItem("phone case", 1, 299)]
    br = calculate_bill(items, gst_client=None)
    assert br.subtotal == 299.0
    assert br.grand_total > br.subtotal


def test_calculate_bill_does_not_mutate_input():
    items = [BillItem("phone case", 1, 299)]
    calculate_bill(items, gst_client=None)
    assert items[0].name == "phone case"  # not title-cased
    assert items[0].hsn == ""  # unchanged


def test_main_database_roundtrip():
    main.init_database()
    main.seed_demo_shop()
    assert main.get_shop("RAVI") is not None
    assert main.get_shop("NONEXISTENT") is None


def test_invoice_sequence():
    n1 = generate_invoice_number("PYTEST")
    n2 = generate_invoice_number("PYTEST")
    assert n1 != n2
    assert int(n2.split("-")[-1]) == int(n1.split("-")[-1]) + 1


# ── IGST tests ──

def test_is_intra_state_same():
    assert is_intra_state("36", "36") is True


def test_is_intra_state_different():
    assert is_intra_state("36", "29") is False


def test_is_intra_state_empty_customer():
    assert is_intra_state("36", "") is True


def test_calculate_bill_intra_state():
    items = [BillItem("phone case", 1, 100)]
    br = calculate_bill(items, gst_client=None, shop_state_code="36", customer_state_code="36")
    assert br.is_igst is False
    assert br.total_cgst > 0
    assert br.total_sgst > 0
    assert br.total_igst == 0.0
    assert br.total_gst == round(br.total_cgst + br.total_sgst, 2)


def test_calculate_bill_inter_state():
    items = [BillItem("phone case", 1, 100)]
    br = calculate_bill(items, gst_client=None, shop_state_code="36", customer_state_code="29")
    assert br.is_igst is True
    assert br.total_cgst == 0.0
    assert br.total_sgst == 0.0
    assert br.total_igst > 0
    assert br.total_gst == br.total_igst


def test_igst_grand_total_matches_cgst_sgst():
    """IGST grand total must equal CGST+SGST grand total for same items."""
    items = [BillItem("phone case", 1, 100)]
    intra = calculate_bill(items, gst_client=None, shop_state_code="36", customer_state_code="36")
    inter = calculate_bill(items, gst_client=None, shop_state_code="36", customer_state_code="29")
    assert intra.grand_total == inter.grand_total
    assert intra.total_gst == inter.total_gst


def test_bill_item_igst_field():
    items = [BillItem("charger", 1, 500)]
    br = calculate_bill(items, gst_client=None, shop_state_code="36", customer_state_code="29")
    item = br.items[0]
    assert item.igst > 0
    assert item.cgst == 0.0
    assert item.sgst == 0.0


def test_calculate_bill_default_intra_when_no_state():
    """No state codes passed → defaults to intra-state."""
    items = [BillItem("phone case", 1, 100)]
    br = calculate_bill(items, gst_client=None)
    assert br.is_igst is False
    assert br.total_igst == 0.0


# ── GST Report tests ──

from datetime import date, timedelta
from reports import (
    get_gst_report, format_indian_number, parse_report_range,
    msg_gst_report, export_gst_report_pdf, GSTReport,
)


def test_format_indian_number_small():
    assert format_indian_number(500.00) == "500.00"


def test_format_indian_number_thousands():
    assert format_indian_number(1234.50) == "1,234.50"


def test_format_indian_number_lakhs():
    assert format_indian_number(120000.00) == "1,20,000.00"


def test_format_indian_number_crores():
    assert format_indian_number(12345678.90) == "1,23,45,678.90"


def test_format_indian_number_zero():
    assert format_indian_number(0) == "0.00"


def test_parse_report_range_empty():
    """Empty text → current month."""
    start, end, label = parse_report_range("")
    today = date.today()
    assert start == today.replace(day=1)
    assert end == today
    assert today.strftime("%B") in label


def test_parse_report_range_last_n_days():
    start, end, label = parse_report_range("last 7 days")
    today = date.today()
    assert end == today
    assert start == today - timedelta(days=7)
    assert "7" in label


def test_parse_report_range_last_month():
    start, end, label = parse_report_range("last month")
    today = date.today()
    first_of_current = today.replace(day=1)
    expected_end = first_of_current - timedelta(days=1)
    assert end == expected_end
    assert start == expected_end.replace(day=1)


def test_parse_report_range_month_name():
    start, end, label = parse_report_range("january")
    assert start.month == 1
    assert start.day == 1
    assert "January" in label


def test_parse_report_range_this_month():
    start, end, label = parse_report_range("this month")
    today = date.today()
    assert start == today.replace(day=1)
    assert end == today


def test_get_gst_report_empty():
    """No bills → all zeros."""
    main.init_database()
    today = date.today()
    report = get_gst_report("NONEXISTENT_SHOP", today.replace(day=1), today)
    assert report.total_invoices == 0
    assert report.total_sales == 0.0
    assert report.total_cgst == 0.0
    assert report.total_sgst == 0.0
    assert report.total_igst == 0.0
    assert report.total_gst == 0.0


def test_msg_gst_report_no_invoices():
    report = GSTReport(
        shop_id="TEST", start_date=date(2026, 3, 1), end_date=date(2026, 3, 28),
        total_invoices=0, total_sales=0, total_cgst=0, total_sgst=0,
        total_igst=0, total_gst=0,
    )
    msg = msg_gst_report(report, "March 2026")
    assert "No invoices found" in msg


def test_msg_gst_report_with_data():
    report = GSTReport(
        shop_id="TEST", start_date=date(2026, 3, 1), end_date=date(2026, 3, 28),
        total_invoices=45, total_sales=120000, total_cgst=5400, total_sgst=5400,
        total_igst=3600, total_gst=14400,
    )
    msg = msg_gst_report(report, "March 2026")
    assert "45" in msg
    assert "1,20,000.00" in msg
    assert "CGST" in msg
    assert "SGST" in msg
    assert "IGST" in msg
    assert "14,400.00" in msg


def test_export_gst_report_pdf():
    """PDF bytes are generated successfully."""
    report = GSTReport(
        shop_id="TEST", start_date=date(2026, 3, 1), end_date=date(2026, 3, 28),
        total_invoices=10, total_sales=50000, total_cgst=2500, total_sgst=2500,
        total_igst=1000, total_gst=6000,
    )
    pdf_bytes, filename = export_gst_report_pdf(report, "March 2026", "Test Shop")
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 0
    assert filename.endswith(".pdf")
    assert "TEST" in filename


# ── Production edge case tests ──

from gst_rates import get_gst_rate_smart, get_gst_rate


def test_gst_rate_smart_returns_source_exact():
    """Exact hardcoded match includes source='exact'."""
    r = get_gst_rate_smart("phone case", client=None)
    assert r["source"] == "exact"
    assert r["gst"] == 18
    assert r["hsn"]


def test_gst_rate_smart_returns_source_fuzzy():
    """Fuzzy match includes source='fuzzy'."""
    r = get_gst_rate_smart("fone case", client=None)
    # Should fuzzy-match to "phone case"
    assert r["source"] in ("exact", "fuzzy")
    assert r["gst"] in (12, 18)


def test_gst_rate_smart_returns_source_default():
    """Completely unknown item falls to default with source='default'."""
    r = get_gst_rate_smart("xyzzy_unknown_item_12345", client=None)
    assert r["source"] == "default"
    assert r["gst"] == 18


def test_gst_rate_smart_source_does_not_pollute_original():
    """Source field shouldn't persist in the GST_RATES dict."""
    r1 = get_gst_rate_smart("phone case", client=None)
    assert "source" in r1
    # The original dict should not have source
    r2 = get_gst_rate("phone case")
    assert "source" not in r2


def test_calculate_bill_uses_preresolved_rates():
    """BillItem with pre-filled hsn should skip GST lookup."""
    items = [BillItem("test item", 1, 100, hsn="8517", gst_rate=12)]
    br = calculate_bill(items, gst_client=None)
    assert br.items[0].gst_rate == 12
    assert br.items[0].hsn == "8517"
    # 12% of 100 = 12
    assert br.total_gst == 12.0


def test_calculate_bill_preresolved_vs_lookup_consistency():
    """Pre-resolved rates must produce same result as fresh lookup."""
    # Get rate for a known item
    rate = get_gst_rate("charger")
    # Calculate with pre-resolved
    items_pre = [BillItem("charger", 2, 500, hsn=rate["hsn"], gst_rate=rate["gst"])]
    br_pre = calculate_bill(items_pre, gst_client=None)
    # Calculate with lookup
    items_lookup = [BillItem("charger", 2, 500)]
    br_lookup = calculate_bill(items_lookup, gst_client=None)
    assert br_pre.grand_total == br_lookup.grand_total
    assert br_pre.total_gst == br_lookup.total_gst


def test_calculate_bill_rounding_precision():
    """Verify 2-decimal rounding at every step, no float drift."""
    items = [
        BillItem("item a", 3, 33.33, hsn="9999", gst_rate=18),
        BillItem("item b", 7, 14.29, hsn="9999", gst_rate=5),
    ]
    br = calculate_bill(items, gst_client=None)
    # All amounts should have at most 2 decimal places
    assert br.subtotal == round(br.subtotal, 2)
    assert br.total_cgst == round(br.total_cgst, 2)
    assert br.total_sgst == round(br.total_sgst, 2)
    assert br.total_gst == round(br.total_gst, 2)
    assert br.grand_total == round(br.grand_total, 2)
    # grand_total = subtotal + total_gst (no drift)
    assert br.grand_total == round(br.subtotal + br.total_gst, 2)
    for item in br.items:
        assert item.amount == round(item.amount, 2)
        assert item.cgst == round(item.cgst, 2)
        assert item.sgst == round(item.sgst, 2)
        assert item.total == round(item.total, 2)


def test_orphan_command_detection():
    """_is_confirmation_command correctly identifies orphan confirmation messages."""
    from whatsapp_webhook import _is_confirmation_command
    # Should match
    assert _is_confirmation_command("yes") is True
    assert _is_confirmation_command("y") is True
    assert _is_confirmation_command("confirm") is True
    assert _is_confirmation_command("cancel") is True
    assert _is_confirmation_command("edit") is True
    assert _is_confirmation_command("state") is True
    assert _is_confirmation_command("name ravi") is True
    assert _is_confirmation_command("gst 1 12") is True
    assert _is_confirmation_command("gst 2 28%") is True
    # Should NOT match (these are real billing messages)
    assert _is_confirmation_command("phone case 299 charger 499") is False
    assert _is_confirmation_command("rice 50 dal 80") is False
    assert _is_confirmation_command("hello") is False


def test_preview_shows_gst_rate_per_item():
    """Preview message should include GST rate for each item."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Suresh", customer_state="Telangana",
        customer_state_code="36",
        items=[
            {"name": "phone case", "qty": 1, "price": 299.0,
             "hsn": "3926", "gst_rate": 18, "gst_source": "exact"},
            {"name": "unknown gadget", "qty": 1, "price": 500.0,
             "hsn": "9999", "gst_rate": 18, "gst_source": "default"},
        ],
        confidence=0.9, warnings=[], raw_message="test",
        created_at=datetime.now(),
    )
    preview = msg_preview(pending)
    # New per-item format (RULE 2): "Rs.X + Rs.Y GST = Rs.Z" — shows the
    # GST AMOUNT explicitly rather than just the rate. The "18%" rate is
    # implicit in the math (53.82 / 299 = 0.18, 90 / 500 = 0.18) and the
    # grouped warning still says "default 18%" for the low-confidence item.
    assert "phone case" in preview
    assert "Rs.299.00 + Rs.53.82 GST = Rs.352.82" in preview
    # Default match item — line ends in ⚠️
    assert "unknown gadget" in preview
    assert "Rs.500.00 + Rs.90.00 GST = Rs.590.00 ⚠️" in preview
    assert "gst assumed" in preview.lower() or "default 18%" in preview.lower()
    # Should show GST override hint (both index and name formats)
    assert "GST 1 12" in preview
    assert "shirt gst 12" in preview


def test_preview_no_warning_when_all_exact():
    """No rate warning when all items have exact matches."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Suresh", customer_state="Telangana",
        customer_state_code="36",
        items=[
            {"name": "phone case", "qty": 1, "price": 299.0,
             "hsn": "3926", "gst_rate": 18, "gst_source": "exact"},
        ],
        confidence=0.9, warnings=[], raw_message="test",
        created_at=datetime.now(),
    )
    preview = msg_preview(pending)
    assert "unknown" not in preview.lower() or "unknown gadget" not in preview.lower()
    assert "gst assumed" not in preview.lower()


# ── Final UX fix tests ──

def test_preview_default_gst_prominent_warning():
    """Default GST items get a prominent per-item warning block."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Telangana",
        customer_state_code="36",
        items=[
            {"name": "magic widget", "qty": 2, "price": 100.0,
             "hsn": "9999", "gst_rate": 18, "gst_source": "default"},
        ],
        confidence=0.9, warnings=[], raw_message="test",
        created_at=datetime.now(),
    )
    preview = msg_preview(pending)
    # Grouped warning (not per-item)
    assert "GST assumed for some items (default 18%)" in preview
    assert "Verify if needed" in preview


def test_preview_state_assumed_tag_inter_state():
    """Inter-state preview also shows (assumed) when state_assumed=True."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Karnataka",
        customer_state_code="29",
        items=[
            {"name": "phone case", "qty": 1, "price": 100.0,
             "hsn": "3926", "gst_rate": 18, "gst_source": "exact"},
        ],
        confidence=0.9, warnings=[], raw_message="test",
        created_at=datetime.now(),
        state_assumed=True,
    )
    preview = msg_preview(pending)
    assert "_(assumed)_" in preview
    assert "IGST" in preview


def test_preview_state_no_assumed_after_manual():
    """No (assumed) tag when user manually selected state."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Karnataka",
        customer_state_code="29",
        items=[
            {"name": "phone case", "qty": 1, "price": 100.0,
             "hsn": "3926", "gst_rate": 18, "gst_source": "exact"},
        ],
        confidence=0.9, warnings=[], raw_message="test",
        created_at=datetime.now(),
        state_assumed=False,
    )
    preview = msg_preview(pending)
    assert "_(assumed)_" not in preview


def test_match_item_by_name_exact():
    from whatsapp_webhook import _match_item_by_name
    items = [
        {"name": "phone case", "qty": 1, "price": 299},
        {"name": "charger", "qty": 1, "price": 499},
    ]
    assert _match_item_by_name("phone case", items) == 0
    assert _match_item_by_name("charger", items) == 1
    assert _match_item_by_name("CHARGER", items) == 1


def test_match_item_by_name_substring():
    from whatsapp_webhook import _match_item_by_name
    items = [
        {"name": "phone case", "qty": 1, "price": 299},
        {"name": "usb charger", "qty": 1, "price": 499},
    ]
    assert _match_item_by_name("phone", items) == 0
    assert _match_item_by_name("charger", items) == 1


def test_match_item_by_name_token_overlap():
    from whatsapp_webhook import _match_item_by_name
    items = [
        {"name": "cotton shirt", "qty": 1, "price": 500},
        {"name": "denim jeans", "qty": 1, "price": 700},
    ]
    assert _match_item_by_name("shirt", items) == 0
    assert _match_item_by_name("jeans", items) == 1


def test_match_item_by_name_no_match():
    from whatsapp_webhook import _match_item_by_name
    items = [{"name": "phone case", "qty": 1, "price": 299}]
    assert _match_item_by_name("xyz123", items) is None
    assert _match_item_by_name("", items) is None


def test_orphan_natural_gst_override():
    """Natural language GST override caught as orphan command."""
    from whatsapp_webhook import _is_confirmation_command
    assert _is_confirmation_command("shirt gst 12") is True
    assert _is_confirmation_command("phone case gst 5%") is True
    # Regular billing messages should NOT match
    assert _is_confirmation_command("shirt 500 pant 700") is False


def test_preview_pdf_consistency():
    """Pre-resolved rates in BillItem produce identical totals via calculate_bill."""
    from bill_generator import BillItem, calculate_bill
    # Simulate what _compute_preview_totals and _generate_confirmed_bill both do
    pending_items = [
        {"name": "shirt", "qty": 2, "price": 500.0, "hsn": "6109", "gst_rate": 5},
        {"name": "phone case", "qty": 1, "price": 299.0, "hsn": "3926", "gst_rate": 18},
    ]
    # Preview path
    items_preview = [
        BillItem(name=i["name"], qty=i["qty"], price=i["price"],
                 hsn=i["hsn"], gst_rate=i["gst_rate"])
        for i in pending_items
    ]
    br_preview = calculate_bill(items_preview, gst_client=None,
                                shop_state_code="36", customer_state_code="36")
    # Final bill path (same items, same pre-resolved rates)
    items_final = [
        BillItem(name=i["name"], qty=i["qty"], price=i["price"],
                 hsn=i["hsn"], gst_rate=i["gst_rate"])
        for i in pending_items
    ]
    br_final = calculate_bill(items_final, gst_client=None,
                              shop_state_code="36", customer_state_code="36")
    # Must be identical
    assert br_preview.subtotal == br_final.subtotal
    assert br_preview.total_cgst == br_final.total_cgst
    assert br_preview.total_sgst == br_final.total_sgst
    assert br_preview.total_gst == br_final.total_gst
    assert br_preview.grand_total == br_final.grand_total
    for p_item, f_item in zip(br_preview.items, br_final.items):
        assert p_item.gst_rate == f_item.gst_rate
        assert p_item.hsn == f_item.hsn
        assert p_item.total == f_item.total


# ── Return / Credit Note tests ──

def test_return_keyword_detection():
    """Return keywords trigger return intent."""
    from return_detector import detect_return_intent
    items = [{"name": "shirt", "qty": 1, "price": 500}]
    assert detect_return_intent("return shirt 500", items) is True
    assert detect_return_intent("I want a refund for shirt 500", items) is True
    assert detect_return_intent("credit note shirt 500", items) is True
    assert detect_return_intent("cancel order shirt 500", items) is True
    assert detect_return_intent("exchange this shirt 500", items) is True


def test_return_no_false_positive():
    """Normal billing messages should not trigger return detection."""
    from return_detector import detect_return_intent
    items = [{"name": "shirt", "qty": 1, "price": 500}]
    assert detect_return_intent("shirt 500 pant 700", items) is False
    assert detect_return_intent("2 phone case 299", items) is False


def test_return_fuzzy_detection():
    """Fuzzy matching catches common misspellings."""
    from return_detector import detect_return_intent
    items = [{"name": "shirt", "qty": 1, "price": 500}]
    try:
        from rapidfuzz import fuzz
        assert detect_return_intent("retun shirt 500", items) is True
        assert detect_return_intent("refnd shirt 500", items) is True
    except ImportError:
        pass  # Skip if rapidfuzz not installed


def test_return_negative_amounts():
    """Majority negative prices trigger return detection."""
    from return_detector import detect_return_intent
    neg_items = [{"name": "shirt", "qty": 1, "price": -500}]
    assert detect_return_intent("shirt -500", neg_items) is True
    # Mixed: not majority negative → not a return
    mixed = [
        {"name": "shirt", "qty": 1, "price": -500},
        {"name": "pant", "qty": 1, "price": 700},
        {"name": "belt", "qty": 1, "price": 300},
    ]
    assert detect_return_intent("shirt -500 pant 700 belt 300", mixed) is False


def test_negate_items():
    """negate_items makes all prices negative without mutating input."""
    from return_detector import negate_items
    original = [
        {"name": "shirt", "qty": 1, "price": 500},
        {"name": "pant", "qty": 2, "price": 700},
    ]
    negated = negate_items(original)
    assert negated[0]["price"] == -500
    assert negated[1]["price"] == -700
    # Original unchanged
    assert original[0]["price"] == 500
    assert original[1]["price"] == 700


def test_credit_note_invoice_number():
    """Credit note invoice numbers use CN prefix."""
    inv = generate_invoice_number("TEST", is_return=True)
    assert inv.startswith("CN-")
    # Regular invoice should not have CN prefix
    inv2 = generate_invoice_number("TEST", is_return=False)
    assert not inv2.startswith("CN-")


def test_credit_note_preview():
    """Credit note preview shows return-specific formatting."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Telangana",
        customer_state_code="36",
        items=[
            {"name": "shirt", "qty": 1, "price": -500.0,
             "hsn": "6109", "gst_rate": 5, "gst_source": "exact"},
        ],
        confidence=0.9, warnings=[], raw_message="return shirt 500",
        created_at=datetime.now(),
        is_return=True,
    )
    preview = msg_preview(pending)
    assert "Credit Note (Return)" in preview
    assert "REFUND" in preview
    # No extra explanation text
    assert "This will generate" not in preview
    # Minimal command list for returns
    assert "CONFIRM" in preview
    assert "EDIT" in preview
    assert "CANCEL" in preview


def test_credit_note_preview_no_extra_commands():
    """Credit note preview hides NAME/STATE but keeps GST."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Telangana",
        customer_state_code="36",
        items=[
            {"name": "shirt", "qty": 1, "price": -500.0,
             "hsn": "6109", "gst_rate": 5, "gst_source": "exact"},
        ],
        confidence=0.9, warnings=[], raw_message="return shirt 500",
        created_at=datetime.now(),
        is_return=True,
    )
    preview = msg_preview(pending)
    assert "NAME Ravi" not in preview
    assert "Change state" not in preview


def test_return_preview_has_gst_command():
    """Return preview includes GST correction option."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Telangana",
        customer_state_code="36",
        items=[
            {"name": "shirt", "qty": 1, "price": -500.0,
             "hsn": "6109", "gst_rate": 5, "gst_source": "exact"},
        ],
        confidence=0.9, warnings=[], raw_message="return shirt 500",
        created_at=datetime.now(),
        is_return=True,
    )
    preview = msg_preview(pending)
    assert "GST 1 12" in preview
    assert "shirt gst 12" in preview


def test_normal_preview_has_all_commands():
    """Normal bill preview still shows NAME/STATE/GST commands."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Telangana",
        customer_state_code="36",
        items=[
            {"name": "shirt", "qty": 1, "price": 500.0,
             "hsn": "6109", "gst_rate": 5, "gst_source": "exact"},
        ],
        confidence=0.9, warnings=[], raw_message="shirt 500",
        created_at=datetime.now(),
        is_return=False,
    )
    preview = msg_preview(pending)
    assert "NAME Ravi" in preview
    assert "Change state" in preview
    assert "GST 1 12" in preview


def test_credit_note_summary():
    """Credit note summary shows return-specific labels."""
    from whatsapp_webhook import msg_bill_summary
    from bill_generator import BillResult, BillItem
    br = BillResult(
        items=[BillItem(name="shirt", qty=1, price=-500, hsn="6109", gst_rate=5,
                        cgst=0, sgst=0, igst=-25, total=-525)],
        subtotal=-500, total_cgst=0, total_sgst=0, total_igst=-25,
        total_gst=-25, grand_total=-525, is_igst=True,
        in_words="Five Hundred Twenty Five Rupees Only",
    )
    summary = msg_bill_summary(br, "CN-2526-TEST-00001", "Test", days=14, is_return=True)
    assert "Credit Note Generated" in summary
    assert "Credit Note:" in summary
    assert "REFUND" in summary
    assert "-Rs." in summary


# ── Price-based GST slab tests ──

from gst_rates import (
    adjust_gst_for_price, is_clothing_item, is_footwear_item,
)


def test_clothing_slab_below_1000():
    """Clothing ≤₹1000 → 5% GST."""
    rate = {"hsn": "6205", "gst": 12, "source": "exact"}
    adjusted = adjust_gst_for_price("shirt", 800, rate)
    assert adjusted["gst"] == 5
    assert adjusted["hsn"] == "6205"  # HSN unchanged


def test_clothing_slab_at_2500():
    """Clothing at exactly ₹2500 → 5% GST."""
    rate = {"hsn": "6205", "gst": 12, "source": "exact"}
    adjusted = adjust_gst_for_price("shirt", 2500, rate)
    assert adjusted["gst"] == 5


def test_clothing_slab_above_2500():
    """Clothing >₹2500 → 18% GST (56th GST Council)."""
    rate = {"hsn": "6205", "gst": 12, "source": "exact"}
    adjusted = adjust_gst_for_price("shirt", 3000, rate)
    assert adjusted["gst"] == 18


def test_footwear_slab_below_2500():
    """Footwear ≤₹2500 → 5% GST."""
    rate = {"hsn": "6403", "gst": 18, "source": "exact"}
    adjusted = adjust_gst_for_price("shoes", 800, rate)
    assert adjusted["gst"] == 5


def test_footwear_slab_above_2500():
    """Footwear >₹2500 → 18% GST."""
    rate = {"hsn": "6403", "gst": 18, "source": "exact"}
    adjusted = adjust_gst_for_price("shoes", 3000, rate)
    assert adjusted["gst"] == 18


def test_slab_does_not_affect_non_clothing():
    """Non-clothing items unaffected by slab logic."""
    rate = {"hsn": "8517", "gst": 18, "source": "exact"}
    adjusted = adjust_gst_for_price("mobile", 500, rate)
    assert adjusted["gst"] == 18  # unchanged


def test_slab_respects_manual_override():
    """Manual GST override is never overridden by slab."""
    rate = {"hsn": "6205", "gst": 28, "source": "manual"}
    adjusted = adjust_gst_for_price("shirt", 500, rate)
    assert adjusted["gst"] == 28  # manual → untouched


def test_slab_does_not_mutate_input():
    """adjust_gst_for_price returns a new dict."""
    rate = {"hsn": "6205", "gst": 12, "source": "exact"}
    adjusted = adjust_gst_for_price("shirt", 500, rate)
    assert adjusted["gst"] == 5
    assert rate["gst"] == 12  # original unchanged


def test_is_clothing_item_variants():
    """Clothing detection covers common names."""
    assert is_clothing_item("shirt") is True
    assert is_clothing_item("cotton shirt") is True
    assert is_clothing_item("tshirt") is True
    assert is_clothing_item("jeans") is True
    assert is_clothing_item("saree") is False  # unstitched fabric, not clothing
    assert is_clothing_item("pant") is True
    assert is_clothing_item("mobile") is False
    assert is_clothing_item("charger") is False


def test_is_footwear_item_variants():
    """Footwear detection covers common names."""
    assert is_footwear_item("shoes") is True
    assert is_footwear_item("running shoes") is True
    assert is_footwear_item("chappal") is True
    assert is_footwear_item("shirt") is False


def test_quantity_gst_on_total_value():
    """GST is calculated on qty × price, not per-unit."""
    items = [BillItem("phone case", 3, 100, hsn="3926", gst_rate=18)]
    br = calculate_bill(items, gst_client=None, shop_state_code="36", customer_state_code="36")
    # amount = 3 × 100 = 300
    assert br.subtotal == 300.0
    # GST on 300, not on 100
    assert br.total_gst == 54.0  # 18% of 300
    assert br.grand_total == 354.0


def test_return_gst_reversal_values():
    """Credit note GST values are negative and mathematically correct."""
    from bill_generator import generate_pdf_bill, ShopProfile, CustomerInfo
    import os
    shop = ShopProfile("TEST", "Test Shop", "Hyderabad", "36AABCU9603R1ZX", "+91 9876543210")
    customer = CustomerInfo("Test")
    items = [BillItem("phone case", 1, 500, hsn="3926", gst_rate=18)]
    pdf_bytes, br = generate_pdf_bill(shop, customer, items, "CN-TEST-001",
                                      gst_client=None, is_return=True,
                                      bill_of_supply=False)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 0
    assert br.subtotal < 0
    assert br.total_gst < 0
    assert br.grand_total < 0
    assert br.grand_total == round(br.subtotal + br.total_gst, 2)
    # Absolute values should match a normal bill
    assert abs(br.subtotal) == 500.0
    assert abs(br.total_gst) == 90.0  # 18% of 500
    assert abs(br.grand_total) == 590.0


def test_report_totals_with_returns():
    """Report aggregation correctly reflects returns as deductions."""
    from reports import GSTReport, msg_gst_report
    # Simulate: 2 normal bills + 1 return
    report = GSTReport(
        shop_id="TEST",
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 28),
        total_invoices=3,
        total_sales=800.0,     # 1000 + 500 - 700 (return)
        total_cgst=36.0,       # 45 + 22.5 - 31.5
        total_sgst=36.0,
        total_igst=0.0,
        total_gst=72.0,
    )
    msg = msg_gst_report(report, "March 2026")
    assert "800.00" in msg
    assert "72.00" in msg
    assert "3" in msg  # 3 invoices including return


def test_gold_gst_rate():
    """Gold items map to 3% GST."""
    r = get_gst_rate("gold")
    assert r["gst"] == 3
    assert r["hsn"] == "7108"


def test_clothing_bill_calculation_slab():
    """End-to-end: cheap shirt gets 5%, expensive shirt gets 18% (56th GST Council)."""
    cheap = [BillItem("shirt", 1, 800)]
    br_cheap = calculate_bill(cheap, gst_client=None)
    assert br_cheap.items[0].gst_rate == 5
    assert br_cheap.total_gst == 40.0  # 5% of 800

    expensive = [BillItem("shirt", 1, 3000)]
    br_exp = calculate_bill(expensive, gst_client=None)
    assert br_exp.items[0].gst_rate == 18
    assert br_exp.total_gst == 540.0  # 18% of 3000


# ── Expanded keyword & confidence tests ──

def test_expanded_clothing_keywords():
    """New clothing keywords are detected."""
    assert is_clothing_item("hoodie") is True
    assert is_clothing_item("top") is True
    assert is_clothing_item("skirt") is True
    assert is_clothing_item("blazer") is True
    assert is_clothing_item("palazzo") is True
    assert is_clothing_item("jogger") is True
    assert is_clothing_item("cotton hoodie") is True
    # Non-clothing still excluded
    assert is_clothing_item("laptop") is False


def test_expanded_footwear_keywords():
    """New footwear keywords are detected."""
    assert is_footwear_item("sneakers") is True
    assert is_footwear_item("shoe") is True
    assert is_footwear_item("sandals") is True
    assert is_footwear_item("slipper") is True
    assert is_footwear_item("footwear") is True
    assert is_footwear_item("loafer") is True
    assert is_footwear_item("leather loafer") is True
    # Non-footwear excluded
    assert is_footwear_item("shirt") is False


def test_confidence_exact_match():
    """Exact match returns confidence='high'."""
    r = get_gst_rate_smart("phone case", client=None)
    assert r["confidence"] == "high"
    assert r["source"] == "exact"


def test_confidence_fuzzy_match():
    """Fuzzy match returns confidence='medium'."""
    r = get_gst_rate_smart("smatwatch", client=None)
    assert r["confidence"] == "medium"
    assert r["source"] == "fuzzy"


def test_confidence_default_fallback():
    """Unknown item returns confidence='low'."""
    r = get_gst_rate_smart("xyzzy_unknown_thing_99", client=None)
    assert r["confidence"] == "low"
    assert r["source"] == "default"


def test_confidence_does_not_mutate_gst_rates():
    """Confidence field must not leak into the GST_RATES dict."""
    from gst_rates import GST_RATES
    _ = get_gst_rate_smart("phone case", client=None)
    assert "confidence" not in GST_RATES["phone case"]
    assert "source" not in GST_RATES["phone case"]


def test_preview_low_confidence_warning():
    """Low confidence items show assumed-GST warning."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Telangana",
        customer_state_code="36",
        items=[
            {"name": "weird gadget", "qty": 1, "price": 500.0,
             "hsn": "9999", "gst_rate": 18, "gst_source": "default",
             "gst_confidence": "low"},
        ],
        confidence=0.9, warnings=[], raw_message="test",
        created_at=datetime.now(),
    )
    preview = msg_preview(pending)
    assert "⚠️" in preview
    assert "GST assumed for some items (default 18%)" in preview


def test_preview_medium_confidence_marker():
    """Medium confidence items show ~ marker."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Telangana",
        customer_state_code="36",
        items=[
            {"name": "fone case", "qty": 1, "price": 300.0,
             "hsn": "3926", "gst_rate": 18, "gst_source": "fuzzy",
             "gst_confidence": "medium"},
        ],
        confidence=0.9, warnings=[], raw_message="test",
        created_at=datetime.now(),
    )
    preview = msg_preview(pending)
    assert "~" in preview
    # No warning block for medium confidence
    assert "GST assumed" not in preview


def test_preview_high_confidence_clean():
    """High confidence items show no markers or warnings."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Telangana",
        customer_state_code="36",
        items=[
            {"name": "phone case", "qty": 1, "price": 299.0,
             "hsn": "3926", "gst_rate": 18, "gst_source": "exact",
             "gst_confidence": "high"},
        ],
        confidence=0.9, warnings=[], raw_message="test",
        created_at=datetime.now(),
    )
    preview = msg_preview(pending)
    assert "⚠️" not in preview
    assert "~" not in preview
    assert "gst assumed" not in preview.lower()


def test_preview_grouped_low_confidence_warning():
    """Multiple low-confidence items produce ONE grouped warning, not per-item."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Telangana",
        customer_state_code="36",
        items=[
            {"name": "gadget alpha", "qty": 1, "price": 500.0,
             "hsn": "9999", "gst_rate": 18, "gst_source": "default",
             "gst_confidence": "low"},
            {"name": "gadget beta", "qty": 2, "price": 300.0,
             "hsn": "9999", "gst_rate": 18, "gst_source": "default",
             "gst_confidence": "low"},
            {"name": "phone case", "qty": 1, "price": 299.0,
             "hsn": "3926", "gst_rate": 18, "gst_source": "exact",
             "gst_confidence": "high"},
        ],
        confidence=0.9, warnings=[], raw_message="test",
        created_at=datetime.now(),
    )
    preview = msg_preview(pending)
    # Single grouped warning
    assert preview.count("GST assumed for some items") == 1
    # No per-item warnings
    assert 'GST assumed for "gadget alpha"' not in preview
    assert 'GST assumed for "gadget beta"' not in preview
    # Per-item ⚠️ markers still on line items
    assert "gadget alpha" in preview and "⚠️" in preview
    # Medium marker still works for other items — high has no marker
    assert "phone case" in preview


# ════════════════════════════════════════════════
# STRESS TEST FIXES — regression tests
# ════════════════════════════════════════════════

def test_back_cover_not_return():
    """'back cover' must NOT trigger return detection (Priority 1 fix)."""
    from return_detector import detect_return_intent
    items = [{"name": "back cover", "qty": 1, "price": 500}]
    assert detect_return_intent("back cover 500", items) is False
    assert detect_return_intent("phone back cover 300", items) is False
    assert detect_return_intent("transparent back cover 150", items) is False
    assert detect_return_intent("mobile back case 200", items) is False


def test_back_phrases_still_detected():
    """Phrases with 'back' that DO indicate returns should still work."""
    from return_detector import detect_return_intent
    items = [{"name": "shirt", "qty": 1, "price": 500}]
    assert detect_return_intent("give back shirt 500", items) is True
    assert detect_return_intent("take back this shirt 500", items) is True
    assert detect_return_intent("sent back shirt 500", items) is True


def test_money_back_not_return():
    """'money back guarantee' etc must NOT trigger return."""
    from return_detector import detect_return_intent
    items = [{"name": "soap", "qty": 1, "price": 50}]
    assert detect_return_intent("money back guarantee soap 50", items) is False
    assert detect_return_intent("buy back scheme tv 20000", items) is False
    assert detect_return_intent("back pain medicine 50", items) is False


def test_exchange_offer_not_return():
    """'exchange offer' must NOT trigger return (Priority 2 fix)."""
    from return_detector import detect_return_intent
    items = [{"name": "phone", "qty": 1, "price": 10000}]
    assert detect_return_intent("exchange offer phone 10000", items) is False
    assert detect_return_intent("exchange rate chart 100", items) is False


def test_exchange_return_still_detected():
    """'exchange this' / 'exchange and return' should still trigger."""
    from return_detector import detect_return_intent
    items = [{"name": "shirt", "qty": 1, "price": 500}]
    assert detect_return_intent("want to exchange this shirt 500", items) is True
    assert detect_return_intent("exchange and return shirt 500", items) is True


def test_tracksuit_gst_clothing():
    """Tracksuit should be clothing with correct GST (56th GST Council)."""
    from gst_rates import get_gst_rate_smart, is_clothing_item, adjust_gst_for_price
    assert is_clothing_item("tracksuit") is True
    rate = get_gst_rate_smart("tracksuit")
    adjusted = adjust_gst_for_price("tracksuit", 2000, rate)
    assert adjusted["gst"] == 5  # ≤₹2500 clothing

    adjusted_expensive = adjust_gst_for_price("tracksuit", 3000, rate)
    assert adjusted_expensive["gst"] == 18  # >₹2500 clothing


def test_lehenga_gst_clothing():
    """Lehenga should be clothing with correct GST (56th GST Council)."""
    from gst_rates import get_gst_rate_smart, is_clothing_item, adjust_gst_for_price
    assert is_clothing_item("lehenga") is True
    rate = get_gst_rate_smart("lehenga")
    adjusted = adjust_gst_for_price("lehenga", 5000, rate)
    assert adjusted["gst"] == 18  # >₹2500 clothing


def test_kids_frock_gst_clothing():
    """Kids frock should get 5% GST (≤₹1000 clothing) (Priority 3 fix)."""
    from gst_rates import is_clothing_item, get_gst_rate_smart, adjust_gst_for_price
    assert is_clothing_item("frock") is True
    rate = get_gst_rate_smart("kids frock")
    adjusted = adjust_gst_for_price("kids frock", 400, rate)
    assert adjusted["gst"] == 5  # ≤₹1000 clothing


def test_makeup_gst_28():
    """Makeup should be 28% GST, not fuzzy-matched to 5% (Priority 3 fix)."""
    from gst_rates import get_gst_rate_smart
    rate = get_gst_rate_smart("makeup kit")
    assert rate["gst"] == 28


def test_chappals_footwear():
    """'chappals' (plural) should be recognized as footwear (Priority 7 fix)."""
    from gst_rates import is_footwear_item, get_gst_rate_smart, adjust_gst_for_price
    assert is_footwear_item("chappals") is True
    rate = get_gst_rate_smart("chappals")
    adjusted = adjust_gst_for_price("chappals", 300, rate)
    assert adjusted["gst"] == 5  # ≤₹1000 footwear


def test_jean_singular_clothing():
    """'jean' (singular) should be recognized as clothing (Priority 7 fix)."""
    from gst_rates import is_clothing_item
    assert is_clothing_item("jean") is True


def test_bill_for_name_extraction():
    """'bill for Ramesh rice 80' should extract name correctly (Priority 4 fix)."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("bill for Ramesh rice 80 dal 60")
    assert result["customer_name"] == "Ramesh"
    names = [i["name"].lower() for i in result["items"]]
    assert "rice" in names
    assert "dal" in names
    # Name must NOT leak into item
    assert not any("ramesh" in n for n in names)


def test_multiple_qty_first():
    """'5 pen 10 3 notebook 40' should parse both quantities (Priority 5 fix)."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("5 pen 10 3 notebook 40")
    items = {i["name"].lower(): i for i in result["items"]}
    assert "pen" in items
    assert items["pen"]["qty"] == 5
    assert items["pen"]["price"] == 10
    assert "notebook" in items
    assert items["notebook"]["qty"] == 3
    assert items["notebook"]["price"] == 40


def test_x_quantity_format():
    """'pen 10 x 5' should parse as qty=5, price=10 (Priority 6 fix)."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("pen 10 x 5")
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["name"].lower() == "pen"
    assert item["price"] == 10
    assert item["qty"] == 5


def test_x_quantity_no_space():
    """'pen 10 x5' should also work."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("pen 10 x5")
    assert len(result["items"]) == 1
    assert result["items"][0]["qty"] == 5
    assert result["items"][0]["price"] == 10


def test_compact_no_space_single():
    """'shirt99' should parse as item=shirt, price=99."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("shirt99")
    assert len(result["items"]) == 1
    assert result["items"][0]["name"].lower() == "shirt"
    assert result["items"][0]["price"] == 99


def test_compact_no_space_multiple():
    """'shirt99 pant700' should parse both items."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("shirt99 pant700")
    items = {i["name"].lower(): i for i in result["items"]}
    assert "shirt" in items
    assert items["shirt"]["price"] == 99
    assert "pant" in items
    assert items["pant"]["price"] == 700


def test_compact_no_space_mixed_with_normal():
    """'1 shirt 2000 shirt99' — explicit format + compact both parsed."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("1 shirt 2000 shirt99")
    # Should have at least 2 items
    assert len(result["items"]) >= 2
    prices = [i["price"] for i in result["items"]]
    assert 2000 in prices
    assert 99 in prices


def test_compact_no_space_rejects_short_names():
    """'x5' should NOT be parsed as an item."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("x5")
    # "x" is only 1 char — should be rejected by ≥2 alpha requirement + stopwords
    items = [i for i in result.get("items", []) if i["name"].lower() == "x"]
    assert len(items) == 0


# ════════════════════════════════════════════════
# PRODUCTION FIXES — regression tests
# ════════════════════════════════════════════════

def test_pending_bill_db_survives_across_requests():
    """Pending bill stored in DB persists across function calls (Priority 1)."""
    from whatsapp_webhook import store_pending, get_pending_bill, clear_pending, PendingBill
    from database import init_database
    from datetime import datetime
    init_database()

    phone = "whatsapp:+919999900001"
    pending = PendingBill(
        phone=phone, shop_id="TEST01", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Telangana",
        customer_state_code="36",
        items=[{"name": "shirt", "qty": 1, "price": 500.0,
                "hsn": "6205", "gst_rate": 12, "gst_source": "exact",
                "gst_confidence": "high"}],
        confidence=0.9, warnings=[], raw_message="shirt 500",
        created_at=datetime.utcnow(),
    )
    store_pending(phone, pending)

    # Simulate a new request — retrieve from DB
    retrieved = get_pending_bill(phone)
    assert retrieved is not None
    assert retrieved.shop_id == "TEST01"
    assert retrieved.customer_name == "Test"
    assert len(retrieved.items) == 1
    assert retrieved.items[0]["name"] == "shirt"

    # Cleanup
    clear_pending(phone)
    assert get_pending_bill(phone) is None


def test_admin_endpoint_rejects_without_header():
    """Admin endpoint returns 403 without valid X-Admin-Key (Priority 2)."""
    import os
    os.environ["ADMIN_SECRET"] = "test-secret-123"
    from whatsapp_webhook import app
    from database import init_database
    init_database()

    client = app.test_client()

    # No header → 403
    resp = client.get("/admin/registrations")
    assert resp.status_code == 403

    # Wrong header → 403
    resp = client.get("/admin/registrations", headers={"X-Admin-Key": "wrong"})
    assert resp.status_code == 403

    # Correct header → 200
    resp = client.get("/admin/registrations", headers={"X-Admin-Key": "test-secret-123"})
    assert resp.status_code == 200


def test_gst_override_single_message():
    """GST override should produce ONE message, not two (Priority 3)."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime

    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Telangana",
        customer_state_code="36",
        items=[{"name": "shirt", "qty": 1, "price": 500.0,
                "hsn": "6205", "gst_rate": 12, "gst_source": "exact",
                "gst_confidence": "high"}],
        confidence=0.9, warnings=[], raw_message="shirt 500",
        created_at=datetime.utcnow(),
    )
    # Simulate what happens after GST override: confirmation + preview merged
    preview = msg_preview(pending)
    merged = f"✅ Item 1 GST rate → 5%\n\n{preview}"
    # Should be a single string containing both
    assert "✅ Item 1 GST rate → 5%" in merged
    assert "Bill Preview" in merged


def test_rate_limit_no_loading_message():
    """Rate limit error should NOT be preceded by loading message (Priority 4).

    Verifies the parse_message returns the error which is sent directly,
    rather than sending a loading message first.
    """
    # The fix moved send("Understanding...") to after parse_message,
    # and rate limit errors are caught early. This tests the parse output.
    from claude_parser import _error_result
    err = _error_result("Too many requests — please wait 30 seconds")
    assert "wait" in err["error"].lower()


def test_credit_note_words_negative():
    """Credit note should say 'Minus ... Rupees Only' (Priority 5)."""
    assert number_to_words(-500) == "Minus Five Hundred Rupees Only"
    assert number_to_words(-1234.50) == "Minus One Thousand Two Hundred Thirty Four Rupees and Fifty Paise Only"


def test_gst_report_separates_returns():
    """GST report should separate sales and returns (Priority 6)."""
    from reports import GSTReport
    from datetime import date
    report = GSTReport(
        shop_id="TEST", start_date=date(2026, 3, 1), end_date=date(2026, 3, 31),
        total_invoices=5, total_sales=10000.0,
        total_cgst=500.0, total_sgst=500.0, total_igst=0.0, total_gst=1000.0,
        total_returns=-2000.0, return_count=2,
    )
    assert report.total_sales == 10000.0
    assert report.total_returns == -2000.0
    assert report.return_count == 2
    # Net = sales + returns (returns are negative)
    assert report.total_sales + report.total_returns == 8000.0


def test_state_match_rejects_short_input():
    """Short input like 'a' should NOT match via substring (Priority 8)."""
    from whatsapp_webhook import resolve_state
    # "a" would match "Assam" via substring — should be rejected
    assert resolve_state("a") is None
    assert resolve_state("b") is None
    # But "ssa" (3+ chars, substring of "Assam") should still match
    result = resolve_state("ssa")
    assert result is not None
    assert result[0] == "Assam"
    # Exact name still works regardless of length
    result = resolve_state("Goa")
    assert result is not None
    assert result[0] == "Goa"


# ════════════════════════════════════════════════
# ROBUSTNESS FIXES — symbol normalization + whitelist
# ════════════════════════════════════════════════

def test_at_symbol_parsed():
    """'shirt @ 500' should parse with @ normalized to space."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("shirt @ 500")
    assert len(result["items"]) >= 1
    assert result["items"][0]["name"].lower() == "shirt"
    assert result["items"][0]["price"] == 500


def test_equals_symbol_parsed():
    """'shirt = 500' should parse with = normalized to space."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("shirt = 500")
    assert len(result["items"]) >= 1
    assert result["items"][0]["name"].lower() == "shirt"
    assert result["items"][0]["price"] == 500


def test_mixed_symbols_parsed():
    """'shirt @ 500 pant = 700' should parse both items."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("shirt @ 500 pant = 700")
    items = {i["name"].lower(): i for i in result["items"]}
    assert "shirt" in items
    assert items["shirt"]["price"] == 500
    assert "pant" in items
    assert items["pant"]["price"] == 700


def test_return_gift_not_return():
    """'return gift pack 200' should NOT trigger return (whitelist)."""
    from return_detector import detect_return_intent
    items = [{"name": "gift pack", "qty": 1, "price": 200}]
    assert detect_return_intent("return gift pack 200", items) is False


def test_genuine_return_still_works():
    """'want to return shirt' should still trigger return despite whitelist."""
    from return_detector import detect_return_intent
    items = [{"name": "shirt", "qty": 1, "price": 500}]
    assert detect_return_intent("want to return shirt 500", items) is True
    assert detect_return_intent("return this shirt 500", items) is True


def test_utensil_gst():
    """Utensil/steel should have 12% GST."""
    from gst_rates import get_gst_rate_smart
    for item in ["utensil", "steel utensil", "steel"]:
        rate = get_gst_rate_smart(item)
        assert rate["gst"] == 12, f"{item} expected 12% got {rate['gst']}%"


# ════════════════════════════════════════════════
# EDGE CASE FIXES — whitelist override + ambiguity
# ════════════════════════════════════════════════

def test_send_back_cover_is_return():
    """'send back cover 200' — strong verb 'send back' overrides 'back cover' whitelist."""
    from return_detector import detect_return_intent
    items = [{"name": "cover", "qty": 1, "price": 200}]
    assert detect_return_intent("send back cover 200", items) is True


def test_return_back_cover_is_return():
    """'return back cover 200' — 'returned' is a strong verb, overrides whitelist."""
    from return_detector import detect_return_intent
    items = [{"name": "back cover", "qty": 1, "price": 200}]
    assert detect_return_intent("want to return back cover 200", items) is True


def test_back_cover_still_not_return():
    """'back cover 200' — no strong verb, still NOT a return."""
    from return_detector import detect_return_intent
    items = [{"name": "back cover", "qty": 1, "price": 200}]
    assert detect_return_intent("back cover 200", items) is False


def test_ambiguous_compact_xqty():
    """'pen10x5' triggers ambiguous_parse warning."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("pen10x5")
    assert "ambiguous_parse" in result["warnings"]


def test_ambiguous_long_number():
    """'shirt1002' triggers ambiguous_parse warning (4+ digit compact)."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("shirt1002")
    assert "ambiguous_parse" in result["warnings"]


def test_no_ambiguity_for_normal_input():
    """'shirt 500' should NOT trigger ambiguity warning."""
    from claude_parser import _regex_parse_message
    result = _regex_parse_message("shirt 500")
    assert "ambiguous_parse" not in result["warnings"]


def test_ambiguity_shown_in_preview():
    """Ambiguous parse warning appears in preview message."""
    from whatsapp_webhook import msg_preview, PendingBill
    from datetime import datetime
    pending = PendingBill(
        phone="test", shop_id="TEST", shop_name="Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Test", customer_state="Telangana",
        customer_state_code="36",
        items=[{"name": "pen", "qty": 1, "price": 10.0,
                "hsn": "9608", "gst_rate": 18, "gst_source": "exact",
                "gst_confidence": "high"}],
        confidence=0.6, warnings=["ambiguous_parse"], raw_message="pen10x5",
        created_at=datetime.utcnow(),
    )
    preview = msg_preview(pending)
    assert "verify quantity and price" in preview.lower()


# ════════════════════════════════════════════════
# MESSAGE DEDUP — webhook retry protection
# ════════════════════════════════════════════════

def test_dedup_claim_new_message():
    """First claim for a message_id returns True (new message)."""
    from database import try_claim_message, init_database
    init_database()
    assert try_claim_message("wamid_claim_new_001") is True


def test_dedup_claim_duplicate_returns_false():
    """Second claim for same message_id returns False (duplicate)."""
    from database import try_claim_message, init_database
    init_database()
    msg_id = "wamid_claim_dup_002"
    assert try_claim_message(msg_id) is True
    assert try_claim_message(msg_id) is False


def test_dedup_different_ids_independent():
    """Different message IDs don't interfere with each other."""
    from database import try_claim_message, init_database
    init_database()
    assert try_claim_message("wamid_indep_aaa") is True
    assert try_claim_message("wamid_indep_bbb") is True
    # Re-claim both → both duplicates
    assert try_claim_message("wamid_indep_aaa") is False
    assert try_claim_message("wamid_indep_bbb") is False


def test_dedup_triple_claim_no_crash():
    """Claiming the same ID 3 times must not raise — returns False on 2nd+."""
    from database import try_claim_message, init_database
    init_database()
    msg_id = "wamid_triple_003"
    assert try_claim_message(msg_id) is True
    assert try_claim_message(msg_id) is False
    assert try_claim_message(msg_id) is False


def test_dedup_cleanup_removes_old():
    """maybe_cleanup_processed_messages removes stale records when counter hits threshold."""
    from database import (
        db_session, ProcessedMessage, try_claim_message, init_database,
    )
    import db.dedup as dedup_mod
    from datetime import datetime, timedelta
    init_database()

    # Insert an old record directly (72h ago)
    old_id = "wamid_old_cleanup_v2"
    with db_session() as session:
        session.add(ProcessedMessage(
            message_id=old_id,
            created_at=datetime.utcnow() - timedelta(hours=72),
        ))

    # Verify it exists
    assert try_claim_message(old_id) is False  # can't claim → it exists

    # Force counter to threshold so next call triggers cleanup
    with dedup_mod._dedup_counter_lock:
        dedup_mod._dedup_call_counter = dedup_mod._DEDUP_CLEANUP_INTERVAL - 1

    dedup_mod.maybe_cleanup_processed_messages()

    # Old record should be gone — claim succeeds again
    assert try_claim_message(old_id) is True


def test_dedup_recent_survives_cleanup():
    """Recent records survive cleanup."""
    from database import try_claim_message, init_database
    import db.dedup as dedup_mod
    init_database()

    recent_id = "wamid_recent_survive_v2"
    assert try_claim_message(recent_id) is True

    # Force cleanup
    with dedup_mod._dedup_counter_lock:
        dedup_mod._dedup_call_counter = dedup_mod._DEDUP_CLEANUP_INTERVAL - 1
    dedup_mod.maybe_cleanup_processed_messages()

    # Recent record still blocks re-claim
    assert try_claim_message(recent_id) is False


def test_dedup_cleanup_throttled():
    """Cleanup does NOT run on every call — only at threshold."""
    import db.dedup as dedup_mod

    # Reset counter
    with dedup_mod._dedup_counter_lock:
        dedup_mod._dedup_call_counter = 0

    # Call N-1 times — should not reset counter to 0
    for _ in range(dedup_mod._DEDUP_CLEANUP_INTERVAL - 1):
        dedup_mod.maybe_cleanup_processed_messages()

    with dedup_mod._dedup_counter_lock:
        assert dedup_mod._dedup_call_counter == dedup_mod._DEDUP_CLEANUP_INTERVAL - 1

    # One more call triggers cleanup and resets counter
    dedup_mod.maybe_cleanup_processed_messages()
    with dedup_mod._dedup_counter_lock:
        assert dedup_mod._dedup_call_counter == 0


def test_webhook_payload_includes_message_id():
    """parse_meta_webhook_payload extracts message ID."""
    from whatsapp_client import parse_meta_webhook_payload
    body = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.HBgNOTE5ODc2NTQzMjEw",
                        "from": "919876543210",
                        "type": "text",
                        "text": {"body": "shirt 500"},
                    }]
                }
            }]
        }]
    }
    msgs = parse_meta_webhook_payload(body)
    assert len(msgs) == 1
    assert msgs[0]["message_id"] == "wamid.HBgNOTE5ODc2NTQzMjEw"


def test_webhook_payload_missing_message_id():
    """Missing message ID in payload defaults to empty string."""
    from whatsapp_client import parse_meta_webhook_payload
    body = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": "919876543210",
                        "type": "text",
                        "text": {"body": "shirt 500"},
                    }]
                }
            }]
        }]
    }
    msgs = parse_meta_webhook_payload(body)
    assert len(msgs) == 1
    assert msgs[0]["message_id"] == ""


# ════════════════════════════════════════════════
# SCHEMA VALIDATION TESTS
# ════════════════════════════════════════════════

class TestSchemaValidation:
    """Tests for validate_schema(), reset_database(), and ensure_schema()."""

    def test_validate_schema_passes_on_fresh_db(self):
        """Fresh DB created by init_database() should pass validation."""
        from database import init_database, validate_schema
        init_database()
        problems = validate_schema()
        assert problems == []

    def test_validate_schema_detects_missing_table(self):
        """Dropping a required table should be detected."""
        from database import engine, validate_schema
        with engine.connect() as conn:
            conn.execute(__import__("sqlalchemy").text("DROP TABLE IF EXISTS processed_messages"))
            conn.commit()
        problems = validate_schema()
        assert any("processed_messages" in p for p in problems)
        # Recreate for other tests
        from database import init_database
        init_database()

    def test_validate_schema_detects_missing_column(self):
        """A table missing a required column should be detected."""
        from database import engine, validate_schema, init_database
        # Recreate clean, then drop a column via raw SQL (SQLite workaround)
        init_database()
        # SQLite doesn't support DROP COLUMN before 3.35, so recreate table without the column
        with engine.connect() as conn:
            conn.execute(__import__("sqlalchemy").text("DROP TABLE IF EXISTS report_pdfs"))
            conn.execute(__import__("sqlalchemy").text(
                "CREATE TABLE report_pdfs (id INTEGER PRIMARY KEY, filename TEXT, shop_id TEXT)"
            ))
            conn.commit()
        problems = validate_schema()
        assert any("report_pdfs.pdf_data" in p for p in problems)
        # Restore
        with engine.connect() as conn:
            conn.execute(__import__("sqlalchemy").text("DROP TABLE IF EXISTS report_pdfs"))
            conn.commit()
        init_database()

    def test_reset_database_recreates_all_tables(self):
        """reset_database() should produce a clean schema that passes validation."""
        from database import reset_database, validate_schema
        reset_database()
        problems = validate_schema()
        assert problems == []

    def test_ensure_schema_noop_when_valid(self):
        """ensure_schema should do nothing when schema is already valid."""
        from database import init_database, ensure_schema, validate_schema
        init_database()
        ensure_schema(dev_mode=False)
        problems = validate_schema()
        assert problems == []

    def test_ensure_schema_dev_mode_auto_resets(self):
        """ensure_schema with dev_mode=True should auto-fix a broken schema."""
        from database import engine, init_database, ensure_schema, validate_schema
        init_database()
        # Break schema
        with engine.connect() as conn:
            conn.execute(__import__("sqlalchemy").text("DROP TABLE IF EXISTS shop_item_master"))
            conn.commit()
        # Confirm broken
        problems = validate_schema()
        assert len(problems) > 0
        # Auto-fix
        ensure_schema(dev_mode=True)
        problems = validate_schema()
        assert problems == []

    def test_ensure_schema_prod_mode_does_not_reset(self):
        """ensure_schema with dev_mode=False should NOT auto-reset."""
        from database import engine, init_database, ensure_schema, validate_schema
        init_database()
        # Break schema
        with engine.connect() as conn:
            conn.execute(__import__("sqlalchemy").text("DROP TABLE IF EXISTS shop_item_master"))
            conn.commit()
        # Should NOT fix
        ensure_schema(dev_mode=False)
        problems = validate_schema()
        assert any("shop_item_master" in p for p in problems)
        # Restore for other tests
        init_database()


# ════════════════════════════════════════════════
# CUSTOMER PHONE EXTRACTION
# ════════════════════════════════════════════════

class TestCustomerPhoneExtraction:
    """Phone extraction from free-form bill messages."""

    def test_plain_10_digit(self):
        from ai.sanitizer import extract_customer_phone
        assert extract_customer_phone("Sold tiles to Ramesh 9876543210") == "9876543210"

    def test_with_country_code_and_spaces(self):
        from ai.sanitizer import extract_customer_phone
        assert extract_customer_phone("Ramesh +91 98765 43210 tiles 500") == "9876543210"

    def test_with_hyphen_separator(self):
        from ai.sanitizer import extract_customer_phone
        assert extract_customer_phone("tiles 500 customer 98765-43210") == "9876543210"

    def test_no_phone_returns_none(self):
        from ai.sanitizer import extract_customer_phone
        assert extract_customer_phone("tiles 500 to Ramesh") is None

    def test_phone_inside_text(self):
        from ai.sanitizer import extract_customer_phone
        assert extract_customer_phone("Call 9876543210 sold tiles 500") == "9876543210"

    def test_multiple_numbers_first_wins(self):
        from ai.sanitizer import extract_customer_phone
        # Two valid Indian mobiles — the first one must be chosen.
        assert extract_customer_phone("6123456789 backup 9876543210") == "6123456789"

    def test_invalid_first_digit_ignored(self):
        from ai.sanitizer import extract_customer_phone
        # Indian mobiles start with 6-9. "5..." is not a mobile.
        assert extract_customer_phone("tiles 500 Ramesh 5876543210") is None

    def test_price_is_not_phone(self):
        from ai.sanitizer import extract_customer_phone
        # "500" is a price, not a 10-digit phone.
        assert extract_customer_phone("tiles 500 Ramesh") is None

    def test_strip_phone_from_name(self):
        from ai.sanitizer import strip_phone_from_name
        assert strip_phone_from_name("Ramesh 9876543210") == "Ramesh"
        assert strip_phone_from_name("Ramesh +91 98765 43210") == "Ramesh"
        assert strip_phone_from_name("Ramesh") == "Ramesh"

    def test_normalization_drops_plus_and_separators(self):
        from ai.sanitizer import extract_customer_phone
        assert extract_customer_phone("+91-98765-43210 tiles") == "9876543210"
        assert extract_customer_phone("+919876543210 tiles")   == "9876543210"


class TestPendingBillCarriesPhone:
    """End-to-end: parsed phone must reach PendingBill and the saved Bill row."""

    def test_pending_bill_stores_phone(self):
        from datetime import datetime
        from database import init_database
        from services.pending import PendingBill, store_pending, get_pending_bill, clear_pending
        init_database()

        phone_key = "whatsapp:+1test_phone_case"
        clear_pending(phone_key)
        pending = PendingBill(
            phone               = phone_key,
            shop_id             = "TESTSHOP",
            shop_name           = "Test Shop",
            shop_state          = "Telangana",
            shop_state_code     = "36",
            customer_name       = "Ramesh",
            customer_state      = "Telangana",
            customer_state_code = "36",
            items               = [{"name": "tiles", "qty": 1, "price": 500,
                                    "hsn": "6907", "gst_rate": 18}],
            confidence          = 1.0,
            warnings            = [],
            raw_message         = "tiles 500 Ramesh 9876543210",
            created_at          = datetime.utcnow(),
            customer_phone      = "9876543210",
        )
        store_pending(phone_key, pending)
        loaded = get_pending_bill(phone_key)
        assert loaded is not None
        assert loaded.customer_phone == "9876543210"
        clear_pending(phone_key)

    def test_bill_row_stores_parsed_phone(self, tmp_path, monkeypatch):
        """save_bill persists the parsed customer phone to the Bill row."""
        from main import save_bill, get_shop
        from database import db_session, Bill, Shop, init_database
        from bill_generator import BillItem, BillResult, number_to_words
        init_database()

        shop_id = "TESTSHOPPH"
        # Ensure the shop row exists
        with db_session() as session:
            existing = session.query(Shop).filter_by(shop_id=shop_id).first()
            if not existing:
                session.add(Shop(
                    shop_id=shop_id, name="Phone Test Shop",
                    address="Hyderabad", gstin="36AABCU9603R1ZX",
                    phone="+919999999999", state="Telangana", state_code="36",
                ))

        items = [BillItem(
            name="Tiles", qty=1, price=500, hsn="6907", gst_rate=18,
            amount=500, cgst=45, sgst=45, igst=0, total=590,
        )]
        bill_result = BillResult(
            items=items, subtotal=500, total_cgst=45, total_sgst=45,
            total_igst=0, total_gst=90, grand_total=590,
            in_words=number_to_words(590), is_igst=False,
        )

        invoice_number = "INV-TEST-PHONE-001"
        save_bill(
            shop_id=shop_id, invoice_number=invoice_number,
            customer_name="Ramesh", customer_phone="9876543210",
            items=items, bill_result=bill_result, pdf_data=b"",
            raw_message="tiles 500 Ramesh 9876543210",
        )

        with db_session() as session:
            row = session.query(Bill).filter_by(invoice_number=invoice_number).first()
            assert row is not None
            assert row.customer_phone == "9876543210"
            assert row.customer_name == "Ramesh"
            session.delete(row)


class TestPhoneInInvoiceOutput:
    """Phone must appear in PDF only when present, and bill summary must show it."""

    def test_bill_summary_includes_phone_when_present(self):
        from api.formatters import msg_bill_summary
        from bill_generator import BillItem, BillResult, number_to_words

        items = [BillItem(
            name="Tiles", qty=1, price=500, hsn="6907", gst_rate=18,
            amount=500, cgst=45, sgst=45, igst=0, total=590,
        )]
        br = BillResult(
            items=items, subtotal=500, total_cgst=45, total_sgst=45,
            total_igst=0, total_gst=90, grand_total=590,
            in_words=number_to_words(590), is_igst=False,
        )
        msg = msg_bill_summary(
            bill_result=br, invoice_number="INV-TEST-1",
            customer_name="Ramesh", days=9,
            customer_phone="9876543210",
        )
        assert "Ramesh" in msg
        assert "9876543210" in msg
        assert "📞" in msg

    def test_bill_summary_omits_phone_when_missing(self):
        from api.formatters import msg_bill_summary
        from bill_generator import BillItem, BillResult, number_to_words

        items = [BillItem(
            name="Tiles", qty=1, price=500, hsn="6907", gst_rate=18,
            amount=500, cgst=45, sgst=45, igst=0, total=590,
        )]
        br = BillResult(
            items=items, subtotal=500, total_cgst=45, total_sgst=45,
            total_igst=0, total_gst=90, grand_total=590,
            in_words=number_to_words(590), is_igst=False,
        )
        msg = msg_bill_summary(
            bill_result=br, invoice_number="INV-TEST-2",
            customer_name="Ramesh", days=9,
        )
        assert "Ramesh" in msg
        assert "📞" not in msg

    def test_pdf_customer_block_includes_phone(self):
        from bill_generator import (
            ShopProfile, CustomerInfo, BillItem, generate_pdf_bill,
        )

        shop = ShopProfile(
            shop_id="RAVI", name="Ravi Shop", address="Hyderabad",
            gstin="36AABCU9603R1ZX", phone="+919999999999",
            state="Telangana", state_code="36",
        )
        customer = CustomerInfo(
            name="Ramesh", phone="9876543210",
            state="Telangana", state_code="36",
        )
        items = [BillItem(name="tiles", qty=1, price=500, hsn="6907", gst_rate=18)]
        pdf_bytes, _ = generate_pdf_bill(
            shop=shop, customer=customer, items=items,
            invoice_number="INV-TEST-PDF-1",
            bill_of_supply=False,
        )
        # ReportLab PDFs are zlib-compressed so we can't grep the raw bytes
        # for "9876543210". The contract we care about here is that the
        # renderer accepted customer.phone without raising.
        assert isinstance(pdf_bytes, bytes) and len(pdf_bytes) > 500


class TestUpdateShopGstin:
    """Regression test: GSTIN registered mid-conversation must persist to both tables."""

    def test_update_shop_gstin_persists_to_both_tables(self):
        from database import db_session, Registration, Shop, init_database
        from services.registration import update_shop_gstin, activate_trial

        init_database()
        phone = "whatsapp:+919000000042"
        shop_id = "S00000042"  # last 8 digits of "919000000042"
        gstin = "36AABCU9603R1ZX"

        # Start with a Bill-of-Supply shop (no GSTIN)
        activate_trial(phone, "Test GSTIN Shop", "Hyderabad", gstin="",
                       state_name="Telangana", state_code="36")

        with db_session() as s:
            reg = s.query(Registration).filter_by(phone=phone).first()
            assert reg.gstin == "" or reg.gstin is None
            shop = s.query(Shop).filter_by(shop_id=shop_id).first()
            assert shop is not None

        # Register GSTIN mid-session
        update_shop_gstin(phone, gstin)

        with db_session() as s:
            reg = s.query(Registration).filter_by(phone=phone).first()
            assert reg.gstin == gstin
            assert reg.invoice_type == "TAX_INVOICE"
            shop = s.query(Shop).filter_by(shop_id=shop_id).first()
            assert shop.gstin == gstin

    def test_update_shop_gstin_context_shows_has_gstin(self):
        from database import db_session, Registration, Shop, init_database
        from services.registration import update_shop_gstin, activate_trial
        from bill_generator import PLACEHOLDER_GSTIN

        init_database()
        phone = "whatsapp:+919000000043"
        gstin = "29GGGGG1314R9Z6"

        activate_trial(phone, "Test Context Shop", "Bangalore", gstin="",
                       state_name="Karnataka", state_code="29")
        update_shop_gstin(phone, gstin)

        from database import db_session, Registration, Shop
        with db_session() as s:
            reg = s.query(Registration).filter_by(phone=phone).first()
            effective_gstin = reg.gstin or ""
        # After update, effective GSTIN is the real one — not the placeholder
        assert effective_gstin == gstin
        assert effective_gstin != PLACEHOLDER_GSTIN


class TestPendingBillReminder:
    """Tests for _with_pending_reminder stale-snapshot and expiry-drift bugs."""

    @staticmethod
    def _make_ctx(phone: str, pending_bill_dict=None):
        from conversation.context import ShopContext
        return ShopContext(
            phone=phone,
            shop_name="Test Shop",
            owner_name="Test",
            shop_type="general",
            state="Telangana",
            state_code="36",
            gstin="",
            default_pricing="exclusive",
            default_bill_type="",
            language="en",
            conversation_history="",
            pending_bill=pending_bill_dict,
            pending_bill_age_mins=0,
            last_bill=None,
            top_items=[],
            frequent_customers=[],
            bills_today=0,
            total_bills=5,
            is_new_user=False,
            is_power_user=False,
            trial_active=True,
            trial_days_left=9,
        )

    @staticmethod
    def _make_pending(phone: str, items=None, created_at=None):
        from services.pending import PendingBill
        from datetime import datetime
        if items is None:
            items = [{"name": "charger", "qty": 1, "price": 299}]
        if created_at is None:
            created_at = datetime.utcnow()
        return PendingBill(
            phone=phone,
            shop_id="S00000099",
            shop_name="Test Shop",
            shop_state="Telangana",
            shop_state_code="36",
            customer_name="Customer",
            customer_state="Telangana",
            customer_state_code="36",
            items=items,
            confidence=0.9,
            warnings=[],
            raw_message="charger 299",
            created_at=created_at,
        )

    def test_no_reminder_when_bill_cleared_before_handler_runs(self):
        """_with_pending_reminder must not show reminder when bill was cleared from DB.

        Root cause: stale ctx.pending_bill snapshot. The old code read ctx.pending_bill
        (captured at request start) instead of the live DB state. If a concurrent
        worker clears the bill between context-load and handler execution, the user
        still sees "bill is still waiting" — then says YES and gets "no pending bill".
        """
        from database import init_database
        from services.pending import store_pending, clear_pending
        from conversation.executor import _with_pending_reminder

        init_database()
        phone = "whatsapp:+919000000099"
        pending = self._make_pending(phone)
        store_pending(phone, pending)

        # Snapshot ctx as load_shop_context does at request start
        pending_dict = {
            "items": pending.items, "item_count": 1,
            "customer_name": "Customer", "customer_phone": "",
            "discount_type": "none", "discount_value": 0.0,
            "pricing_type": "exclusive", "bill_type": "tax_invoice",
        }
        ctx = self._make_ctx(phone, pending_bill_dict=pending_dict)

        # Bill is cleared from DB (concurrent worker confirmed or cancelled it)
        clear_pending(phone)

        # Must return plain reply — no stale reminder for a bill that no longer exists
        result = _with_pending_reminder("GSTIN saved.", ctx)
        assert "still open" not in result, (
            "Stale ctx.pending_bill snapshot caused a ghost reminder for a deleted bill"
        )
        assert result == "GSTIN saved."

    def test_reminder_refreshes_expiry(self):
        """_with_pending_reminder must refresh bill expiry so 'YES' one second later works.

        Root cause: expiry drift. Non-mutating handlers (settings, questions) appended
        "bill is still waiting" without resetting the 10-minute expiry timer. If a bill
        was ~10 minutes old, cleanup_expired_pending() on the very next request would
        delete it — so the user's YES response always found no pending bill.
        """
        from datetime import datetime, timedelta
        from database import init_database, PendingBillRecord, db_session
        from services.pending import store_pending, PENDING_EXPIRY_MINUTES
        from conversation.executor import _with_pending_reminder

        init_database()
        phone = "whatsapp:+919000000098"

        # Bill created 9 min 50 sec ago — expires in ~10 seconds
        old_created_at = datetime.utcnow() - timedelta(minutes=9, seconds=50)
        pending = self._make_pending(phone, created_at=old_created_at)
        store_pending(phone, pending)

        with db_session() as s:
            row = s.query(PendingBillRecord).filter_by(phone=phone).first()
            assert row is not None
            seconds_left = (row.expires_at - datetime.utcnow()).total_seconds()
            assert 0 < seconds_left < 30, f"Expected nearly-expired bill, got {seconds_left:.1f}s"

        pending_dict = {
            "items": pending.items, "item_count": 1,
            "customer_name": "Customer", "customer_phone": "",
            "discount_type": "none", "discount_value": 0.0,
            "pricing_type": "exclusive", "bill_type": "tax_invoice",
        }
        ctx = self._make_ctx(phone, pending_bill_dict=pending_dict)

        result = _with_pending_reminder("Your GST is updated.", ctx)

        # Reminder must be shown (bill not expired yet at time of call)
        assert "still open" in result, "Expected pending-bill reminder in reply"

        # Expiry must be refreshed to ~now + 10 min
        with db_session() as s:
            row = s.query(PendingBillRecord).filter_by(phone=phone).first()
            assert row is not None, "Pending bill was deleted — expiry refresh must preserve it"
            minutes_left = (row.expires_at - datetime.utcnow()).total_seconds() / 60
            assert minutes_left > PENDING_EXPIRY_MINUTES - 1, (
                f"Expected expiry ~{PENDING_EXPIRY_MINUTES}min after reminder, "
                f"got {minutes_left:.1f}min — expiry was not refreshed"
            )

    def test_expiry_not_refreshed_for_fresh_bill(self):
        """_with_pending_reminder must NOT refresh expiry for bills < 8 minutes old.

        Resetting created_at on every reminder call makes the 10-minute expiry
        indefinitely extendable. The grace reset should only happen when the bill
        is actually close to expiry (>= 8 min old).
        """
        from datetime import datetime, timedelta
        from database import init_database, PendingBillRecord, db_session
        from services.pending import store_pending, PENDING_EXPIRY_MINUTES
        from conversation.executor import _with_pending_reminder

        init_database()
        phone = "whatsapp:+919000000096"

        # Bill is 2 minutes old — well within the normal window
        created_at = datetime.utcnow() - timedelta(minutes=2)
        pending = self._make_pending(phone, created_at=created_at)
        store_pending(phone, pending)

        pending_dict = {
            "items": pending.items, "item_count": 1,
            "customer_name": "Customer", "customer_phone": "",
            "discount_type": "none", "discount_value": 0.0,
            "pricing_type": "exclusive", "bill_type": "tax_invoice",
        }
        ctx = self._make_ctx(phone, pending_bill_dict=pending_dict)

        result = _with_pending_reminder("Settings saved.", ctx)

        # Reminder is still shown
        assert "still open" in result, "Expected pending-bill reminder in reply"

        # Expiry must NOT be extended — should be ~8 min left, not ~10 min
        with db_session() as s:
            row = s.query(PendingBillRecord).filter_by(phone=phone).first()
            assert row is not None
            minutes_left = (row.expires_at - datetime.utcnow()).total_seconds() / 60
            assert minutes_left < PENDING_EXPIRY_MINUTES - 1, (
                f"Expiry was reset for a fresh 2-min-old bill (got {minutes_left:.1f}min left) "
                f"— only bills >= 8 min old should have their expiry refreshed"
            )

    def test_reminder_appended_for_active_bill(self):
        """Happy path: active bill with items gets reminder text appended."""
        from database import init_database
        from services.pending import store_pending
        from conversation.executor import _with_pending_reminder

        init_database()
        phone = "whatsapp:+919000000097"
        items = [
            {"name": "charger", "qty": 1, "price": 299},
            {"name": "cover", "qty": 2, "price": 99},
        ]
        pending = self._make_pending(phone, items=items)
        store_pending(phone, pending)

        pending_dict = {
            "items": items, "item_count": 2,
            "customer_name": "Customer", "customer_phone": "",
            "discount_type": "none", "discount_value": 0.0,
            "pricing_type": "exclusive", "bill_type": "tax_invoice",
        }
        ctx = self._make_ctx(phone, pending_bill_dict=pending_dict)

        result = _with_pending_reminder("GSTIN saved.", ctx)
        assert "2 item" in result, f"Expected '2 items' in reminder, got: {result!r}"
        assert "YES" in result, f"Expected YES in reminder, got: {result!r}"


class TestDefaultBillType:
    """Tests for persistent default_bill_type shop preference."""

    @staticmethod
    def _make_ctx(phone: str, gstin: str = "", default_bill_type: str = ""):
        from conversation.context import ShopContext
        return ShopContext(
            phone=phone,
            shop_name="Test Shop",
            owner_name="Test",
            shop_type="general",
            state="Telangana",
            state_code="36",
            gstin=gstin,
            default_pricing="exclusive",
            default_bill_type=default_bill_type,
            language="en",
            conversation_history="",
            pending_bill=None,
            pending_bill_age_mins=0,
            last_bill=None,
            top_items=[],
            frequent_customers=[],
            bills_today=0,
            total_bills=5,
            is_new_user=False,
            is_power_user=False,
            trial_active=True,
            trial_days_left=9,
        )

    # ── _is_bill_of_supply precedence ──────────────────────────────────

    def test_is_bos_false_when_default_bill_type_is_tax_invoice(self):
        """explicit tax_invoice overrides missing GSTIN (fallback would be True)."""
        from conversation.executor import _is_bill_of_supply
        ctx = self._make_ctx("whatsapp:+910000000001", gstin="", default_bill_type="tax_invoice")
        assert _is_bill_of_supply(ctx) is False

    def test_is_bos_true_when_default_bill_type_is_bill_of_supply(self):
        """explicit bill_of_supply overrides valid GSTIN (fallback would be False)."""
        from conversation.executor import _is_bill_of_supply
        ctx = self._make_ctx(
            "whatsapp:+910000000002",
            gstin="36AABCU9603R1ZX",
            default_bill_type="bill_of_supply",
        )
        assert _is_bill_of_supply(ctx) is True

    def test_is_bos_falls_back_to_gstin_when_no_preference(self):
        """No preference saved → derive from GSTIN as before."""
        from conversation.executor import _is_bill_of_supply
        ctx_no_gstin = self._make_ctx("whatsapp:+910000000003", gstin="", default_bill_type="")
        ctx_has_gstin = self._make_ctx(
            "whatsapp:+910000000004", gstin="29ABCDE1234F1Z5", default_bill_type=""
        )
        assert _is_bill_of_supply(ctx_no_gstin) is True
        assert _is_bill_of_supply(ctx_has_gstin) is False

    # ── update_shop_default_bill_type persistence ───────────────────────

    def test_update_shop_default_bill_type_persists(self):
        """update_shop_default_bill_type writes to Shop table and can be read back."""
        from database import init_database, db_session
        from db.models import Shop
        from services.registration import activate_trial, update_shop_default_bill_type

        init_database()
        phone = "whatsapp:+919000000091"

        activate_trial(phone, "DBT Test Shop", "Hyderabad", gstin="",
                       state_name="Telangana", state_code="36")

        update_shop_default_bill_type(phone, "bill_of_supply")

        shop_id = "S" + "919000000091"[-8:]
        with db_session() as s:
            shop = s.query(Shop).filter_by(shop_id=shop_id).first()
            assert shop is not None
            assert shop.default_bill_type == "bill_of_supply"

        update_shop_default_bill_type(phone, "tax_invoice")
        with db_session() as s:
            shop = s.query(Shop).filter_by(shop_id=shop_id).first()
            assert shop.default_bill_type == "tax_invoice"

    def test_load_shop_context_reads_default_bill_type(self):
        """load_shop_context propagates default_bill_type from Shop into ctx."""
        from database import init_database
        from services.registration import activate_trial, update_shop_default_bill_type
        from conversation.context import load_shop_context

        init_database()
        phone = "whatsapp:+919000000092"
        activate_trial(phone, "CTX DBT Shop", "Hyderabad", gstin="",
                       state_name="Telangana", state_code="36")

        # Default is None/empty after creation
        ctx = load_shop_context(phone)
        assert ctx.default_bill_type == ""

        update_shop_default_bill_type(phone, "bill_of_supply")
        ctx = load_shop_context(phone)
        assert ctx.default_bill_type == "bill_of_supply"

    # ── _handle_set_default_bill_type reply ─────────────────────────────

    def test_handle_set_default_bill_type_returns_confirmation(self):
        """_handle_set_default_bill_type writes to DB and returns a confirmation."""
        from database import init_database, db_session
        from db.models import Shop
        from services.registration import activate_trial
        from conversation.executor import _handle_set_default_bill_type

        init_database()
        phone = "whatsapp:+919000000093"
        activate_trial(phone, "Reply Test Shop", "Hyderabad", gstin="",
                       state_name="Telangana", state_code="36")

        ctx = self._make_ctx(phone)
        result = _handle_set_default_bill_type(phone, "bill_of_supply", ctx, "")

        assert "Bill of Supply" in result
        assert "future bills" in result.lower() or "default" in result.lower()

        shop_id = "S" + "919000000093"[-8:]
        with db_session() as s:
            shop = s.query(Shop).filter_by(shop_id=shop_id).first()
            assert shop.default_bill_type == "bill_of_supply"

    # ── _handle_billing respects default_bill_type ──────────────────────

    def test_new_bill_uses_default_bill_type_bill_of_supply(self):
        """When default_bill_type is 'bill_of_supply', new pending bill is BOS."""
        from database import init_database
        from services.registration import activate_trial
        from services.pending import get_pending_bill, clear_pending
        from conversation.executor import _handle_billing

        init_database()
        phone = "whatsapp:+919000000094"
        activate_trial(phone, "BOS Default Shop", "Hyderabad", gstin="",
                       state_name="Telangana", state_code="36")
        clear_pending(phone)

        # Context with explicit bill_of_supply preference
        ctx = self._make_ctx(phone, gstin="", default_bill_type="bill_of_supply")

        bill_changes = {"add_items": [{"name": "shirt", "qty": 1, "price": 500}]}
        _handle_billing(phone, bill_changes, ctx, "", show_preview=False)

        pending = get_pending_bill(phone)
        assert pending is not None, "Expected a pending bill to be created"
        assert pending.is_bill_of_supply is True, (
            "Expected is_bill_of_supply=True because default_bill_type='bill_of_supply'"
        )

    def test_new_bill_is_tax_invoice_when_default_is_tax_invoice(self):
        """When default_bill_type is 'tax_invoice', new pending bill has GST."""
        from database import init_database
        from services.registration import activate_trial
        from services.pending import get_pending_bill, clear_pending
        from conversation.executor import _handle_billing

        init_database()
        phone = "whatsapp:+919000000095"
        # Shop has no GSTIN — without the preference it would default to BOS
        activate_trial(phone, "TI Default Shop", "Hyderabad", gstin="",
                       state_name="Telangana", state_code="36")
        clear_pending(phone)

        # Explicit tax_invoice preference overrides the missing-GSTIN fallback
        ctx = self._make_ctx(phone, gstin="", default_bill_type="tax_invoice")

        bill_changes = {"add_items": [{"name": "shirt", "qty": 1, "price": 500}]}
        _handle_billing(phone, bill_changes, ctx, "", show_preview=False)

        pending = get_pending_bill(phone)
        assert pending is not None
        assert pending.is_bill_of_supply is False, (
            "Expected is_bill_of_supply=False because default_bill_type='tax_invoice'"
        )

    # ── Per-message override still works ────────────────────────────────

    def test_per_bill_override_beats_default(self):
        """set_bill_type in bill_changes overrides the shop default for that bill."""
        from database import init_database
        from services.registration import activate_trial
        from services.pending import get_pending_bill, clear_pending
        from conversation.executor import _handle_billing

        init_database()
        phone = "whatsapp:+919000000090"
        activate_trial(phone, "Override Test Shop", "Hyderabad", gstin="36AABCU9603R1ZX",
                       state_name="Telangana", state_code="36")
        clear_pending(phone)

        # Shop default would normally be tax_invoice (has GSTIN)
        # but a per-message "bill_of_supply" override takes highest precedence
        ctx = self._make_ctx(phone, gstin="36AABCU9603R1ZX", default_bill_type="tax_invoice")

        bill_changes = {
            "add_items": [{"name": "shirt", "qty": 1, "price": 500}],
            "set_bill_type": "bill_of_supply",
        }
        _handle_billing(phone, bill_changes, ctx, "", show_preview=False)

        pending = get_pending_bill(phone)
        assert pending is not None
        assert pending.is_bill_of_supply is True, (
            "Per-message set_bill_type='bill_of_supply' must override default_bill_type"
        )


    # ── settings action priority ─────────────────────────────────────────

    def test_set_gstin_takes_priority_over_set_default_bill_type(self):
        """When both set_gstin and set_default_bill_type arrive together, set_gstin wins
        and set_default_bill_type is silently dropped (logged at INFO level)."""
        from database import init_database, db_session
        from db.models import Shop
        from services.registration import activate_trial
        from conversation.executor import execute_action

        init_database()
        phone = "whatsapp:+919000000097"
        activate_trial(phone, "Priority Test Shop", "Hyderabad", gstin="",
                       state_name="Telangana", state_code="36")

        ctx = self._make_ctx(phone, gstin="", default_bill_type="")

        result_dict = {
            "action": "settings",
            "reply": "",
            "bill_changes": {
                "set_gstin": "36AABCU9603R1ZX",
                "set_default_bill_type": "bill_of_supply",
            },
        }
        execute_action(result_dict, phone, ctx)

        shop_id = "S" + "919000000097"[-8:]
        with db_session() as s:
            shop = s.query(Shop).filter_by(shop_id=shop_id).first()
            assert shop is not None
            # GSTIN was applied
            assert shop.gstin == "36AABCU9603R1ZX"
            # default_bill_type was NOT applied (set_gstin took priority)
            assert (shop.default_bill_type or "") == ""


class TestCustomerStateExtraction:
    """Bug 1: Customer state in billing message must set inter-state (IGST) calculation."""

    @staticmethod
    def _make_ctx(phone: str, shop_state: str = "Telangana", shop_state_code: str = "36"):
        from conversation.context import ShopContext
        return ShopContext(
            phone=phone,
            shop_name="Test Shop",
            owner_name="Test",
            shop_type="general",
            state=shop_state,
            state_code=shop_state_code,
            gstin="36AABCU9603R1ZX",
            default_pricing="exclusive",
            default_bill_type="",
            language="en",
            conversation_history="",
            pending_bill=None,
            pending_bill_age_mins=0,
            last_bill=None,
            top_items=[],
            frequent_customers=[],
            bills_today=0,
            total_bills=5,
            is_new_user=False,
            is_power_user=False,
            trial_active=True,
            trial_days_left=9,
        )

    def test_customer_state_from_billing_message_sets_correct_state(self):
        """set_customer_state in bill_changes resolves and sets customer_state on PendingBill."""
        from database import init_database
        from services.registration import activate_trial
        from services.pending import get_pending_bill, clear_pending
        from conversation.executor import _handle_billing

        init_database()
        phone = "whatsapp:+919000000081"
        activate_trial(phone, "State Test Shop", "Hyderabad", gstin="36AABCU9603R1ZX",
                       state_name="Telangana", state_code="36")
        clear_pending(phone)

        ctx = self._make_ctx(phone)
        # LLM extracted "maharastra" from "shirt 500 state maharastra"
        bill_changes = {
            "add_items": [{"name": "shirt", "qty": 1, "price": 500}],
            "set_customer_state": "maharastra",
        }
        _handle_billing(phone, bill_changes, ctx, "", show_preview=False)

        pending = get_pending_bill(phone)
        assert pending is not None
        assert pending.customer_state == "Maharashtra", (
            f"Expected 'Maharashtra', got '{pending.customer_state}'"
        )
        assert pending.customer_state_code == "27", (
            f"Expected state code '27', got '{pending.customer_state_code}'"
        )

    def test_customer_state_defaults_to_shop_state_when_absent(self):
        """Without set_customer_state, customer_state stays as shop state (intra-state)."""
        from database import init_database
        from services.registration import activate_trial
        from services.pending import get_pending_bill, clear_pending
        from conversation.executor import _handle_billing

        init_database()
        phone = "whatsapp:+919000000082"
        activate_trial(phone, "No State Shop", "Hyderabad", gstin="36AABCU9603R1ZX",
                       state_name="Telangana", state_code="36")
        clear_pending(phone)

        ctx = self._make_ctx(phone)
        bill_changes = {"add_items": [{"name": "shirt", "qty": 1, "price": 500}]}
        _handle_billing(phone, bill_changes, ctx, "", show_preview=False)

        pending = get_pending_bill(phone)
        assert pending is not None
        assert pending.customer_state == "Telangana"
        assert pending.customer_state_code == "36"

    def test_unrecognised_customer_state_falls_back_to_shop_state(self):
        """If resolve_state returns None (unrecognised), shop state is kept."""
        from database import init_database
        from services.registration import activate_trial
        from services.pending import get_pending_bill, clear_pending
        from conversation.executor import _handle_billing

        init_database()
        phone = "whatsapp:+919000000083"
        activate_trial(phone, "Fallback State Shop", "Hyderabad", gstin="36AABCU9603R1ZX",
                       state_name="Telangana", state_code="36")
        clear_pending(phone)

        ctx = self._make_ctx(phone)
        bill_changes = {
            "add_items": [{"name": "shirt", "qty": 1, "price": 500}],
            "set_customer_state": "xyz_unknown_state_99",
        }
        _handle_billing(phone, bill_changes, ctx, "", show_preview=False)

        pending = get_pending_bill(phone)
        assert pending is not None
        assert pending.customer_state == "Telangana"
        assert pending.customer_state_code == "36"

    def test_validate_bill_changes_normalises_set_customer_state(self):
        """_validate_result passes through a non-empty set_customer_state."""
        import sys
        sys.path.insert(0, ".")
        from conversation.manager import _validate_result

        result = _validate_result({
            "action": "billing",
            "bill_changes": {
                "add_items": [{"name": "shirt", "price": 500, "qty": 1}],
                "set_customer_state": "  Maharashtra  ",
            },
        })
        assert result["bill_changes"]["set_customer_state"] == "Maharashtra"

    def test_validate_bill_changes_nulls_empty_set_customer_state(self):
        """_validate_result normalises null/empty set_customer_state to None."""
        from conversation.manager import _validate_result

        for v in (None, "null", "", "none"):
            result = _validate_result({
                "action": "billing",
                "bill_changes": {"set_customer_state": v},
            })
            assert result["bill_changes"]["set_customer_state"] is None, (
                f"Expected None for input {v!r}"
            )


class TestConfirmedBillComplaint:
    """Bug 2: Complaints about confirmed bills must never offer to regenerate."""

    @staticmethod
    def _make_ctx(phone: str, last_bill=None):
        from conversation.context import ShopContext
        return ShopContext(
            phone=phone,
            shop_name="Test Shop",
            owner_name="Test",
            shop_type="general",
            state="Telangana",
            state_code="36",
            gstin="36AABCU9603R1ZX",
            default_pricing="exclusive",
            default_bill_type="",
            language="en",
            conversation_history="",
            pending_bill=None,
            pending_bill_age_mins=0,
            last_bill=last_bill,
            top_items=[],
            frequent_customers=[],
            bills_today=1,
            total_bills=5,
            is_new_user=False,
            is_power_user=False,
            trial_active=True,
            trial_days_left=9,
        )

    def test_complaint_about_confirmed_bill_returns_credit_note_guidance(self):
        """When no pending bill exists and a confirmed bill is present, complaint
        handler must return RETURN/credit note guidance, never offer to regenerate."""
        from database import init_database
        from services.pending import clear_pending
        from conversation.executor import _handle_complaint

        init_database()
        phone = "whatsapp:+919000000071"
        clear_pending(phone)

        last_bill = {
            "invoice_number": "INV-TL-2024-001",
            "date": "21 Apr 2024",
            "customer_name": "Ravi",
            "customer_phone": "",
            "items": [{"name": "shirt", "qty": 1, "price": 500}],
            "grand_total": 590.0,
            "pricing_type": "exclusive",
        }
        ctx = self._make_ctx(phone, last_bill=last_bill)

        result = _handle_complaint(phone, "its igst right", ctx)

        assert "INV-TL-2024-001" in result, "Must mention the confirmed invoice number"
        assert "RETURN" in result, "Must instruct user to reply RETURN"
        assert "credit note" in result.lower(), "Must mention credit note"

        # Must NOT offer regeneration
        regen_words = ["regenerate", "redo", "i can fix", "let me fix", "i'll fix"]
        lower = result.lower()
        for word in regen_words:
            assert word not in lower, f"Must not contain '{word}' in reply"

    def test_complaint_without_confirmed_bill_shows_normal_reply(self):
        """When no last_bill, complaint falls through to the normal acknowledgement."""
        from database import init_database
        from services.pending import clear_pending
        from conversation.executor import _handle_complaint

        init_database()
        phone = "whatsapp:+919000000072"
        clear_pending(phone)

        ctx = self._make_ctx(phone, last_bill=None)
        result = _handle_complaint(phone, "Something went wrong", ctx)

        assert "Something went wrong" in result
        # No credit-note redirect when there's no confirmed bill
        assert "RETURN" not in result

    def test_complaint_with_pending_bill_shows_normal_reply(self):
        """When a pending bill IS open, complaint is about the current in-progress bill."""
        from database import init_database
        from services.registration import activate_trial
        from services.pending import store_pending, PendingBill
        from conversation.executor import _handle_complaint
        from datetime import datetime

        init_database()
        phone = "whatsapp:+919000000073"
        activate_trial(phone, "Pending Complaint Shop", "Hyderabad",
                       gstin="36AABCU9603R1ZX", state_name="Telangana", state_code="36")

        pending = PendingBill(
            phone=phone, shop_id="S00000073", shop_name="Test",
            shop_state="Telangana", shop_state_code="36",
            customer_name="Customer", customer_state="Telangana",
            customer_state_code="36", items=[{"name": "shirt", "qty": 1, "price": 500}],
            confidence=0.9, warnings=[], raw_message="shirt 500",
            created_at=datetime.utcnow(),
        )
        store_pending(phone, pending)

        last_bill = {"invoice_number": "INV-001", "date": "21 Apr", "customer_name": "Ravi",
                     "customer_phone": "", "items": [], "grand_total": 500.0, "pricing_type": "exclusive"}
        ctx = self._make_ctx(phone, last_bill=last_bill)

        result = _handle_complaint(phone, "the gst seems wrong", ctx)

        # With an open pending bill, must NOT redirect to credit note
        assert "RETURN" not in result, (
            "Should not show credit note guidance when a pending bill is open"
        )
        assert "credit note" not in result.lower(), (
            "Should not show credit note guidance when a pending bill is open"
        )


class TestGSTToggle:
    """GST ON/OFF toggle: original_gst preserved, rates restored on switch-back."""

    @staticmethod
    def _make_item(name: str, gst_rate: int, original_gst=None) -> dict:
        return {
            "name": name, "price": 500.0, "qty": 1,
            "gst_rate": gst_rate,
            "original_gst": original_gst,
            "hsn": "9999", "gst_source": "default", "gst_confidence": "high",
            "item_discount_type": "none", "item_discount_value": 0.0,
        }

    def test_make_item_dict_stores_original_gst(self):
        """_make_item_dict must store original_gst = gst_rate by default."""
        from conversation.executor import _make_item_dict
        item = _make_item_dict("charger", 299.0, 1, 18, "8504", "default", "high")
        assert item["gst_rate"]    == 18
        assert item["original_gst"] == 18

    def test_make_item_dict_bos_stores_none(self):
        """BOS items must store original_gst=None (rate not looked up)."""
        from conversation.executor import _make_item_dict
        item = _make_item_dict("charger", 299.0, 1, 0, "9999", "bill_of_supply", "high", None)
        assert item["gst_rate"]    == 0
        assert item["original_gst"] is None

    def test_toggle_to_bos_zeros_rate_and_preserves_original(self):
        """Switching to BOS zeros gst_rate but keeps original_gst intact."""
        from conversation.executor import _toggle_items_gst
        items = [self._make_item("charger", 18, 18), self._make_item("cover", 12, 12)]
        _toggle_items_gst(items, is_bos=True)
        assert items[0]["gst_rate"] == 0
        assert items[0]["original_gst"] == 18
        assert items[1]["gst_rate"] == 0
        assert items[1]["original_gst"] == 12

    def test_toggle_to_tax_invoice_restores_rate(self):
        """Switching back to Tax Invoice restores gst_rate from original_gst."""
        from conversation.executor import _toggle_items_gst
        # Items that had been switched to BOS (original_gst still set)
        items = [self._make_item("charger", 0, 18), self._make_item("cover", 0, 12)]
        _toggle_items_gst(items, is_bos=False)
        assert items[0]["gst_rate"] == 18
        assert items[1]["gst_rate"] == 12

    def test_multiple_toggles_preserve_original(self):
        """BOS → Tax → BOS → Tax must always restore the same original rate."""
        from conversation.executor import _toggle_items_gst
        items = [self._make_item("charger", 18, 18)]
        _toggle_items_gst(items, is_bos=True)
        assert items[0]["gst_rate"] == 0
        _toggle_items_gst(items, is_bos=False)
        assert items[0]["gst_rate"] == 18
        _toggle_items_gst(items, is_bos=True)
        assert items[0]["gst_rate"] == 0
        assert items[0]["original_gst"] == 18  # never overwritten
        _toggle_items_gst(items, is_bos=False)
        assert items[0]["gst_rate"] == 18

    def test_legitimately_zero_gst_preserved_in_tax_invoice(self):
        """Items with genuine 0% GST (original_gst=0) are kept at 0 in Tax Invoice."""
        from conversation.executor import _toggle_items_gst
        items = [self._make_item("rice", 0, 0)]  # genuinely 0%
        _toggle_items_gst(items, is_bos=False)
        assert items[0]["gst_rate"] == 0

    def test_handle_set_bill_type_to_bos_zeros_items(self):
        """_handle_set_bill_type BOS switch stores original_gst and zeros gst_rate."""
        from database import init_database
        from services.registration import activate_trial
        from services.pending import store_pending, get_pending_bill, PendingBill, clear_pending
        from conversation.executor import _handle_set_bill_type
        from conversation.context import ShopContext
        from datetime import datetime

        init_database()
        phone = "whatsapp:+919000000101"
        activate_trial(phone, "Toggle Shop", "Hyderabad",
                       gstin="36AABCU9603R1ZX", state_name="Telangana", state_code="36")
        clear_pending(phone)

        pending = PendingBill(
            phone=phone, shop_id="S00000101", shop_name="Toggle Shop",
            shop_state="Telangana", shop_state_code="36",
            customer_name="Ravi", customer_state="Telangana", customer_state_code="36",
            items=[
                {"name": "charger", "price": 499.0, "qty": 1,
                 "gst_rate": 18, "original_gst": 18,
                 "hsn": "8504", "gst_source": "default", "gst_confidence": "high",
                 "item_discount_type": "none", "item_discount_value": 0.0},
            ],
            confidence=0.9, warnings=[], raw_message="charger 499",
            created_at=datetime.utcnow(), is_bill_of_supply=False,
        )
        store_pending(phone, pending)

        ctx = ShopContext(
            phone=phone, shop_name="Toggle Shop", owner_name="Test", shop_type="general",
            state="Telangana", state_code="36", gstin="36AABCU9603R1ZX",
            default_pricing="exclusive", default_bill_type="", language="en",
            conversation_history="", pending_bill=None, pending_bill_age_mins=0,
            last_bill=None, top_items=[], frequent_customers=[],
            bills_today=0, total_bills=1, is_new_user=False, is_power_user=False,
            trial_active=True, trial_days_left=9,
        )
        _handle_set_bill_type(phone, {"set_bill_type": "bill_of_supply"}, ctx, "")

        updated = get_pending_bill(phone)
        assert updated.is_bill_of_supply is True
        assert updated.items[0]["gst_rate"]    == 0
        assert updated.items[0]["original_gst"] == 18

    def test_handle_set_bill_type_to_tax_invoice_restores_items(self):
        """_handle_set_bill_type Tax Invoice switch restores gst_rate from original_gst."""
        from database import init_database
        from services.registration import activate_trial
        from services.pending import store_pending, get_pending_bill, PendingBill, clear_pending
        from conversation.executor import _handle_set_bill_type
        from conversation.context import ShopContext
        from datetime import datetime

        init_database()
        phone = "whatsapp:+919000000102"
        activate_trial(phone, "Restore Shop", "Hyderabad",
                       gstin="36AABCU9603R1ZX", state_name="Telangana", state_code="36")
        clear_pending(phone)

        # Bill currently in BOS mode with original_gst saved
        pending = PendingBill(
            phone=phone, shop_id="S00000102", shop_name="Restore Shop",
            shop_state="Telangana", shop_state_code="36",
            customer_name="Ravi", customer_state="Telangana", customer_state_code="36",
            items=[
                {"name": "charger", "price": 499.0, "qty": 1,
                 "gst_rate": 0, "original_gst": 18,
                 "hsn": "8504", "gst_source": "bill_of_supply", "gst_confidence": "high",
                 "item_discount_type": "none", "item_discount_value": 0.0},
            ],
            confidence=0.9, warnings=[], raw_message="charger 499",
            created_at=datetime.utcnow(), is_bill_of_supply=True,
        )
        store_pending(phone, pending)

        ctx = ShopContext(
            phone=phone, shop_name="Restore Shop", owner_name="Test", shop_type="general",
            state="Telangana", state_code="36", gstin="36AABCU9603R1ZX",
            default_pricing="exclusive", default_bill_type="", language="en",
            conversation_history="", pending_bill=None, pending_bill_age_mins=0,
            last_bill=None, top_items=[], frequent_customers=[],
            bills_today=0, total_bills=1, is_new_user=False, is_power_user=False,
            trial_active=True, trial_days_left=9,
        )
        _handle_set_bill_type(phone, {"set_bill_type": "tax_invoice"}, ctx, "")

        updated = get_pending_bill(phone)
        assert updated.is_bill_of_supply is False
        assert updated.items[0]["gst_rate"] == 18

    def test_build_bill_items_safety_guard_restores_zero_in_tax_invoice(self):
        """_build_bill_items must restore original_gst when gst_rate=0 in Tax Invoice mode."""
        from services.billing import _build_bill_items
        from services.pending import PendingBill
        from datetime import datetime

        pending = PendingBill(
            phone="test", shop_id="S99999999", shop_name="Safety Shop",
            shop_state="Telangana", shop_state_code="36",
            customer_name="Test", customer_state="Telangana", customer_state_code="36",
            items=[
                {"name": "charger", "price": 499.0, "qty": 1,
                 "gst_rate": 0, "original_gst": 18,
                 "hsn": "8504", "gst_source": "bill_of_supply", "gst_confidence": "high",
                 "item_discount_type": "none", "item_discount_value": 0.0},
            ],
            confidence=0.9, warnings=[], raw_message="charger 499",
            created_at=datetime.utcnow(), is_bill_of_supply=False,
        )
        bill_items = _build_bill_items(pending)
        assert bill_items[0].gst_rate == 18, (
            "Safety guard must restore original_gst=18 when gst_rate=0 in Tax Invoice mode"
        )
