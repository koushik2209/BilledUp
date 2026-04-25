"""Regression test for the manager.py hard-command guard.

When a PendingBill is in awaiting_gst_clarification=True state, the
"yes" / "edit" hard-command shortcuts must NOT short-circuit — they
must fall through to LLM routing so the executor's clarification
intercept can handle them. Otherwise "yes" would generate a zero-item
bill because pending.items == [] during clarification.
"""
from datetime import datetime

import pytest

import main


@pytest.fixture
def phone():
    return "whatsapp:+919000055555"


@pytest.fixture
def shop_id(phone):
    digits = "".join(c for c in phone if c.isdigit())
    return "S" + digits[-8:]


@pytest.fixture
def reg(phone):
    """Activate trial + clear any pending."""
    from services.registration import activate_trial
    from services.pending import clear_pending
    main.init_database()
    activate_trial(
        phone, "Guard Test Shop", "Hyderabad",
        gstin="36AABCU9603R1ZX",
        state_name="Telangana", state_code="36",
    )
    clear_pending(phone)
    return phone


def _seed_clarification(phone, shop_id):
    """Persist a PendingBill already in clarification state."""
    from services.pending import PendingBill, store_pending
    pending = PendingBill(
        phone=phone, shop_id=shop_id, shop_name="Guard Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Ramesh",
        customer_state="Telangana", customer_state_code="36",
        items=[],
        confidence=0.95, warnings=[], raw_message="rice 100 xyz 200",
        created_at=datetime.utcnow(),
        is_return=False, is_bill_of_supply=False,
        is_inclusive=False, pricing_type="exclusive",
        valid_items=[{
            "name": "rice", "qty": 1, "price": 100,
            "hsn": "1006", "gst_rate": 5,
            "gst_source": "exact", "gst_confidence": "high",
            "item_discount_type": "none", "item_discount_value": 0,
        }],
        failed_items=[{"name": "xyz", "qty": 1, "price": 200}],
        awaiting_gst_clarification=True,
    )
    store_pending(phone, pending)
    return pending


def test_yes_during_clarification_does_not_create_bill(phone, shop_id, reg, monkeypatch):
    """Full-path regression: 'yes' while awaiting clarification must NOT
    generate a zero-item bill. Manager guard returns None → LLM is called
    → LLM classifies 'yes' as confirm → executor intercept blocks with
    the 'Can't confirm yet' nudge. PendingBill state is preserved."""
    from conversation import manager as mgr
    from db.session import db_session
    from db.models import Bill
    from services.pending import get_pending_bill

    _seed_clarification(phone, shop_id)

    with db_session() as s:
        bill_count_before = s.query(Bill).filter_by(shop_id=shop_id).count()

    # Mock the LLM so the test does not touch the network.
    # The LLM would naturally classify "yes" as action=confirm, mirroring
    # what _CONFIRM_WORDS would have done synchronously.
    def _fake_llm(_system, _messages):
        return {
            "action": "confirm",
            "bill_changes": {},
            "reply": "",
            "show_preview": False,
        }
    monkeypatch.setattr(mgr, "_call_llm", _fake_llm)

    reply = mgr.handle_message(phone, "yes")

    # Executor intercept must have produced the nudge.
    assert "can't confirm yet" in reply.lower(), (
        f"Expected the executor's clarification block message, got: {reply!r}"
    )
    # The outstanding item must be referenced so the user knows what's missing.
    assert "xyz" in reply

    # No Bill row was created.
    with db_session() as s:
        bill_count_after = s.query(Bill).filter_by(shop_id=shop_id).count()
    assert bill_count_after == bill_count_before, (
        "A Bill row was generated despite clarification state — guard failed"
    )

    # PendingBill state preserved across the round-trip.
    still = get_pending_bill(phone)
    assert still is not None
    assert still.awaiting_gst_clarification is True
    assert [i["name"] for i in still.failed_items] == ["xyz"]
    assert [i["name"] for i in still.valid_items] == ["rice"]


def test_check_hard_command_returns_none_for_yes_during_clarification(
    phone, shop_id, reg,
):
    """Lower-level assertion that the guard returns None (not a reply
    string) so manager.handle_message proceeds to the LLM call."""
    from conversation.manager import _check_hard_command
    from conversation.context import load_shop_context

    _seed_clarification(phone, shop_id)
    ctx = load_shop_context(phone)

    assert _check_hard_command("yes", phone, ctx) is None
    assert _check_hard_command("ok", phone, ctx) is None


def test_check_hard_command_returns_none_for_edit_during_clarification(
    phone, shop_id, reg,
):
    """Edit must also fall through during clarification so the executor
    intercept handles it consistently — _handle_edit's direct
    clear_pending would silently destroy the partial bill."""
    from conversation.manager import _check_hard_command
    from conversation.context import load_shop_context

    _seed_clarification(phone, shop_id)
    ctx = load_shop_context(phone)

    assert _check_hard_command("edit", phone, ctx) is None
    assert _check_hard_command("redo", phone, ctx) is None


def test_check_hard_command_yes_still_works_without_clarification(
    phone, shop_id, reg, monkeypatch,
):
    """Regression guard: with NO clarification state, 'yes' must still
    short-circuit through execute_action (no LLM call needed). Verifies
    the guard doesn't break the normal happy path."""
    from conversation.manager import _check_hard_command
    from conversation.context import load_shop_context
    from services.pending import PendingBill, store_pending

    # Suppress real WhatsApp Graph API calls — _handle_confirm will try
    # to send messages and a PDF; we only care about the routing result.
    import services.billing as billing_mod
    monkeypatch.setattr(billing_mod, "send",     lambda *a, **kw: True)
    monkeypatch.setattr(billing_mod, "send_pdf", lambda *a, **kw: None)

    # Normal pending bill — items populated, no clarification flag.
    pending = PendingBill(
        phone=phone, shop_id=shop_id, shop_name="Guard Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Ramesh",
        customer_state="Telangana", customer_state_code="36",
        items=[{
            "name": "rice", "qty": 1, "price": 100,
            "hsn": "1006", "gst_rate": 5,
            "gst_source": "exact", "gst_confidence": "high",
            "item_discount_type": "none", "item_discount_value": 0,
        }],
        confidence=1.0, warnings=[], raw_message="rice 100",
        created_at=datetime.utcnow(),
    )
    store_pending(phone, pending)
    ctx = load_shop_context(phone)

    # _check_hard_command should NOT return None — it should route to
    # execute_action(action=confirm) and produce a non-None reply.
    result = _check_hard_command("yes", phone, ctx)
    assert result is not None, (
        "Guard incorrectly fell through outside clarification state"
    )
