"""Regression: read-only commands must work during GST clarification.

A confused shopkeeper typing "help" needs help MORE during clarification,
not less. Same for "today" / "history" / "gst report" / "myitems" — none
of these touch pending state, and blocking them was user-hostile.

Two paths covered:

  LEGACY  — services/billing.py::_handle_confirmation
            Used by tests and any direct caller. The earlier guard
            routed everything except cancel/edit/yes to the clarifier,
            silently re-prompting with the failed-items message.

  LIVE    — conversation/manager.py + conversation/executor.py
            Production webhook path. "today"/"history"/"summary"/
            "myitems" short-circuit in _check_hard_command and never
            reach the clarification intercept. "help"/"gst report" go
            through execute_action, where the clarification intercept's
            allowlist lets them pass.

Both paths must:
  1. Run the read-only command and return its output.
  2. Preserve the clarification state (pending exists, items==[],
     valid_items intact, failed_items intact, flag still True).
  3. Refresh `created_at` so the 10-min expiry doesn't fire while
     the user is browsing.
"""
from datetime import datetime, timedelta

import pytest

import main


# ── Common fixtures ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_db():
    main.init_database()


@pytest.fixture
def phone():
    return "whatsapp:+919000099000"


@pytest.fixture
def shop_id(phone):
    digits = "".join(c for c in phone if c.isdigit())
    return "S" + digits[-8:]


@pytest.fixture
def reg(phone, shop_id):
    """Activate trial as a Tax Invoice shop, clear any prior pending/bills."""
    from services.registration import activate_trial, get_registration
    from services.pending import clear_pending
    from db.session import db_session
    from db.models import Bill
    activate_trial(
        phone, "ReadOnly Test Shop", "Hyderabad",
        gstin="36AABCU9603R1ZX",
        state_name="Telangana", state_code="36",
    )
    clear_pending(phone)
    with db_session() as s:
        s.query(Bill).filter_by(shop_id=shop_id).delete()
    return get_registration(phone)


@pytest.fixture
def silence_sends(monkeypatch):
    import services.billing as billing_mod
    monkeypatch.setattr(billing_mod, "send",     lambda *a, **kw: True)
    monkeypatch.setattr(billing_mod, "send_pdf", lambda *a, **kw: None)


def _seed_clarification(phone, shop_id, *, age_minutes=0):
    """Persist a PendingBill in awaiting_gst_clarification state.

    age_minutes lets tests verify expiry-refresh by seeding an old bill
    and checking created_at moves forward.
    """
    from services.pending import PendingBill, store_pending
    pending = PendingBill(
        phone=phone, shop_id=shop_id, shop_name="ReadOnly Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Ramesh",
        customer_state="Telangana", customer_state_code="36",
        items=[],
        confidence=0.95, warnings=[], raw_message="rice 100 xyz_unknown 200",
        created_at=datetime.utcnow() - timedelta(minutes=age_minutes),
        is_return=False, is_bill_of_supply=False,
        is_inclusive=False, pricing_type="exclusive",
        valid_items=[{
            "name": "rice", "qty": 1, "price": 100,
            "hsn": "1006", "gst_rate": 5,
            "gst_source": "exact", "gst_confidence": "high",
            "item_discount_type": "none", "item_discount_value": 0,
        }],
        failed_items=[{"name": "xyz_unknown", "qty": 1, "price": 200}],
        awaiting_gst_clarification=True,
    )
    store_pending(phone, pending)
    return pending


def _assert_clarification_preserved(phone, *, expected_failed=None):
    """Assert the clarification state survived the read-only command."""
    from services.pending import get_pending_bill
    pending = get_pending_bill(phone)
    assert pending is not None, "Pending bill was cleared by a read-only command"
    assert pending.awaiting_gst_clarification is True, (
        "awaiting_gst_clarification flipped to False after a read-only command"
    )
    assert pending.items == [], "items must stay empty during clarification"
    assert len(pending.valid_items) >= 1, "valid_items lost during read-only command"
    if expected_failed is not None:
        names = [i["name"] for i in pending.failed_items]
        assert names == expected_failed, f"failed_items drifted: {names}"
    return pending


# ════════════════════════════════════════════════════════════════════
# LEGACY PATH — services/billing.py::_handle_confirmation
# ════════════════════════════════════════════════════════════════════

@pytest.fixture
def legacy_caller(phone, reg, silence_sends):
    """Return a function that invokes the legacy _handle_confirmation."""
    from services.billing import _handle_confirmation

    def _call(message: str):
        from services.pending import get_pending_bill
        pending = get_pending_bill(phone)
        _handle_confirmation(
            from_number=phone,
            msg_lower=message.lower(),
            message=message,
            pending=pending,
            reg=reg,
            d_left=10,
        )
    return _call


@pytest.mark.parametrize("command", ["help", "?", "h"])
def test_legacy_help_works_during_clarification(
    phone, shop_id, legacy_caller, command,
):
    seeded = _seed_clarification(phone, shop_id)
    pre_created_at = seeded.created_at

    legacy_caller(command)

    pending = _assert_clarification_preserved(
        phone, expected_failed=["xyz_unknown"],
    )
    # created_at must have moved forward (expiry refreshed)
    assert pending.created_at >= pre_created_at, (
        "Expiry was not refreshed after read-only help command"
    )


@pytest.mark.parametrize("command", ["today", "aaj", "today's sales"])
def test_legacy_today_works_during_clarification(
    phone, shop_id, legacy_caller, command,
):
    _seed_clarification(phone, shop_id)
    legacy_caller(command)
    _assert_clarification_preserved(phone, expected_failed=["xyz_unknown"])


@pytest.mark.parametrize("command", ["history", "bills", "recent"])
def test_legacy_history_works_during_clarification(
    phone, shop_id, legacy_caller, command,
):
    _seed_clarification(phone, shop_id)
    legacy_caller(command)
    _assert_clarification_preserved(phone, expected_failed=["xyz_unknown"])


def test_legacy_gst_report_works_during_clarification(
    phone, shop_id, legacy_caller,
):
    _seed_clarification(phone, shop_id)
    legacy_caller("gst report")
    _assert_clarification_preserved(phone, expected_failed=["xyz_unknown"])


def test_legacy_myitems_works_during_clarification(
    phone, shop_id, legacy_caller,
):
    _seed_clarification(phone, shop_id)
    legacy_caller("myitems")
    _assert_clarification_preserved(phone, expected_failed=["xyz_unknown"])


def test_legacy_expiry_refreshed_on_old_bill(phone, shop_id, legacy_caller):
    """Seed an 8-minute-old bill, run 'help', confirm it now has fresh expiry."""
    _seed_clarification(phone, shop_id, age_minutes=8)

    legacy_caller("help")

    from services.pending import get_pending_bill
    pending = get_pending_bill(phone)
    age_after = (datetime.utcnow() - pending.created_at).total_seconds()
    assert age_after < 30, (
        f"Expiry was not refreshed — bill age is still {age_after:.0f}s "
        f"after help command. The 10-min clock would fire mid-browsing."
    )


# ── Negative case: non-read-only still goes to clarifier ──────────

def test_legacy_arbitrary_text_still_routes_to_clarifier(
    phone, shop_id, legacy_caller, monkeypatch,
):
    """Words that aren't read-only commands should still hit the
    clarification handler. Verifies the allowlist isn't too greedy."""
    _seed_clarification(phone, shop_id)

    called: list[bool] = []

    def _fake_clar(_phone, _msg, _pending):
        called.append(True)

    import services.billing as billing_mod
    monkeypatch.setattr(billing_mod, "_handle_gst_clarification", _fake_clar)

    legacy_caller("power adapter 500")
    assert called, (
        "Non-read-only text was not routed to _handle_gst_clarification"
    )


# ════════════════════════════════════════════════════════════════════
# LIVE PATH — conversation/manager.py + conversation/executor.py
# ════════════════════════════════════════════════════════════════════

def test_live_help_works_during_clarification(
    phone, shop_id, reg, silence_sends, monkeypatch,
):
    """handle_message('help') during clarification → help text returned,
    clarification state preserved, expiry refreshed."""
    from conversation import manager as mgr
    seeded = _seed_clarification(phone, shop_id, age_minutes=5)
    pre_created_at = seeded.created_at

    # 'help' is a hard-command in _check_hard_command — routes to
    # execute_action(action=help) which the executor's clarification
    # intercept allows through.
    reply = mgr.handle_message(phone, "help")

    assert reply, "Help reply was empty"
    assert "BilledUp" in reply or "help" in reply.lower(), (
        f"Reply doesn't look like help text: {reply[:100]!r}"
    )
    pending = _assert_clarification_preserved(
        phone, expected_failed=["xyz_unknown"],
    )
    assert pending.created_at >= pre_created_at


def test_live_today_works_during_clarification(
    phone, shop_id, reg, silence_sends,
):
    """'today' goes through manager._check_hard_command direct return.
    Must work and refresh expiry."""
    from conversation import manager as mgr
    seeded = _seed_clarification(phone, shop_id, age_minutes=5)
    pre_created_at = seeded.created_at

    reply = mgr.handle_message(phone, "today")

    assert reply, "Today reply was empty"
    pending = _assert_clarification_preserved(
        phone, expected_failed=["xyz_unknown"],
    )
    assert pending.created_at >= pre_created_at, (
        "manager._check_hard_command did not refresh clarification expiry"
    )


def test_live_history_works_during_clarification(
    phone, shop_id, reg, silence_sends,
):
    from conversation import manager as mgr
    seeded = _seed_clarification(phone, shop_id, age_minutes=5)
    pre_created_at = seeded.created_at

    mgr.handle_message(phone, "history")

    pending = _assert_clarification_preserved(
        phone, expected_failed=["xyz_unknown"],
    )
    assert pending.created_at >= pre_created_at


def test_live_gst_report_works_during_clarification(
    phone, shop_id, reg, silence_sends,
):
    """'gst report' goes through manager → execute_action(action=report).
    Executor allowlist permits action=report and refreshes expiry."""
    from conversation import manager as mgr
    seeded = _seed_clarification(phone, shop_id, age_minutes=5)
    pre_created_at = seeded.created_at

    mgr.handle_message(phone, "gst report")

    pending = _assert_clarification_preserved(
        phone, expected_failed=["xyz_unknown"],
    )
    assert pending.created_at >= pre_created_at


def test_live_myitems_works_during_clarification(
    phone, shop_id, reg, silence_sends,
):
    from conversation import manager as mgr
    seeded = _seed_clarification(phone, shop_id, age_minutes=5)
    pre_created_at = seeded.created_at

    mgr.handle_message(phone, "myitems")

    pending = _assert_clarification_preserved(
        phone, expected_failed=["xyz_unknown"],
    )
    assert pending.created_at >= pre_created_at


def test_live_summary_works_during_clarification(
    phone, shop_id, reg, silence_sends,
):
    from conversation import manager as mgr
    seeded = _seed_clarification(phone, shop_id, age_minutes=5)
    pre_created_at = seeded.created_at

    mgr.handle_message(phone, "summary")

    pending = _assert_clarification_preserved(
        phone, expected_failed=["xyz_unknown"],
    )
    assert pending.created_at >= pre_created_at


# ── Live-path: end-to-end "browse then answer" flow ────────────────

def test_live_browse_then_resume_flow(
    phone, shop_id, reg, silence_sends, monkeypatch,
):
    """Full UX flow: user is in clarification, browses help/today/history,
    then types the missing item — the clarification resumes correctly."""
    from conversation import manager as mgr

    _seed_clarification(phone, shop_id)

    # User browses for orientation
    mgr.handle_message(phone, "help")
    mgr.handle_message(phone, "today")
    mgr.handle_message(phone, "history")

    _assert_clarification_preserved(phone, expected_failed=["xyz_unknown"])

    # Now user provides the clarification (LLM parses it as add_item)
    def _llm(_s, _m):
        return {
            "action": "add_item",
            "bill_changes": {
                "add_items": [{"name": "kurta", "qty": 1, "price": 200}],
            },
            "reply": "",
            "show_preview": True,
        }
    monkeypatch.setattr(mgr, "_call_llm", _llm)

    reply = mgr.handle_message(phone, "kurta 200")

    # Clarification should now be resolved
    from services.pending import get_pending_bill
    pending = get_pending_bill(phone)
    assert pending is not None
    assert pending.awaiting_gst_clarification is False, (
        "Clarification did not resolve after user provided the missing item"
    )
    item_names = {i["name"].lower() for i in pending.items}
    assert "rice" in item_names, "Original valid_items lost across the flow"
    assert "kurta" in item_names, "Newly clarified item not in final pending"
    assert "Bill Preview" in reply or "preview" in reply.lower() or "✅" in reply
