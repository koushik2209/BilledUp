"""Regression: silent data drop in GST clarification.

Scenario: a bill has [xyz, abc] failing GST. User responds with only
"lamp 200" — both originals are silently replaced. Pre-fix, the final
preview said "All items resolved" with shirt + lamp and gave no signal
that xyz and abc had been dropped. The user could press YES thinking
abc was included.

Fix: at the moment of final resolution, compute originals that weren't
fuzzy-matched by the user's clarification, and prepend a "Not included
(not restated)" warning to the final preview. The user can either
restate the missing items or type YES to confirm without them.

Both paths covered:
  LEGACY  — services/billing.py::_handle_gst_clarification
  LIVE    — conversation/executor.py::_handle_gst_clarification
"""
from datetime import datetime

import pytest

import main


# ── Common fixtures ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_db():
    main.init_database()


@pytest.fixture
def phone():
    return "whatsapp:+919000111222"


@pytest.fixture
def shop_id(phone):
    digits = "".join(c for c in phone if c.isdigit())
    return "S" + digits[-8:]


@pytest.fixture
def reg(phone, shop_id):
    """Activate a Tax Invoice trial; clean any prior pending/bills."""
    from services.registration import activate_trial, get_registration
    from services.pending import clear_pending
    from db.session import db_session
    from db.models import Bill
    activate_trial(
        phone, "Drop Test Shop", "Hyderabad",
        gstin="36AABCU9603R1ZX",
        state_name="Telangana", state_code="36",
    )
    clear_pending(phone)
    with db_session() as s:
        s.query(Bill).filter_by(shop_id=shop_id).delete()
    return get_registration(phone)


@pytest.fixture
def captured_sends(monkeypatch):
    """Capture services.billing.send messages (legacy path uses send())."""
    out: list[tuple[str, str]] = []

    def _fake(to, body):
        out.append((to, body))
        return True

    import services.billing as billing_mod
    monkeypatch.setattr(billing_mod, "send", _fake)
    monkeypatch.setattr(billing_mod, "send_pdf", lambda *a, **kw: None)
    return out


def _seed_clarification(
    phone, shop_id,
    *,
    valid=None,
    failed=None,
    is_return=False, is_bill_of_supply=False,
):
    """Persist a pending bill in awaiting_gst_clarification state."""
    from services.pending import PendingBill, store_pending
    if valid is None:
        valid = [{
            "name": "shirt", "qty": 1, "price": 500,
            "hsn": "6205", "gst_rate": 5,
            "gst_source": "exact", "gst_confidence": "high",
            "item_discount_type": "none", "item_discount_value": 0,
        }]
    if failed is None:
        failed = [
            {"name": "xyz_unknown", "qty": 1, "price": 200},
            {"name": "abc_unknown", "qty": 1, "price": 300},
        ]
    pending = PendingBill(
        phone=phone, shop_id=shop_id, shop_name="Drop Test Shop",
        shop_state="Telangana", shop_state_code="36",
        customer_name="Ramesh",
        customer_state="Telangana", customer_state_code="36",
        items=[],
        confidence=0.95, warnings=[], raw_message="shirt 500 xyz 200 abc 300",
        created_at=datetime.utcnow(),
        is_return=is_return,
        is_bill_of_supply=is_bill_of_supply,
        is_inclusive=False, pricing_type="exclusive",
        valid_items=valid,
        failed_items=failed,
        awaiting_gst_clarification=True,
    )
    store_pending(phone, pending)
    return pending


def _install_parser(monkeypatch, items, **extra):
    import services.billing as billing_mod

    def _fake(_msg):
        return {
            "customer_name":       extra.get("customer_name", "Ramesh"),
            "customer_phone":      None,
            "items":               items,
            "bill_discount_type":  "none",
            "bill_discount_value": 0.0,
            "pricing_type":        "exclusive",
            "needs_confirmation":  False,
            "confidence":          0.95,
            "warnings":            [],
            "notes":               "",
            "error":               extra.get("error"),
            "parse_time_ms":       0,
        }

    monkeypatch.setattr(billing_mod, "parse_message", _fake)


def _install_gst_resolver(monkeypatch, ok_names):
    """Make _resolve_gst_for_item succeed only for names in ok_names."""
    import services.billing as billing_mod
    ok_lower = {n.lower() for n in ok_names}

    def _fake(name, price, shop_id):
        if name.lower() in ok_lower:
            return {"hsn": "9999", "gst": 18,
                    "source": "exact", "confidence": "high"}
        raise billing_mod.GSTLookupError(name)

    monkeypatch.setattr(billing_mod, "_resolve_gst_for_item", _fake)


# ════════════════════════════════════════════════════════════════════
# UNIT — _any_name_match (the fuzzy-match helper)
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("target,candidates,expected", [
    ("xyz",        ["xyz"],                    True),    # equality
    ("xyz",        ["xyzz"],                   True),    # substring (xyz in xyzz)
    ("power adapter", ["power adapter 5w"],    True),    # substring
    ("phone case", ["phone cover"],            True),    # token overlap (phone)
    ("xyz_unknown", ["lamp"],                  False),   # no overlap, no fuzzy
    ("abc_unknown", ["completely different"],  False),
    ("kurta",      ["kurtaa"],                 True),    # rapidfuzz (1-char diff)
    ("",           ["xyz"],                    False),   # empty target
    ("xyz",        [""],                       False),   # empty candidate
    ("xyz",        [],                         False),   # empty list
])
def test_any_name_match_legacy(target, candidates, expected):
    from services.billing import _any_name_match
    assert _any_name_match(target, candidates) is expected


def test_any_name_match_executor_mirror_matches_legacy():
    """The two helpers must behave identically — they're a mirror."""
    from services.billing import _any_name_match as legacy
    from conversation.executor import _any_name_match as live
    cases = [
        ("xyz", ["xyz"]),
        ("xyz", ["lamp"]),
        ("phone case", ["phone cover"]),
        ("kurta", ["kurtaa"]),
        ("", []),
    ]
    for target, candidates in cases:
        assert legacy(target, candidates) == live(target, candidates), (
            f"mirror divergence on ({target!r}, {candidates!r})"
        )


# ════════════════════════════════════════════════════════════════════
# LEGACY PATH — services/billing.py::_handle_gst_clarification
# ════════════════════════════════════════════════════════════════════

def test_legacy_dropped_items_show_warning_and_excluded_from_bill(
    phone, shop_id, reg, captured_sends, monkeypatch,
):
    """The headline scenario: bill has [xyz, abc] failed; user sends only
    'lamp 200'. Both originals must show in the dropped-warning, and
    neither must appear in the final pending.items."""
    from services.billing import _handle_gst_clarification
    from services.pending import get_pending_bill

    pending = _seed_clarification(phone, shop_id)  # failed=[xyz, abc]

    _install_parser(monkeypatch, [
        {"name": "lamp", "qty": 1, "price": 200,
         "item_discount_type": "none", "item_discount_value": 0},
    ])
    _install_gst_resolver(monkeypatch, ok_names={"lamp"})

    _handle_gst_clarification(phone, "lamp 200", pending)

    # Inspect the message sent to the user — must contain the warning AND
    # both dropped item names.
    sent_bodies = [body for _, body in captured_sends]
    final_message = next(
        (b for b in sent_bodies if "All items resolved" in b),
        None,
    )
    assert final_message is not None, (
        f"Final summary not sent. Bodies were: {sent_bodies!r}"
    )
    assert "Not included" in final_message, (
        "Dropped warning missing from final summary"
    )
    assert "xyz_unknown" in final_message
    assert "abc_unknown" in final_message
    assert "YES" in final_message, (
        "Final message must remind user that YES confirms without dropped items"
    )

    # The actual bill items must be [shirt, lamp] — xyz and abc must NOT
    # be in the bill.
    final_pending = get_pending_bill(phone)
    assert final_pending is not None
    assert final_pending.awaiting_gst_clarification is False
    item_names = {i["name"].lower() for i in final_pending.items}
    assert "shirt" in item_names, "Pre-existing valid item lost"
    assert "lamp"  in item_names, "Newly resolved item missing from bill"
    assert "xyz_unknown" not in item_names, (
        "DROPPED item is in the bill — silent data drop bug returned"
    )
    assert "abc_unknown" not in item_names, (
        "DROPPED item is in the bill — silent data drop bug returned"
    )


def test_legacy_no_warning_when_all_originals_restated(
    phone, shop_id, reg, captured_sends, monkeypatch,
):
    """Regression: if the user restates BOTH failed items, no warning."""
    from services.billing import _handle_gst_clarification

    _seed_clarification(phone, shop_id)  # failed=[xyz_unknown, abc_unknown]

    pending_now = None
    from services.pending import get_pending_bill
    pending_now = get_pending_bill(phone)

    _install_parser(monkeypatch, [
        {"name": "xyz_unknown", "qty": 1, "price": 200,
         "item_discount_type": "none", "item_discount_value": 0},
        {"name": "abc_unknown", "qty": 1, "price": 300,
         "item_discount_type": "none", "item_discount_value": 0},
    ])
    _install_gst_resolver(monkeypatch, ok_names={"xyz_unknown", "abc_unknown"})

    _handle_gst_clarification(phone, "xyz 200 abc 300", pending_now)

    final_message = next(
        (b for _, b in captured_sends if "All items resolved" in b),
        None,
    )
    assert final_message is not None
    assert "Not included" not in final_message, (
        "Spurious warning when user restated everything"
    )


def test_legacy_partial_drop_only_lists_actually_dropped(
    phone, shop_id, reg, captured_sends, monkeypatch,
):
    """User restates one of two — warning must list only the missing one."""
    from services.billing import _handle_gst_clarification
    from services.pending import get_pending_bill

    _seed_clarification(phone, shop_id)  # failed=[xyz_unknown, abc_unknown]
    pending_now = get_pending_bill(phone)

    # User restates xyz but not abc
    _install_parser(monkeypatch, [
        {"name": "xyz_unknown", "qty": 1, "price": 200,
         "item_discount_type": "none", "item_discount_value": 0},
    ])
    _install_gst_resolver(monkeypatch, ok_names={"xyz_unknown"})

    _handle_gst_clarification(phone, "xyz 200", pending_now)

    final_message = next(
        b for _, b in captured_sends if "All items resolved" in b
    )
    assert "Not included" in final_message
    assert "abc_unknown" in final_message, (
        "Dropped item missing from warning"
    )
    assert "xyz_unknown" not in final_message.split("Not included")[1].split("\n")[0], (
        "Restated item incorrectly listed as dropped"
    )

    # Bill: shirt + xyz_unknown, NOT abc_unknown
    final = get_pending_bill(phone)
    names = {i["name"].lower() for i in final.items}
    assert names == {"shirt", "xyz_unknown"}


def test_legacy_warning_uses_fuzzy_match_not_exact(
    phone, shop_id, reg, captured_sends, monkeypatch,
):
    """When user restates a slightly different spelling that fuzzy-matches
    the original, no warning. Verifies _any_name_match is doing fuzzy."""
    from services.billing import _handle_gst_clarification

    _seed_clarification(
        phone, shop_id,
        valid=[],
        failed=[{"name": "kurta", "qty": 1, "price": 500}],
    )

    from services.pending import get_pending_bill
    pending_now = get_pending_bill(phone)

    # User types "kurtaa" — typo, fuzzy matches kurta → no warning
    _install_parser(monkeypatch, [
        {"name": "kurtaa", "qty": 1, "price": 500,
         "item_discount_type": "none", "item_discount_value": 0},
    ])
    _install_gst_resolver(monkeypatch, ok_names={"kurtaa"})

    _handle_gst_clarification(phone, "kurtaa 500", pending_now)

    final_message = next(
        b for _, b in captured_sends if "All items resolved" in b
    )
    assert "Not included" not in final_message, (
        "Fuzzy match should have considered kurtaa==kurta — no drop"
    )


# ════════════════════════════════════════════════════════════════════
# LIVE PATH — conversation/executor.py
# ════════════════════════════════════════════════════════════════════

def test_live_dropped_items_show_warning_in_reply(
    phone, shop_id, reg, captured_sends, monkeypatch,
):
    """LIVE path: same scenario via execute_action's clarification
    delegation. Reply must contain the dropped-warning."""
    from conversation.executor import _handle_gst_clarification as live_handler
    from conversation.context import load_shop_context
    from services.pending import get_pending_bill

    _seed_clarification(phone, shop_id)  # failed=[xyz, abc]
    ctx = load_shop_context(phone)

    # The live path receives bill_changes with add_items already parsed
    # by the LLM. Simulate the LLM having extracted "lamp 200".
    bill_changes = {
        "add_items": [{"name": "lamp", "qty": 1, "price": 200}],
    }

    # Stub out the GST resolver for the live path's get_gst_rate_smart.
    # The executor's _resolve_gst_strict raises GSTClarificationNeeded
    # when source=="default"; we want lamp to RESOLVE so the round
    # terminates and the dropped-warning fires.
    from core import gst_rates as gst_rates_mod

    def _fake_smart(name, client=None, shop_id=None):
        return {"hsn": "9999", "gst": 18,
                "source": "exact", "confidence": "high"}

    monkeypatch.setattr(gst_rates_mod, "get_gst_rate_smart", _fake_smart)

    reply = live_handler(phone, bill_changes, ctx)

    assert "All items resolved" in reply
    assert "Not included" in reply
    assert "xyz_unknown" in reply
    assert "abc_unknown" in reply

    # Bill items: shirt + lamp; xyz/abc dropped.
    final = get_pending_bill(phone)
    names = {i["name"].lower() for i in final.items}
    assert "shirt" in names and "lamp" in names
    assert "xyz_unknown" not in names and "abc_unknown" not in names


# ════════════════════════════════════════════════════════════════════
# YES after the warning generates exactly the items shown
# ════════════════════════════════════════════════════════════════════

def test_yes_after_warning_generates_only_shown_items(
    phone, shop_id, reg, captured_sends, monkeypatch,
):
    """End-to-end: dropped warning is shown → user reads it → user types
    YES → bill is generated containing exactly the items in the preview
    (NOT the dropped originals). This is the user's safety check: what
    they see in the warning is what they're agreeing to."""
    from services.billing import _handle_gst_clarification, _handle_confirmation
    from services.pending import get_pending_bill
    from db.session import db_session
    from db.models import Bill

    _seed_clarification(phone, shop_id)
    pending_now = get_pending_bill(phone)

    _install_parser(monkeypatch, [
        {"name": "lamp", "qty": 1, "price": 200,
         "item_discount_type": "none", "item_discount_value": 0},
    ])
    _install_gst_resolver(monkeypatch, ok_names={"lamp"})

    # Round 1: clarification → warning shown, pending exits clarification
    _handle_gst_clarification(phone, "lamp 200", pending_now)

    pending_after_clarification = get_pending_bill(phone)
    assert pending_after_clarification.awaiting_gst_clarification is False
    item_names_in_pending = {
        i["name"].lower() for i in pending_after_clarification.items
    }
    assert "xyz_unknown" not in item_names_in_pending
    assert "abc_unknown" not in item_names_in_pending

    # Round 2: user types YES — bill generates with shirt + lamp ONLY.
    bills_before = 0
    with db_session() as s:
        bills_before = s.query(Bill).filter_by(shop_id=shop_id).count()

    _handle_confirmation(
        from_number=phone, msg_lower="yes", message="yes",
        pending=pending_after_clarification, reg=reg, d_left=10,
    )

    with db_session() as s:
        bills = s.query(Bill).filter_by(shop_id=shop_id).all()
    assert len(bills) == bills_before + 1
    bill = bills[-1]
    import json
    items_json = json.loads(bill.items_json)
    bill_item_names = {i["name"].lower() for i in items_json}
    assert "shirt" in bill_item_names
    assert "lamp"  in bill_item_names
    assert "xyz_unknown" not in bill_item_names, (
        "Final BILL contains a dropped item — warning lied"
    )
    assert "abc_unknown" not in bill_item_names, (
        "Final BILL contains a dropped item — warning lied"
    )
