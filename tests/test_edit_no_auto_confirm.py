"""Regression: edit commands during bill preview must NOT auto-generate
the bill. The LLM may classify mixed edits ("Add kurta 500 remove pant
600") as `confirm_with_change`, and the executor used to call
_handle_confirm at the end of that path — silently finalizing bills the
user never explicitly approved. The fix: _handle_confirm_with_change
applies all changes and returns the updated preview; only an explicit
YES/CONFIRM generates the bill.
"""
from datetime import datetime

import pytest

import main


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_db():
    main.init_database()


@pytest.fixture
def phone():
    return "whatsapp:+919000088000"


@pytest.fixture
def shop_id(phone):
    digits = "".join(c for c in phone if c.isdigit())
    return "S" + digits[-8:]


@pytest.fixture
def reg(phone, shop_id):
    """Activate trial as a Tax Invoice shop (with GSTIN).

    Cleans up any prior bills + pending for this phone so the
    duplicate-bill guard (same shop + raw_message within 60s) can't
    cross-contaminate tests that all run against the same fixture.
    """
    from services.registration import activate_trial
    from services.pending import clear_pending
    from db.session import db_session
    from db.models import Bill
    activate_trial(
        phone, "Edit Test Boutique", "Hyderabad",
        gstin="36AABCU9603R1ZX",
        state_name="Telangana", state_code="36",
    )
    clear_pending(phone)
    with db_session() as s:
        s.query(Bill).filter_by(shop_id=shop_id).delete()


@pytest.fixture
def silence_sends(monkeypatch):
    """Stub WhatsApp Graph API calls so the test never hits the network."""
    import services.billing as billing_mod
    monkeypatch.setattr(billing_mod, "send",     lambda *a, **kw: True)
    monkeypatch.setattr(billing_mod, "send_pdf", lambda *a, **kw: None)


def _seed_pending(phone, shop_id, items):
    """Persist a normal-state pending bill (no clarification flags)."""
    from services.pending import PendingBill, store_pending
    pending = PendingBill(
        phone=phone, shop_id=shop_id, shop_name="Edit Test Boutique",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Ramesh",
        customer_state="Telangana", customer_state_code="36",
        items=items,
        confidence=1.0, warnings=[], raw_message="seed",
        created_at=datetime.utcnow(),
        is_return=False, is_bill_of_supply=False,
        is_inclusive=False, pricing_type="exclusive",
    )
    store_pending(phone, pending)
    return pending


# ── The headline test the user asked for ──────────────────────────

def test_edit_then_confirm_two_step_flow(
    phone, shop_id, reg, silence_sends, monkeypatch,
):
    """Full flow: user sees preview, sends mixed-edit command, sees
    UPDATED preview (no bill generated), then types YES → bill generates.

    Asserts:
      1. After edit: NO Bill row created, pending still exists, the
         updated preview is returned with the CONFIRM/EDIT/CANCEL options.
      2. After YES: Bill row exists, pending is cleared.
    """
    from conversation import manager as mgr
    from db.session import db_session
    from db.models import Bill
    from services.pending import get_pending_bill

    # Pre-seed pending bill with [shirt 500, pant 600]
    _seed_pending(phone, shop_id, items=[
        {"name": "shirt", "qty": 1, "price": 500,
         "hsn": "6205", "gst_rate": 5,
         "gst_source": "exact", "gst_confidence": "high",
         "item_discount_type": "none", "item_discount_value": 0},
        {"name": "pant", "qty": 1, "price": 600,
         "hsn": "6203", "gst_rate": 5,
         "gst_source": "exact", "gst_confidence": "high",
         "item_discount_type": "none", "item_discount_value": 0},
    ])

    with db_session() as s:
        bills_before = s.query(Bill).filter_by(shop_id=shop_id).count()

    # ── Round 1: mixed-edit message → LLM classifies as confirm_with_change ──
    # This is the exact scenario from the bug report. Pre-fix, this would
    # have generated a bill immediately.
    def _llm_returns_confirm_with_change(_system, _messages):
        return {
            "action": "confirm_with_change",
            "bill_changes": {
                "add_items":    [{"name": "kurta", "qty": 1, "price": 500}],
                "remove_item":  "pant",
                "update_items": [],
            },
            "reply": "",
            "show_preview": True,
        }
    monkeypatch.setattr(mgr, "_call_llm", _llm_returns_confirm_with_change)

    edit_reply = mgr.handle_message(phone, "Add kurta 500 remove pant 600")

    # No bill must have been generated.
    with db_session() as s:
        bills_mid = s.query(Bill).filter_by(shop_id=shop_id).count()
    assert bills_mid == bills_before, (
        f"Bill was generated on edit command — expected NO bill until "
        f"explicit YES/CONFIRM. (before={bills_before}, after={bills_mid})"
    )

    # Pending must still exist with the edited items reflected.
    pending = get_pending_bill(phone)
    assert pending is not None, "Pending bill was cleared after edit — should still exist"
    assert pending.awaiting_gst_clarification is False, "Unexpected clarification state"
    item_names = {i["name"].lower() for i in pending.items}
    assert "kurta" in item_names, "kurta was not added by the edit"
    assert "pant" not in item_names, "pant was not removed by the edit"
    assert "shirt" in item_names, "shirt should still be in the bill"

    # The reply must look like a preview (carries the YES/CANCEL options).
    assert "CONFIRM" in edit_reply or "yes" in edit_reply.lower(), (
        f"Reply did not include the CONFIRM option. Reply was:\n{edit_reply!r}"
    )
    assert "CANCEL" in edit_reply, "Reply did not include CANCEL option"

    # ── Round 2: user types YES → bill generates ──
    def _llm_returns_confirm(_system, _messages):
        return {
            "action": "confirm",
            "bill_changes": {},
            "reply": "",
            "show_preview": False,
        }
    monkeypatch.setattr(mgr, "_call_llm", _llm_returns_confirm)

    mgr.handle_message(phone, "yes")

    # Bill row must now exist.
    with db_session() as s:
        bills_after = s.query(Bill).filter_by(shop_id=shop_id).count()
    assert bills_after == bills_before + 1, (
        f"YES did not generate a bill. (before_edit={bills_before}, "
        f"after_edit={bills_mid}, after_yes={bills_after})"
    )

    # Pending should be cleared after confirmation.
    assert get_pending_bill(phone) is None, (
        "Pending bill was not cleared after successful confirmation"
    )


# ── Companion unit test: handler-level proof ──────────────────────

def test_confirm_with_change_handler_does_not_auto_confirm(
    phone, shop_id, reg, silence_sends,
):
    """Direct call to _handle_confirm_with_change must apply the edits
    and return a preview, never invoke _generate_confirmed_bill."""
    from conversation.executor import _handle_confirm_with_change
    from conversation.context import load_shop_context
    from db.session import db_session
    from db.models import Bill
    from services.pending import get_pending_bill

    _seed_pending(phone, shop_id, items=[
        {"name": "shirt", "qty": 1, "price": 500,
         "hsn": "6205", "gst_rate": 5,
         "gst_source": "exact", "gst_confidence": "high",
         "item_discount_type": "none", "item_discount_value": 0},
        {"name": "pant", "qty": 1, "price": 600,
         "hsn": "6203", "gst_rate": 5,
         "gst_source": "exact", "gst_confidence": "high",
         "item_discount_type": "none", "item_discount_value": 0},
    ])

    with db_session() as s:
        bills_before = s.query(Bill).filter_by(shop_id=shop_id).count()

    ctx = load_shop_context(phone)
    bill_changes = {
        "add_items":   [{"name": "kurta", "qty": 1, "price": 500}],
        "remove_item": "pant",
    }
    reply = _handle_confirm_with_change(phone, bill_changes, ctx, reply="")

    # No bill should have been generated.
    with db_session() as s:
        bills_after = s.query(Bill).filter_by(shop_id=shop_id).count()
    assert bills_after == bills_before, (
        "_handle_confirm_with_change auto-confirmed despite the fix"
    )

    # Pending must still hold the edited items.
    pending = get_pending_bill(phone)
    assert pending is not None
    names = {i["name"].lower() for i in pending.items}
    assert "kurta" in names and "pant" not in names

    # Reply must summarize the change AND include the preview options.
    assert "added" in reply.lower() or "kurta" in reply.lower()
    assert "removed" in reply.lower() or "pant" in reply
    assert "CONFIRM" in reply, "Updated preview missing CONFIRM option"
    assert "CANCEL" in reply, "Updated preview missing CANCEL option"


# ── Regression guard: explicit "yes but ..." still works ──────────

def test_explicit_confirm_with_change_via_yes_still_generates_bill(
    phone, shop_id, reg, silence_sends, monkeypatch,
):
    """Sanity: when the user genuinely says "yes" + change in ONE message,
    the user is expected to follow up with a separate YES under the new
    UX rule. The first message applies the change and shows preview;
    the second YES generates the bill. This documents the trade-off."""
    from conversation import manager as mgr
    from db.session import db_session
    from db.models import Bill

    _seed_pending(phone, shop_id, items=[
        {"name": "shirt", "qty": 1, "price": 500,
         "hsn": "6205", "gst_rate": 5,
         "gst_source": "exact", "gst_confidence": "high",
         "item_discount_type": "none", "item_discount_value": 0},
    ])

    with db_session() as s:
        bills_before = s.query(Bill).filter_by(shop_id=shop_id).count()

    # User says "yes but add kurta 500" → LLM picks confirm_with_change.
    def _llm(_s, _m):
        return {
            "action": "confirm_with_change",
            "bill_changes": {"add_items": [{"name": "kurta", "qty": 1, "price": 500}]},
            "reply": "",
            "show_preview": True,
        }
    monkeypatch.setattr(mgr, "_call_llm", _llm)
    mgr.handle_message(phone, "yes but add kurta 500")

    # Even with explicit "yes" word, the change is applied and preview
    # shown — bill is NOT generated until a second YES.
    with db_session() as s:
        bills_mid = s.query(Bill).filter_by(shop_id=shop_id).count()
    assert bills_mid == bills_before, (
        "First message should not have generated a bill — explicit "
        "second YES is required under the new UX rule"
    )

    # Now an explicit second YES does the trick.
    def _llm_yes(_s, _m):
        return {"action": "confirm", "bill_changes": {}, "reply": "",
                "show_preview": False}
    monkeypatch.setattr(mgr, "_call_llm", _llm_yes)
    mgr.handle_message(phone, "yes")

    with db_session() as s:
        bills_after = s.query(Bill).filter_by(shop_id=shop_id).count()
    assert bills_after == bills_before + 1
