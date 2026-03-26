"""Smoke tests for BillEasy (no live Claude API calls). Run with: pytest"""

import bill_generator as bg
from bill_generator import BillItem, calculate_bill, number_to_words, generate_invoice_number
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


def test_main_database_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(bg, "INVOICE_REGISTRY_FILE", str(tmp_path / "registry.json"))
    main.init_database()
    main.seed_demo_shop()
    assert main.get_shop("RAVI") is not None
    assert main.get_shop("NONEXISTENT") is None


def test_invoice_sequence(tmp_path, monkeypatch):
    monkeypatch.setattr(bg, "INVOICE_REGISTRY_FILE", str(tmp_path / "registry.json"))
    n1 = generate_invoice_number("PYTEST")
    n2 = generate_invoice_number("PYTEST")
    assert n1 != n2
    assert int(n2.split("-")[-1]) == int(n1.split("-")[-1]) + 1
