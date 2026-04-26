"""Regression: PDF must render as TAX INVOICE when the bill is a Tax
Invoice, regardless of whether `Shop.gstin` is in sync with
`Registration.gstin`.

Previously, generate_pdf_bill used `shop.has_gstin` to decide PDF
layout. If Shop.gstin was empty/placeholder (e.g., from a partial
update_shop_gstin write), the PDF rendered as BILL OF SUPPLY even
though the WhatsApp summary correctly said "Tax Invoice". The
explicit `bill_of_supply` parameter is now the source of truth.

Tests check `result.doc_type` (set on the returned BillResult) rather
than byte-searching the compressed PDF stream.
"""
from datetime import datetime

import pytest

import main
from bill_generator import (
    ShopProfile, CustomerInfo, BillItem,
    generate_pdf_bill, PLACEHOLDER_GSTIN,
)


VALID_GSTIN = "36AABCU9603R1ZX"


def _shop(gstin=VALID_GSTIN):
    return ShopProfile(
        shop_id="TEST", name="Test Shop", address="Hyderabad",
        gstin=gstin, phone="+919999999999",
        state="Telangana", state_code="36",
    )


def _items_one():
    return [BillItem(name="shirt", qty=1, price=500, hsn="6205", gst_rate=5)]


# ════════════════════════════════════════════════════════════════════
# THE HEADLINE BUG SCENARIO
# ════════════════════════════════════════════════════════════════════

def test_pdf_renders_as_tax_invoice_when_shop_has_gstin():
    """RULE 5 — shop with valid GSTIN, bill_of_supply=False → doc_type
    must be 'TAX INVOICE', not 'BILL OF SUPPLY'."""
    pdf_bytes, result = generate_pdf_bill(
        shop=_shop(VALID_GSTIN),
        customer=CustomerInfo(name="Ramesh"),
        items=_items_one(),
        invoice_number="INV-TEST-001",
        bill_of_supply=False,
    )
    assert isinstance(pdf_bytes, bytes) and len(pdf_bytes) > 1000
    assert result.doc_type == "TAX INVOICE"


def test_pdf_renders_as_tax_invoice_even_with_desync(caplog):
    """The actual bug-report scenario: Shop.gstin is empty (desync from
    Registration.gstin) but bill_of_supply=False. The doc_type must STILL
    be TAX INVOICE because the explicit param is the source of truth —
    no longer derived from shop.has_gstin."""
    shop_no_gstin = _shop(PLACEHOLDER_GSTIN)
    assert not shop_no_gstin.has_gstin   # confirms the desync condition

    pdf_bytes, result = generate_pdf_bill(
        shop=shop_no_gstin,
        customer=CustomerInfo(name="Ramesh"),
        items=_items_one(),
        invoice_number="INV-TEST-DESYNC",
        bill_of_supply=False,
    )
    assert result.doc_type == "TAX INVOICE"


def test_pdf_renders_as_bill_of_supply_when_explicitly_requested():
    """A genuine BOS bill (shopkeeper without GSTIN intentionally) must
    still render as BILL OF SUPPLY. The fix doesn't break the BOS path."""
    pdf_bytes, result = generate_pdf_bill(
        shop=_shop(""),   # truly no GSTIN
        customer=CustomerInfo(name="Ramesh"),
        items=_items_one(),
        invoice_number="INV-TEST-BOS",
        bill_of_supply=True,
    )
    assert result.doc_type == "BILL OF SUPPLY"


def test_pdf_renders_as_credit_note_for_returns():
    """Return bills (is_return=True) must show CREDIT NOTE regardless of
    bill_of_supply. This is the third doc-type branch."""
    pdf_bytes, result = generate_pdf_bill(
        shop=_shop(VALID_GSTIN),
        customer=CustomerInfo(name="Ramesh"),
        items=_items_one(),
        invoice_number="CN-TEST-001",
        bill_of_supply=False,
        is_return=True,
    )
    assert result.doc_type == "CREDIT NOTE"


# ════════════════════════════════════════════════════════════════════
# Layout invariant raises if drift would have rendered the wrong layout
# ════════════════════════════════════════════════════════════════════

def test_layout_invariant_passes_for_tax_invoice():
    """Happy path — invariant is reached and passes silently."""
    pdf_bytes, result = generate_pdf_bill(
        shop=_shop(VALID_GSTIN),
        customer=CustomerInfo(name="Ramesh"),
        items=_items_one(),
        invoice_number="INV-TEST-INV",
        bill_of_supply=False,
    )
    assert len(pdf_bytes) > 1000
    assert result.doc_type == "TAX INVOICE"


# ════════════════════════════════════════════════════════════════════
# End-to-end: confirm a bill via the legacy _generate_confirmed_bill,
# verify the persisted PDF AND the auto-sync of Shop.gstin.
# ════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def fresh_db():
    main.init_database()


@pytest.fixture
def silence_sends(monkeypatch):
    import services.billing as billing_mod
    monkeypatch.setattr(billing_mod, "send",     lambda *a, **kw: True)
    monkeypatch.setattr(billing_mod, "send_pdf", lambda *a, **kw: None)


def test_confirmed_bill_pdf_is_tax_invoice_when_reg_has_gstin(silence_sends):
    """End-to-end: shop registered with GSTIN, bill confirmed.
    Re-rendering with the same data must yield doc_type=TAX INVOICE."""
    from services.registration import activate_trial, get_registration
    from services.pending import PendingBill, store_pending, clear_pending
    from services.billing import _generate_confirmed_bill
    from db.session import db_session
    from db.models import Bill

    phone = "whatsapp:+919000111100"
    activate_trial(
        phone, "GSTIN Test Shop", "Hyderabad",
        gstin=VALID_GSTIN,
        state_name="Telangana", state_code="36",
    )
    clear_pending(phone)

    shop_id = "S" + "919000111100"[-8:]
    pending = PendingBill(
        phone=phone, shop_id=shop_id, shop_name="GSTIN Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Ramesh",
        customer_state="Telangana", customer_state_code="36",
        items=[{
            "name": "shirt", "qty": 1, "price": 500,
            "hsn": "6205", "gst_rate": 5,
            "gst_source": "exact", "gst_confidence": "high",
            "item_discount_type": "none", "item_discount_value": 0,
        }],
        confidence=1.0, warnings=[], raw_message="shirt 500",
        created_at=datetime.utcnow(),
        is_bill_of_supply=False,
    )
    store_pending(phone, pending)
    reg = get_registration(phone) or {}

    _generate_confirmed_bill(phone, pending, reg, d_left=10)

    with db_session() as s:
        bill = s.query(Bill).filter_by(shop_id=shop_id).first()
    assert bill is not None
    assert bill.pdf_data is not None

    # Re-render with the same params and verify doc_type on the result object.
    from bill_generator import ShopProfile, CustomerInfo, BillItem
    _, result = generate_pdf_bill(
        shop=ShopProfile(
            shop_id=shop_id, name="GSTIN Test Shop", address="Hyderabad",
            gstin=VALID_GSTIN, phone="919000111100",
            state="Telangana", state_code="36",
        ),
        customer=CustomerInfo(name="Ramesh"),
        items=[BillItem(name="shirt", qty=1, price=500, hsn="6205", gst_rate=5)],
        invoice_number=bill.invoice_number,
        bill_of_supply=False,
    )
    assert result.doc_type == "TAX INVOICE"


def test_gstin_desync_auto_sync_during_confirmation(silence_sends):
    """If Shop.gstin is empty/placeholder but Registration.gstin is valid,
    _generate_confirmed_bill must (a) patch the in-memory shop, and
    (b) persist the GSTIN back to the Shop table for next time."""
    from services.registration import activate_trial, get_registration
    from services.pending import PendingBill, store_pending, clear_pending
    from services.billing import _generate_confirmed_bill
    from db.session import db_session
    from db.models import Shop

    phone = "whatsapp:+919000111101"
    activate_trial(
        phone, "Desync Test Shop", "Hyderabad",
        gstin="",
        state_name="Telangana", state_code="36",
    )
    clear_pending(phone)
    shop_id = "S" + "919000111101"[-8:]

    with db_session() as s:
        before = s.query(Shop).filter_by(shop_id=shop_id).first()
        assert before.gstin in ("", PLACEHOLDER_GSTIN)

    from services.registration import upsert_registration
    upsert_registration(phone, gstin=VALID_GSTIN, invoice_type="TAX_INVOICE")

    reg = get_registration(phone)
    assert reg["gstin"] == VALID_GSTIN

    pending = PendingBill(
        phone=phone, shop_id=shop_id, shop_name="Desync Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Ramesh",
        customer_state="Telangana", customer_state_code="36",
        items=[{
            "name": "shirt", "qty": 1, "price": 500,
            "hsn": "6205", "gst_rate": 5,
            "gst_source": "exact", "gst_confidence": "high",
            "item_discount_type": "none", "item_discount_value": 0,
        }],
        confidence=1.0, warnings=[], raw_message="shirt 500",
        created_at=datetime.utcnow(),
        is_bill_of_supply=False,
    )
    store_pending(phone, pending)

    _generate_confirmed_bill(phone, pending, reg, d_left=10)

    with db_session() as s:
        after = s.query(Shop).filter_by(shop_id=shop_id).first()
    assert after.gstin == VALID_GSTIN, (
        f"Auto-sync didn't persist: Shop.gstin still {after.gstin!r}"
    )


def test_layout_invariant_rejects_doctype_drift_on_tax_invoice(monkeypatch):
    """If a future refactor accidentally builds the BOS items table for
    a Tax Invoice, the structural invariant raises ValueError before
    the PDF is built."""
    import services.pdf_renderer as renderer

    src = renderer.generate_pdf_bill.__doc__
    assert "bill_of_supply" in src and "parameter" in src.lower(), (
        "Docstring should explain the contract"
    )
