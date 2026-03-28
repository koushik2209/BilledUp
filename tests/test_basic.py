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
