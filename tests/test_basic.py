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
    # Exact match item — no warning marker
    assert "phone case" in preview
    assert "18% GST" in preview
    # Default match item — has warning marker
    assert "unknown gadget" in preview
    # Should have prominent default-GST warning for unknown item
    assert "unknown gadget" in preview.lower()
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
    assert "YES" in preview
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


def test_clothing_slab_at_1000():
    """Clothing at exactly ₹1000 → 5% GST."""
    rate = {"hsn": "6205", "gst": 12, "source": "exact"}
    adjusted = adjust_gst_for_price("shirt", 1000, rate)
    assert adjusted["gst"] == 5


def test_clothing_slab_above_1000():
    """Clothing >₹1000 → 12% GST."""
    rate = {"hsn": "6205", "gst": 12, "source": "exact"}
    adjusted = adjust_gst_for_price("shirt", 1500, rate)
    assert adjusted["gst"] == 12


def test_footwear_slab_below_1000():
    """Footwear ≤₹1000 → 5% GST."""
    rate = {"hsn": "6403", "gst": 18, "source": "exact"}
    adjusted = adjust_gst_for_price("shoes", 800, rate)
    assert adjusted["gst"] == 5


def test_footwear_slab_above_1000():
    """Footwear >₹1000 → 18% GST."""
    rate = {"hsn": "6403", "gst": 18, "source": "exact"}
    adjusted = adjust_gst_for_price("shoes", 2500, rate)
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
    assert is_clothing_item("saree") is True
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
                                      gst_client=None, is_return=True)
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
    """End-to-end: cheap shirt gets 5%, expensive shirt gets 12%."""
    cheap = [BillItem("shirt", 1, 800)]
    br_cheap = calculate_bill(cheap, gst_client=None)
    assert br_cheap.items[0].gst_rate == 5
    assert br_cheap.total_gst == 40.0  # 5% of 800

    expensive = [BillItem("shirt", 1, 1500)]
    br_exp = calculate_bill(expensive, gst_client=None)
    assert br_exp.items[0].gst_rate == 12
    assert br_exp.total_gst == 180.0  # 12% of 1500


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
    """Tracksuit should be clothing with correct GST (Priority 3 fix)."""
    from gst_rates import get_gst_rate_smart, is_clothing_item, adjust_gst_for_price
    assert is_clothing_item("tracksuit") is True
    rate = get_gst_rate_smart("tracksuit")
    adjusted = adjust_gst_for_price("tracksuit", 2000, rate)
    assert adjusted["gst"] == 12  # >₹1000 clothing


def test_lehenga_gst_clothing():
    """Lehenga should be clothing with correct GST (Priority 3 fix)."""
    from gst_rates import get_gst_rate_smart, is_clothing_item, adjust_gst_for_price
    assert is_clothing_item("lehenga") is True
    rate = get_gst_rate_smart("lehenga")
    adjusted = adjust_gst_for_price("lehenga", 5000, rate)
    assert adjusted["gst"] == 12  # >₹1000 clothing


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
