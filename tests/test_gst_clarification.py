"""Tests for the GST clarification state-machine flow."""

import json
from datetime import datetime

import pytest

import main


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def phone():
    return "whatsapp:+919000044444"


@pytest.fixture
def shop_id(phone):
    digits = "".join(c for c in phone if c.isdigit())
    return "S" + digits[-8:]


@pytest.fixture
def reg(phone):
    """Activate a trial shop and return its registration dict.

    Also clears any pending bill for this phone so every test starts clean.
    """
    from services.registration import activate_trial, get_registration
    from services.pending import clear_pending
    main.init_database()
    activate_trial(
        phone, "Clarify Shop", "Hyderabad",
        gstin="36AABCU9603R1ZX",
        state_name="Telangana", state_code="36",
    )
    clear_pending(phone)
    return get_registration(phone)


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound messages from services.billing.send."""
    out: list[tuple[str, str]] = []

    def _fake_send(to, body):
        out.append((to, body))
        return True

    import services.billing as billing_mod
    monkeypatch.setattr(billing_mod, "send", _fake_send)
    return out


# ── Mock helpers ────────────────────────────────────────────────────

def _install_parser(monkeypatch, items, **extra):
    """Replace parse_message to return a fixed canned result."""
    import services.billing as billing_mod

    def _fake(_msg):
        return {
            "customer_name":       extra.get("customer_name", "Customer"),
            "customer_phone":      extra.get("customer_phone"),
            "items":               items,
            "bill_discount_type":  extra.get("bill_discount_type", "none"),
            "bill_discount_value": extra.get("bill_discount_value", 0.0),
            "pricing_type":        extra.get("pricing_type", "exclusive"),
            "needs_confirmation":  False,
            "confidence":          0.95,
            "warnings":            [],
            "notes":               "",
            "error":               extra.get("error"),
            "parse_time_ms":       0,
        }

    monkeypatch.setattr(billing_mod, "parse_message", _fake)


def _install_gst_resolver(monkeypatch, ok_names):
    """Replace _resolve_gst_for_item: succeed for names in ok_names, else raise."""
    import services.billing as billing_mod
    ok_lower = {n.lower() for n in ok_names}

    def _fake(name, price, shop_id):
        if name.lower() in ok_lower:
            return {
                "hsn": "9999", "gst": 18,
                "source": "exact", "confidence": "high",
            }
        raise billing_mod.GSTLookupError(name)

    monkeypatch.setattr(billing_mod, "_resolve_gst_for_item", _fake)


def _make_item(name, price, qty=1):
    return {
        "name": name, "qty": qty, "price": price,
        "item_discount_type": "none", "item_discount_value": 0,
    }


def _seed_clarification_state(
    phone, shop_id, *,
    valid_items=None, failed_items=None,
    is_return=False, is_bill_of_supply=False,
):
    """Persist a PendingBill already in clarification state."""
    from services.pending import PendingBill, store_pending
    if valid_items is None:
        valid_items = [{
            "name": "rice", "qty": 1, "price": 100,
            "hsn": "1006", "gst_rate": 5,
            "gst_source": "exact", "gst_confidence": "high",
            "item_discount_type": "none", "item_discount_value": 0,
        }]
    if failed_items is None:
        failed_items = [_make_item("xyz", 200)]
    pending = PendingBill(
        phone=phone, shop_id=shop_id, shop_name="Clarify Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Ramesh",
        customer_state="Telangana", customer_state_code="36",
        items=[],
        confidence=0.95, warnings=[], raw_message="rice 100 xyz 200",
        created_at=datetime.utcnow(),
        is_return=is_return,
        is_bill_of_supply=is_bill_of_supply,
        is_inclusive=False, pricing_type="exclusive",
        valid_items=valid_items,
        failed_items=failed_items,
        awaiting_gst_clarification=True,
    )
    store_pending(phone, pending)
    return pending


# ── Serialization ───────────────────────────────────────────────────

def test_pending_roundtrips_clarification_fields():
    from services.pending import (
        PendingBill, _serialize_pending, _deserialize_pending,
    )
    pb = PendingBill(
        phone="+919", shop_id="S1", shop_name="x",
        shop_state="tg", shop_state_code="36",
        customer_name="C",
        customer_state="tg", customer_state_code="36",
        items=[], confidence=1.0, warnings=[], raw_message="",
        created_at=datetime(2026, 4, 24, 12, 0, 0),
        valid_items=[{"name": "rice", "qty": 1, "price": 100,
                      "hsn": "1006", "gst_rate": 5}],
        failed_items=[{"name": "xyz", "qty": 1, "price": 200}],
        awaiting_gst_clarification=True,
    )
    back = _deserialize_pending(_serialize_pending(pb))
    assert back.valid_items == pb.valid_items
    assert back.failed_items == pb.failed_items
    assert back.awaiting_gst_clarification is True


def test_pending_backcompat_without_clarification_fields():
    """Old serialized pending bills deserialize with safe defaults."""
    from services.pending import _deserialize_pending
    old = json.dumps({
        "phone": "+919", "shop_id": "S1", "shop_name": "x",
        "shop_state": "tg", "shop_state_code": "36",
        "customer_name": "C",
        "customer_state": "tg", "customer_state_code": "36",
        "items": [], "confidence": 1.0, "warnings": [],
        "raw_message": "", "created_at": "2026-04-24T12:00:00",
    })
    back = _deserialize_pending(old)
    assert back.valid_items == []
    assert back.failed_items == []
    assert back.awaiting_gst_clarification is False


# ── _handle_new_bill: partial failure enters clarification ─────────

def test_new_bill_partial_failure_enters_clarification(
    phone, shop_id, reg, sent, monkeypatch,
):
    from services.billing import _handle_new_bill
    from services.pending import get_pending_bill

    _install_parser(monkeypatch, [
        _make_item("rice", 100),
        _make_item("xyz_unknown", 200),
    ])
    _install_gst_resolver(monkeypatch, ok_names={"rice"})

    _handle_new_bill(phone, "rice 100 xyz_unknown 200", reg, shop_id, "Shop", 9)

    pending = get_pending_bill(phone)
    assert pending is not None
    assert pending.awaiting_gst_clarification is True
    assert pending.items == []   # never populated during partial failure
    assert [i["name"] for i in pending.valid_items] == ["rice"]
    assert pending.valid_items[0]["hsn"] == "9999"
    assert pending.valid_items[0]["gst_rate"] == 18
    assert [i["name"] for i in pending.failed_items] == ["xyz_unknown"]
    assert any("xyz_unknown" in body for _, body in sent)


def test_new_bill_all_success_skips_clarification(
    phone, shop_id, reg, sent, monkeypatch,
):
    """Regression: all items resolve → preview is sent, not clarification."""
    from services.billing import _handle_new_bill
    from services.pending import get_pending_bill

    _install_parser(monkeypatch, [_make_item("rice", 100)])
    _install_gst_resolver(monkeypatch, ok_names={"rice"})

    _handle_new_bill(phone, "rice 100", reg, shop_id, "Shop", 9)

    pending = get_pending_bill(phone)
    assert pending is not None
    assert pending.awaiting_gst_clarification is False
    assert pending.valid_items == []
    assert pending.failed_items == []
    assert len(pending.items) == 1
    assert pending.items[0]["hsn"] == "9999"


# ── _handle_gst_clarification ──────────────────────────────────────

def test_clarification_resolves_and_resumes(
    phone, shop_id, reg, sent, monkeypatch,
):
    from services.billing import _handle_gst_clarification
    from services.pending import get_pending_bill

    pending = _seed_clarification_state(phone, shop_id)

    _install_parser(monkeypatch, [_make_item("power adapter", 200)])
    _install_gst_resolver(monkeypatch, ok_names={"power adapter"})

    _handle_gst_clarification(phone, "power adapter 200", pending)

    updated = get_pending_bill(phone)
    assert updated.awaiting_gst_clarification is False
    assert updated.valid_items == []
    assert updated.failed_items == []
    names = [i["name"] for i in updated.items]
    assert "rice" in names          # originally resolved item preserved
    assert "power adapter" in names  # newly resolved item merged in
    assert any("all items resolved" in body.lower() for _, body in sent)


def test_clarification_still_failing_keeps_state(
    phone, shop_id, reg, sent, monkeypatch,
):
    from services.billing import _handle_gst_clarification
    from services.pending import get_pending_bill

    pending = _seed_clarification_state(phone, shop_id)

    _install_parser(monkeypatch, [_make_item("gibberish", 200)])
    _install_gst_resolver(monkeypatch, ok_names=set())

    _handle_gst_clarification(phone, "gibberish 200", pending)

    still = get_pending_bill(phone)
    assert still.awaiting_gst_clarification is True
    # failed_items replaced with the NEW failures (not appended)
    assert [i["name"] for i in still.failed_items] == ["gibberish"]
    # Original valid_items preserved
    assert [i["name"] for i in still.valid_items] == ["rice"]


def test_clarification_repeated_failure_loop(
    phone, shop_id, reg, sent, monkeypatch,
):
    """User can retry multiple times; earlier resolved items never get lost."""
    from services.billing import _handle_gst_clarification
    from services.pending import get_pending_bill

    pending = _seed_clarification_state(phone, shop_id)

    # Round 1: fails
    _install_parser(monkeypatch, [_make_item("widget", 150)])
    _install_gst_resolver(monkeypatch, ok_names=set())
    _handle_gst_clarification(phone, "widget 150", pending)

    # Round 2: succeeds
    pending2 = get_pending_bill(phone)
    assert pending2.awaiting_gst_clarification is True
    _install_parser(monkeypatch, [_make_item("lamp", 150)])
    _install_gst_resolver(monkeypatch, ok_names={"lamp"})
    _handle_gst_clarification(phone, "lamp 150", pending2)

    final = get_pending_bill(phone)
    assert final.awaiting_gst_clarification is False
    names = [i["name"] for i in final.items]
    assert "rice" in names    # survived both rounds
    assert "lamp" in names    # resolved in round 2
    assert "widget" not in names  # dropped (user replaced it)


def test_clarification_unparseable_input_reprompts(
    phone, shop_id, reg, sent, monkeypatch,
):
    """If parse yields no items, reply re-prompts and state is preserved."""
    from services.billing import _handle_gst_clarification
    from services.pending import get_pending_bill

    pending = _seed_clarification_state(phone, shop_id)
    _install_parser(monkeypatch, [], error="nothing parseable")

    _handle_gst_clarification(phone, "lol idk", pending)

    still = get_pending_bill(phone)
    assert still.awaiting_gst_clarification is True
    assert [i["name"] for i in still.failed_items] == ["xyz"]
    assert any("couldn't read" in body.lower() for _, body in sent)


def test_clarification_preserves_is_return(
    phone, shop_id, reg, sent, monkeypatch,
):
    """is_return flag survives clarification → final items get negated prices."""
    from services.billing import _handle_gst_clarification
    from services.pending import get_pending_bill

    pending = _seed_clarification_state(
        phone, shop_id,
        valid_items=[],                         # nothing resolved yet
        failed_items=[_make_item("xyz", 200)],
        is_return=True,
    )

    _install_parser(monkeypatch, [_make_item("lamp", 200)])
    _install_gst_resolver(monkeypatch, ok_names={"lamp"})
    _handle_gst_clarification(phone, "lamp 200", pending)

    final = get_pending_bill(phone)
    assert final.is_return is True
    assert final.awaiting_gst_clarification is False
    assert final.items[0]["price"] < 0   # return items have negative price


def test_clarification_bos_uses_apply_bos_defaults(
    phone, shop_id, reg, sent, monkeypatch,
):
    """BOS bill doesn't hit GST lookup at all — items get BOS defaults."""
    from services.billing import _handle_gst_clarification
    from services.pending import get_pending_bill

    pending = _seed_clarification_state(
        phone, shop_id,
        valid_items=[],
        failed_items=[_make_item("custom item", 300)],
        is_bill_of_supply=True,
    )

    _install_parser(monkeypatch, [_make_item("custom item", 300)])
    # Resolver should NEVER be called in BOS mode — make it raise if it is
    import services.billing as billing_mod

    def _boom(*a, **kw):
        raise AssertionError("resolver must not be called in BOS mode")

    monkeypatch.setattr(billing_mod, "_resolve_gst_for_item", _boom)

    _handle_gst_clarification(phone, "custom item 300", pending)

    final = get_pending_bill(phone)
    assert final.awaiting_gst_clarification is False
    assert final.items[0]["gst_rate"] == 0
    assert final.items[0]["hsn"] == "9999"


# ── _handle_confirmation routing ────────────────────────────────────

def test_confirmation_cancel_during_clarification(
    phone, shop_id, reg, sent,
):
    from services.billing import _handle_confirmation
    from services.pending import get_pending_bill

    pending = _seed_clarification_state(phone, shop_id)
    _handle_confirmation(phone, "cancel", "cancel", pending, reg, 9)

    assert get_pending_bill(phone) is None
    assert any("discarded" in body.lower() for _, body in sent)


def test_confirmation_edit_during_clarification(
    phone, shop_id, reg, sent,
):
    from services.billing import _handle_confirmation
    from services.pending import get_pending_bill

    pending = _seed_clarification_state(phone, shop_id)
    _handle_confirmation(phone, "edit", "edit", pending, reg, 9)

    assert get_pending_bill(phone) is None
    assert any("cleared" in body.lower() for _, body in sent)


def test_confirmation_yes_during_clarification_is_rejected(
    phone, shop_id, reg, sent,
):
    """YES mid-clarification must not silently confirm a half-built bill."""
    from services.billing import _handle_confirmation
    from services.pending import get_pending_bill

    pending = _seed_clarification_state(phone, shop_id)
    _handle_confirmation(phone, "yes", "yes", pending, reg, 9)

    still = get_pending_bill(phone)
    assert still is not None
    assert still.awaiting_gst_clarification is True
    assert any("can't confirm" in body.lower() for _, body in sent)


def test_confirmation_items_during_clarification_delegates(
    phone, shop_id, reg, sent, monkeypatch,
):
    """Generic text while awaiting clarification routes to the clarifier."""
    from services.billing import _handle_confirmation
    from services.pending import get_pending_bill

    pending = _seed_clarification_state(phone, shop_id)

    _install_parser(monkeypatch, [_make_item("lamp", 200)])
    _install_gst_resolver(monkeypatch, ok_names={"lamp"})

    _handle_confirmation(phone, "lamp 200", "lamp 200", pending, reg, 9)

    final = get_pending_bill(phone)
    assert final.awaiting_gst_clarification is False
    assert any(i["name"] == "lamp" for i in final.items)
