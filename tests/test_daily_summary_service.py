"""Tests for daily summary service and schema."""
import uuid
from datetime import date, datetime

import pytest

import main
from db.session import db_session
from db.models import Shop, Bill


def _next_inv() -> str:
    return f"TEST-{uuid.uuid4().hex[:8].upper()}"


# ── DB fixture helpers ────────────────────────────────────────────

def _shop(session, shop_id="S99999999", gstin="36AABCU9603R1ZX"):
    s = Shop(
        shop_id=shop_id,
        name="Test Shop",
        address="123 Test St",
        gstin=gstin,
        phone="9999999999",
        state="Telangana",
        state_code="36",
    )
    session.add(s)
    session.flush()
    return s


def _bill(session, shop_id, grand_total, subtotal, total_gst,
          created_at, is_return=False):
    b = Bill(
        invoice_number=_next_inv(),
        shop_id=shop_id,
        customer_name="Customer",
        items_json="[]",
        subtotal=subtotal,
        total_cgst=total_gst / 2,
        total_sgst=total_gst / 2,
        total_igst=0.0,
        total_gst=total_gst,
        grand_total=grand_total,
        is_return=is_return,
        pdf_path="test.pdf",
        created_at=created_at,
    )
    session.add(b)


# ── Tests ─────────────────────────────────────────────────────────

def test_shop_schema_has_new_columns():
    """Verify last_summary_sent_at and summary_opt_out exist on shops table."""
    from sqlalchemy import inspect as sa_inspect
    from db.session import engine

    main.init_database()

    inspector = sa_inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("shops")}
    assert "last_summary_sent_at" in cols, "missing column: last_summary_sent_at"
    assert "summary_opt_out" in cols, "missing column: summary_opt_out"


def test_get_daily_summary_data_basic():
    main.init_database()
    from services.daily_summary_service import get_daily_summary_data

    target = date(2026, 4, 22)

    with db_session() as session:
        _shop(session, "S10000001")
        _bill(session, "S10000001",
              grand_total=1180.0, subtotal=1000.0, total_gst=180.0,
              created_at=datetime(2026, 4, 22, 10, 0, 0))

    result = get_daily_summary_data("S10000001", target)

    assert result["today"]["total_bills"]   == 1
    assert result["today"]["grand_total"]   == 1180.0
    assert result["today"]["sale_amount"]   == 1000.0
    assert result["today"]["total_gst"]     == 180.0
    assert result["today"]["returns_count"] == 0
    assert result["has_gstin"] is True
    assert result["date"] == "22 Apr 2026"
    assert result["month"]["name"] == "April"


def test_get_daily_summary_data_separates_returns():
    main.init_database()
    from services.daily_summary_service import get_daily_summary_data

    target = date(2026, 4, 22)

    with db_session() as session:
        _shop(session, "S10000002")
        _bill(session, "S10000002",
              grand_total=5000.0, subtotal=4500.0, total_gst=500.0,
              created_at=datetime(2026, 4, 22, 9, 0, 0))
        _bill(session, "S10000002",
              grand_total=1000.0, subtotal=900.0, total_gst=100.0,
              created_at=datetime(2026, 4, 22, 11, 0, 0),
              is_return=True)

    result = get_daily_summary_data("S10000002", target)

    assert result["today"]["total_bills"]    == 2          # 1 sale + 1 return
    assert result["today"]["grand_total"]    == 5000.0     # sales only
    assert result["today"]["returns_count"]  == 1
    assert result["today"]["returns_amount"] == 1000.0     # abs value


def test_get_daily_summary_data_month_aggregation():
    main.init_database()
    from services.daily_summary_service import get_daily_summary_data

    target = date(2026, 4, 22)

    with db_session() as session:
        _shop(session, "S10000003")
        # 3 bills earlier in the month
        for day in [1, 10, 15]:
            _bill(session, "S10000003",
                  grand_total=1000.0, subtotal=900.0, total_gst=100.0,
                  created_at=datetime(2026, 4, day, 10, 0, 0))
        # 1 bill today
        _bill(session, "S10000003",
              grand_total=2000.0, subtotal=1800.0, total_gst=200.0,
              created_at=datetime(2026, 4, 22, 10, 0, 0))

    result = get_daily_summary_data("S10000003", target)

    assert result["today"]["total_bills"]  == 1
    assert result["month"]["total_bills"]  == 4
    assert result["month"]["grand_total"]  == 5000.0


def test_get_daily_summary_data_no_gstin():
    main.init_database()
    from services.daily_summary_service import get_daily_summary_data

    with db_session() as session:
        _shop(session, "S10000004", gstin="GSTIN00000000000")  # placeholder
        _bill(session, "S10000004",
              grand_total=500.0, subtotal=500.0, total_gst=0.0,
              created_at=datetime(2026, 4, 22, 10, 0, 0))

    result = get_daily_summary_data("S10000004", date(2026, 4, 22))
    assert result["has_gstin"] is False


def test_get_daily_summary_data_raises_for_unknown_shop():
    main.init_database()
    from services.daily_summary_service import get_daily_summary_data
    with pytest.raises(ValueError, match="Shop not found"):
        get_daily_summary_data("NOSUCHSHOP", date(2026, 4, 22))


def test_get_daily_summary_data_excludes_previous_month_bills():
    main.init_database()
    from services.daily_summary_service import get_daily_summary_data

    target = date(2026, 4, 22)

    with db_session() as session:
        _shop(session, "S10000005")
        # bill in March — must NOT appear in April month totals
        _bill(session, "S10000005",
              grand_total=9999.0, subtotal=9000.0, total_gst=999.0,
              created_at=datetime(2026, 3, 31, 10, 0, 0))
        # bill in April — must appear
        _bill(session, "S10000005",
              grand_total=1000.0, subtotal=900.0, total_gst=100.0,
              created_at=datetime(2026, 4, 1, 10, 0, 0))

    result = get_daily_summary_data("S10000005", target)

    assert result["month"]["total_bills"] == 1
    assert result["month"]["grand_total"] == 1000.0


def test_get_daily_summary_data_returns_amount_is_positive_for_negative_stored_value():
    main.init_database()
    from services.daily_summary_service import get_daily_summary_data

    with db_session() as session:
        _shop(session, "S10000006")
        _bill(session, "S10000006",
              grand_total=5000.0, subtotal=4500.0, total_gst=500.0,
              created_at=datetime(2026, 4, 22, 9, 0, 0))
        # return bill with negatively stored grand_total (some systems store returns as negative)
        _bill(session, "S10000006",
              grand_total=-1000.0, subtotal=-900.0, total_gst=-100.0,
              created_at=datetime(2026, 4, 22, 11, 0, 0),
              is_return=True)

    result = get_daily_summary_data("S10000006", date(2026, 4, 22))

    assert result["today"]["returns_amount"] == 1000.0  # positive, not -1000
    assert result["today"]["grand_total"]    == 5000.0  # sales unaffected
