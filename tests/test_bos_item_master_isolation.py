"""Regression tests: Bill of Supply confirmations must NOT poison the
shop_item_master with gst_rate=0 entries.

Background: a BOS bill carries gst_rate=0 on every item by design. Before
the is_bos guard in save_item_master, those zero-rate rows wrote into
ShopItemMaster as confirmed=True. Step 0 of get_gst_rate_smart hits the
master FIRST, so the same shop's next Tax Invoice bill would silently
return 0% for the same item — bypassing the explicit GST_RATES dict
and Claude lookup entirely.

Two integration tests + one unit test cover the contract.
"""
from datetime import datetime

import pytest

import main


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_db():
    """Ensure a fresh DB schema for every test."""
    main.init_database()


@pytest.fixture
def silence_sends(monkeypatch):
    """Stub out WhatsApp Graph API calls — _generate_confirmed_bill
    sends text + PDF; tests must not hit the network."""
    import services.billing as billing_mod
    monkeypatch.setattr(billing_mod, "send",     lambda *a, **kw: True)
    monkeypatch.setattr(billing_mod, "send_pdf", lambda *a, **kw: None)


def _make_pending(*, phone, shop_id, shop_name, gstin, is_bos, item_gst_rate):
    """Build a PendingBill ready to be confirmed.

    item_gst_rate=0 with is_bos=True simulates the BOS path.
    item_gst_rate=5 with is_bos=False simulates a Tax Invoice for kurtha.
    """
    from services.pending import PendingBill
    return PendingBill(
        phone=phone, shop_id=shop_id, shop_name=shop_name,
        shop_state="Telangana", shop_state_code="36",
        customer_name="Ramesh",
        customer_state="Telangana", customer_state_code="36",
        items=[{
            "name": "kurtha", "qty": 1, "price": 500,
            "hsn": "6211", "gst_rate": item_gst_rate,
            "gst_source": "exact" if not is_bos else "bill_of_supply",
            "gst_confidence": "high",
            "item_discount_type": "none", "item_discount_value": 0,
        }],
        confidence=1.0, warnings=[], raw_message="kurtha 500",
        created_at=datetime.utcnow(),
        is_return=False,
        is_bill_of_supply=is_bos,
        is_inclusive=False,
        pricing_type="exclusive",
    )


def _query_master(shop_id, item_name="kurtha"):
    """Return the ShopItemMaster row for (shop_id, item_name) or None."""
    from db.session import db_session
    from db.models import ShopItemMaster
    with db_session() as s:
        row = s.query(ShopItemMaster).filter_by(
            shop_id=shop_id, item_name=item_name.lower().strip(),
        ).first()
        if row is None:
            return None
        return {
            "item_name": row.item_name,
            "hsn":       row.hsn,
            "gst_rate":  row.gst_rate,
            "confirmed": row.confirmed,
            "use_count": row.use_count,
        }


# ── Test 1: BOS bill must NOT pollute item master ──────────────────

def test_bos_bill_confirmation_does_not_save_zero_rate_kurtha(silence_sends):
    """Confirm a BOS bill for kurtha 500 → shop_item_master must contain
    NO row with gst_rate=0 for kurtha.

    This is the core anti-poisoning guarantee. Before this fix, a kurtha
    row with gst_rate=0, confirmed=True would be created here and would
    intercept every future Tax Invoice lookup for the same shop.
    """
    from services.registration import activate_trial
    from services.billing import _generate_confirmed_bill

    phone = "whatsapp:+919000077001"
    activate_trial(phone, "BOS Sari Shop", "Hyderabad",
                   gstin="",  # no GSTIN → BOS
                   state_name="Telangana", state_code="36")

    shop_id = "S" + "919000077001"[-8:]
    pending = _make_pending(
        phone=phone, shop_id=shop_id, shop_name="BOS Sari Shop",
        gstin="", is_bos=True, item_gst_rate=0,
    )

    reg = {"address": "Hyderabad", "gstin": "", "bills_count": 0}
    _generate_confirmed_bill(phone, pending, reg, d_left=10)

    row = _query_master(shop_id, "kurtha")
    assert row is None, (
        f"BOS bill polluted item master with kurtha row: {row!r} — "
        f"this would force gst_rate=0 on every future Tax Invoice for "
        f"this shop until manually corrected."
    )


# ── Test 2: Tax Invoice bill SAVES the correct rate ────────────────

def test_tax_invoice_bill_confirmation_saves_kurtha_at_5pct(silence_sends):
    """Confirm a Tax Invoice bill for kurtha 500 → shop_item_master
    DOES contain a kurtha row at gst_rate=5, confirmed=True.

    Verifies the is_bos guard didn't accidentally short-circuit the
    happy path — Tax Invoice bills must still populate the master.
    """
    from services.registration import activate_trial
    from services.billing import _generate_confirmed_bill

    phone = "whatsapp:+919000077002"
    activate_trial(phone, "Tax Invoice Boutique", "Hyderabad",
                   gstin="36AABCU9603R1ZX",
                   state_name="Telangana", state_code="36")

    shop_id = "S" + "919000077002"[-8:]
    pending = _make_pending(
        phone=phone, shop_id=shop_id, shop_name="Tax Invoice Boutique",
        gstin="36AABCU9603R1ZX", is_bos=False, item_gst_rate=5,
    )

    reg = {"address": "Hyderabad", "gstin": "36AABCU9603R1ZX",
           "bills_count": 0}
    _generate_confirmed_bill(phone, pending, reg, d_left=10)

    row = _query_master(shop_id, "kurtha")
    assert row is not None, (
        "Tax Invoice bill failed to save kurtha to item master — "
        "is_bos guard regressed the happy path."
    )
    assert row["gst_rate"] == 5, (
        f"Tax Invoice saved kurtha at gst_rate={row['gst_rate']}% — expected 5%"
    )
    assert row["confirmed"] is True
    assert row["hsn"] == "6211"
    assert row["use_count"] >= 1


# ── Test 3: Direct unit on save_item_master with the flag ──────────

def test_save_item_master_with_is_bos_true_skips_save():
    """Unit-level: save_item_master(is_bos=True) is a no-op.

    Covers both code paths the flag must short-circuit:
      (a) creating a new row (poison-by-creation)
      (b) overwriting an existing row (poison-by-clobber)
    """
    from db.session import db_session
    from db.models import ShopItemMaster
    from db.item_master import save_item_master

    shop_id = "TEST_BOS_GUARD"

    # (a) New row never created when is_bos=True.
    save_item_master(shop_id, "kurtha", "6211", 0,
                     confirmed=True, is_bos=True)
    with db_session() as s:
        assert s.query(ShopItemMaster).filter_by(
            shop_id=shop_id, item_name="kurtha",
        ).first() is None, "is_bos=True still created a new row"

    # (b) Pre-seed a valid 5% Tax Invoice entry.
    save_item_master(shop_id, "kurtha", "6211", 5,
                     confirmed=True, is_bos=False)
    pre = _query_master(shop_id, "kurtha")
    assert pre is not None and pre["gst_rate"] == 5

    # Now a BOS save attempt MUST NOT clobber it.
    save_item_master(shop_id, "kurtha", "6211", 0,
                     confirmed=True, is_bos=True)
    post = _query_master(shop_id, "kurtha")
    assert post is not None
    assert post["gst_rate"] == 5, (
        f"BOS save clobbered an existing 5% entry: now {post['gst_rate']}%"
    )
    # use_count must NOT increment for the BOS attempt — that would
    # mis-rank the item in 'myitems'.
    assert post["use_count"] == pre["use_count"], (
        "is_bos=True incremented use_count — BOS sales must not influence ranking"
    )

    # Cleanup so this test is hermetic.
    with db_session() as s:
        s.query(ShopItemMaster).filter_by(shop_id=shop_id).delete()
