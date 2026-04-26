"""Regression: BilledUp must never refuse an item based on shop type.

Scenario: a "clothing" shop (detected from top_items) sends "clutch plates 500".
The system must NOT reply with a category-restriction message. The expected
outcomes are:
  (a) bill is created / preview shown, OR
  (b) GST clarification is triggered for the unknown item.

Either way, the reply must NOT contain words like "clothing", "not in my system",
"not supported", or "did you mean a clothing item".

The test bypasses the live LLM by monkey-patching _call_llm in manager.py so it
returns action=billing — the most natural LLM response when a shopkeeper sends
item names with prices. We then verify the executor produces a bill preview or
GST-clarification prompt, not a category rejection.
"""
from datetime import datetime

import pytest

import main


@pytest.fixture(autouse=True)
def fresh_db():
    main.init_database()


@pytest.fixture
def phone():
    return "whatsapp:+919000055100"


@pytest.fixture
def shop_id(phone):
    digits = "".join(c for c in phone if c.isdigit())
    return "S" + digits[-8:]


@pytest.fixture
def reg(phone, shop_id):
    """Activate a clothing-type shop (seeded with shirt/kurta items) so
    _detect_shop_type() returns 'clothing' — the worst-case scenario for
    triggering the category-restriction bug."""
    from services.registration import activate_trial
    from services.pending import clear_pending
    from db.session import db_session
    from db.models import ShopItemMaster

    activate_trial(
        phone, "Ravi Garments", "Hyderabad",
        gstin="36AABCU9603R1ZX",
        state_name="Telangana", state_code="36",
    )
    clear_pending(phone)

    # Seed clothing items so shop_type is detected as "clothing"
    with db_session() as s:
        for name, hsn, gst in [("shirt", "6205", 5), ("kurta", "6211", 5)]:
            s.query(ShopItemMaster).filter_by(shop_id=shop_id, item_name=name).delete()
            s.add(ShopItemMaster(
                shop_id=shop_id, item_name=name,
                hsn=hsn, gst_rate=gst, confirmed=True, use_count=10,
            ))

    return phone


@pytest.fixture
def silence_sends(monkeypatch):
    import services.billing as billing_mod
    monkeypatch.setattr(billing_mod, "send",     lambda *a, **kw: True)
    monkeypatch.setattr(billing_mod, "send_pdf", lambda *a, **kw: None)


_REJECTION_PHRASES = [
    "not in my system",
    "built for clothing",
    "clothing shop",
    "did you mean a clothing",
    "not supported",
    "only bill",
    "shirts, kurtas",
    "kurtas, pants",
    "clothing items",
    "add a custom item",
    "I can bill",
    "I don't have",
]


# ════════════════════════════════════════════════════════════════════
# TEST 1 — LLM classifies clutch plates as billing → no rejection
# ════════════════════════════════════════════════════════════════════

def test_clutch_plates_no_category_rejection(phone, reg, silence_sends, monkeypatch):
    """Full-path: 'clutch plates 500' on a clothing shop.

    LLM is mocked to return action=billing (the expected classification).
    The executor must produce a bill preview or GST-clarification, NOT a
    category-rejection message.
    """
    from conversation import manager as mgr

    def _llm_billing(_system, _messages):
        return {
            "action": "billing",
            "bill_changes": {
                "add_items": [{"name": "clutch plates", "price": 500.0, "qty": 1}],
            },
            "reply": "",
            "show_preview": True,
        }

    monkeypatch.setattr(mgr, "_call_llm", _llm_billing)

    reply = mgr.handle_message(phone, "clutch plates 500")

    reply_lower = reply.lower()
    for phrase in _REJECTION_PHRASES:
        assert phrase.lower() not in reply_lower, (
            f"Category-restriction phrase found in reply: {phrase!r}\n"
            f"Full reply: {reply!r}"
        )

    # Reply must indicate a bill is in progress OR GST clarification was triggered
    is_bill_preview = (
        "preview" in reply_lower
        or "clutch" in reply_lower
        or "₹500" in reply
        or "Rs.500" in reply
        or "confirm" in reply_lower
        or "yes" in reply_lower
    )
    is_gst_clarification = (
        "gst" in reply_lower and (
            "rate" in reply_lower or "%" in reply or "clarif" in reply_lower
        )
    )
    assert is_bill_preview or is_gst_clarification, (
        f"Reply is neither a bill preview nor a GST clarification prompt.\n"
        f"Full reply: {reply!r}"
    )


# ════════════════════════════════════════════════════════════════════
# TEST 2 — Prompt text does NOT contain category-restriction language
# ════════════════════════════════════════════════════════════════════

def test_system_prompt_has_no_category_restriction():
    """The LLM system prompt must explicitly state BilledUp works for
    any item, and must NOT use language that implies clothing-only."""
    from conversation.prompt import build_system_prompt
    from conversation.context import ShopContext

    ctx = ShopContext(
        phone="whatsapp:+919000055100",
        shop_name="Ravi Garments",
        owner_name="Ravi",
        shop_type="clothing",
        state="Telangana",
        state_code="36",
        gstin="36AABCU9603R1ZX",
        default_pricing="exclusive",
        default_bill_type="",
        language="en",
        conversation_history="",
        pending_bill=None,
        pending_bill_age_mins=0,
        last_bill=None,
        top_items=[],
        frequent_customers=[],
        bills_today=0,
        total_bills=0,
        trial_active=True,
        trial_days_left=10,
        is_new_user=False,
        is_power_user=False,
    )

    prompt = build_system_prompt(ctx)
    prompt_lower = prompt.lower()

    # Must contain an explicit "any" / general-purpose declaration
    assert "any" in prompt_lower or "general" in prompt_lower, (
        "Prompt does not contain a general-purpose declaration"
    )
    assert "never refuse" in prompt_lower or "never restrict" in prompt_lower or "rule 13" in prompt_lower, (
        "Prompt is missing RULE 13 (category-restriction ban)"
    )

    # Shop type must be marked as informational
    assert "informational" in prompt_lower, (
        "shop_type line must be labelled informational in the prompt"
    )


# ════════════════════════════════════════════════════════════════════
# TEST 3 — Non-clothing items work on a clothing shop (no LLM mock)
# ════════════════════════════════════════════════════════════════════

def test_non_clothing_items_accepted_by_billing_handler(
    phone, shop_id, reg, monkeypatch,
):
    """Direct billing handler test (no live Claude API).

    _handle_new_bill is called with mocked parser output containing auto parts.
    The handler must create a pending bill or enter GST clarification —
    NEVER send a category-rejection message.
    """
    import services.billing as billing_mod
    from services.billing import _handle_new_bill
    from services.pending import get_pending_bill

    # Mock the parser so no real API call is made
    def _fake_parse(_msg):
        return {
            "customer_name": "Customer",
            "customer_phone": None,
            "items": [{"name": "clutch plates", "qty": 1, "price": 500.0,
                       "item_discount_type": "none", "item_discount_value": 0}],
            "bill_discount_type": "none",
            "bill_discount_value": 0.0,
            "pricing_type": "exclusive",
            "needs_confirmation": False,
            "confidence": 0.9,
            "warnings": [],
            "notes": "",
            "error": None,
            "parse_time_ms": 0,
        }

    monkeypatch.setattr(billing_mod, "parse_message", _fake_parse)

    sent_bodies: list[str] = []
    monkeypatch.setattr(billing_mod, "send", lambda to, body: sent_bodies.append(body) or True)

    reg_dict = {"address": "Hyderabad", "gstin": "36AABCU9603R1ZX",
                "bills_count": 5}
    _handle_new_bill(
        phone, "clutch plates 500",
        reg_dict, shop_id, "Ravi Garments", d_left=10,
    )

    # Either a pending bill was created or GST clarification was started
    pending = get_pending_bill(phone)
    assert pending is not None, (
        "No pending bill was created — item may have been silently rejected"
    )

    # No sent message must contain category-restriction language
    all_sent = " ".join(sent_bodies).lower()
    for phrase in _REJECTION_PHRASES:
        assert phrase.lower() not in all_sent, (
            f"Category-restriction phrase found in sent message: {phrase!r}\n"
            f"All sent messages: {sent_bodies!r}"
        )
