"""Unit tests for core.daily_summary formatter. No DB, no network."""
import pytest
from core.daily_summary import _fmt, format_daily_summary


# ── _fmt: Indian number formatting ────────────────────────────────


def test_fmt_below_1000():
    assert _fmt(0)    == "₹0"
    assert _fmt(850)  == "₹850"
    assert _fmt(999)  == "₹999"


def test_fmt_thousands():
    assert _fmt(1000)  == "₹1,000"
    assert _fmt(34200) == "₹34,200"
    assert _fmt(99999) == "₹99,999"


def test_fmt_lakhs_basic():
    assert _fmt(100000) == "₹1.0L"
    assert _fmt(420000) == "₹4.2L"


def test_fmt_lakhs_rounding():
    # Traditional rounding (not banker's rounding)
    assert _fmt(104000) == "₹1.0L"   # 1.04 → rounds down → 1.0
    assert _fmt(105000) == "₹1.1L"   # 1.05 → rounds up  → 1.1


def test_fmt_rounds_before_format():
    # 34200.99 rounds to 34201 before formatting
    assert _fmt(34200.99) == "₹34,201"


def test_fmt_negative_becomes_zero():
    assert _fmt(-500) == "₹0"


def test_fmt_none_becomes_zero():
    assert _fmt(None) == "₹0"


# ── format_daily_summary ─────────────────────────────────────────

def _sample_data(
    has_gstin=True,
    total_bills=18,
    grand_total=34200.0,
    sale_amount=29000.0,
    total_gst=5200.0,
    returns_count=0,
    returns_amount=0.0,
    month_returns_count=0,
    month_returns_amount=0.0,
):
    """Build a minimal valid data dict."""
    return {
        "shop_name": "Ravi Mobile Accessories",
        "has_gstin": has_gstin,
        "date": "22 Apr 2026",
        "today": {
            "total_bills":    total_bills,
            "grand_total":    grand_total,
            "sale_amount":    sale_amount,
            "total_gst":      total_gst,
            "returns_count":  returns_count,
            "returns_amount": returns_amount,
        },
        "month": {
            "name":           "April",
            "total_bills":    312,
            "grand_total":    420000.0,
            "sale_amount":    356000.0,
            "total_gst":      64000.0,
            "returns_count":  month_returns_count,
            "returns_amount": month_returns_amount,
        },
    }


def test_gst_variant_contains_required_lines():
    result = format_daily_summary(_sample_data(has_gstin=True))
    assert "📊 *Today's Summary — 22 Apr 2026*" in result
    assert "🧾 Bills: 18" in result
    assert "💰 Sale Amount: ₹29,000" in result
    assert "🏛 GST Collected: ₹5,200" in result
    assert "✅ Grand Total: ₹34,200" in result
    assert "📅 *April So Far*" in result
    assert "✅ Total: ₹4.2L" in result


def test_gst_variant_no_returns_omits_returns_lines():
    result = format_daily_summary(_sample_data(has_gstin=True, returns_count=0))
    assert "↩️" not in result
    assert "🏁" not in result


def test_gst_variant_with_returns_shows_net_total():
    result = format_daily_summary(_sample_data(
        has_gstin=True, returns_count=1, returns_amount=850.0
    ))
    assert "↩️ Returns: 1 bill(s) — ₹850" in result
    assert "🏁 Net Total: ₹33,350" in result


def test_no_gstin_variant_omits_gst_lines():
    result = format_daily_summary(_sample_data(has_gstin=False))
    assert "💰 Total Billed:" in result
    assert "🏛 GST Collected" not in result
    assert "💰 Sale Amount" not in result


def test_zero_bills_shows_motivational_message():
    result = format_daily_summary(_sample_data(total_bills=0))
    assert "No bills today" in result
    assert "stronger tomorrow" in result
    assert "📅 *April So Far*" in result


def test_line_limit_trims_month_returns_first():
    # Worst case: GST + today returns + month returns = 19 lines
    data = _sample_data(
        has_gstin=True,
        returns_count=1,
        returns_amount=850.0,
        month_returns_count=4,
        month_returns_amount=3200.0,
    )
    result = format_daily_summary(data)
    lines = result.split("\n")
    assert len(lines) <= 18
    assert "↩️ Returns: 4 bill(s)" not in result   # month returns trimmed
    assert "↩️ Returns: 1 bill(s)" in result        # today returns kept


def test_missing_fields_default_to_zero():
    result = format_daily_summary({
        "shop_name": "X",
        "has_gstin": True,
        "date": "22 Apr 2026",
        "today": {},
        "month": {"name": "April"},
    })
    # Zero bills → motivational message
    assert "No bills today" in result


def test_net_total_never_negative():
    # returns_amount > grand_total → net = 0
    result = format_daily_summary(_sample_data(
        returns_count=1, returns_amount=99999.0
    ))
    assert "🏁 Net Total: ₹0" in result
