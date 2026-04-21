"""
conversation.manager — Main Conversation Entry Point
------------------------------------------------------
Called from services/router.py for every message from an ACTIVE shop.

Flow per message:
  1. Guard empty input
  2. Load shop context (DB + pending bill)
  3. Check hard commands — bypass LLM entirely
  4. Build system prompt + messages array
  5. Call LLM  (Gemini 2.0 Flash primary, Claude Haiku fallback)
  6. Validate + normalise LLM result
  7. Execute action via conversation/executor.py
  8. Log outgoing message + return reply string

Callers must check the return value: "" means the underlying handler
already sent its own messages (e.g. PDF generation on confirm).
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

from services.pending import get_pending_bill, clear_pending, store_pending
from services.billing import msg_preview
from services.registration import get_shop_id as _derive_shop_id
from db.item_master import get_top_items

from conversation.context import ShopContext, load_shop_context
from conversation.prompt import build_system_prompt, build_messages
from conversation.executor import execute_action

log = logging.getLogger("billedup.conversation.manager")


# ════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════

_CONFIRM_WORDS = frozenset({
    "yes", "y", "ok", "okay", "confirm", "send", "👍",
    "haan", "ha", "theek",        # Hindi
    "avunu", "sari",               # Telugu
})

_CANCEL_WORDS = frozenset({
    "no", "cancel", "stop", "👎",
    "nahi", "na",                  # Hindi
    "vaddhu", "ledu", "kaadu",     # Telugu
})

_VALID_ACTIONS = frozenset({
    "billing", "add_item", "remove_item", "update_item",
    "confirm", "confirm_with_change", "cancel", "load_last_bill",
    "set_customer", "set_discount", "set_pricing", "set_bill_type",
    "return", "report", "question", "complaint", "greeting",
    "help", "settings", "unknown",
})

_VALID_RANGES = frozenset({"this_month", "last_month", "last_7_days", "today"})


# ════════════════════════════════════════════════
# MAIN ENTRY POINT
# ════════════════════════════════════════════════

def handle_message(phone: str, text: str) -> str:
    """Process one incoming WhatsApp message and return the reply.

    Returns "" when the underlying handler already sent its messages
    (PDF confirmation, GST report) — callers should skip send_text_message.
    """
    # 1. Guard
    text = (text or "").strip()
    if not text:
        return (
            "📱 Please send a billing message.\n"
            "_Example: charger 499 cover 199 for Ramesh_\n"
            "Type *help* for all options."
        )

    # 2. Load context
    try:
        ctx = load_shop_context(phone)
    except Exception as exc:
        log.error(f"load_shop_context failed for {phone}: {exc}", exc_info=True)
        return (
            "⚠️ Could not load your shop data. Please try again.\n"
            "If this keeps happening, type *help* or contact support."
        )

    # 3. Hard commands — no LLM needed
    try:
        hard_reply = _check_hard_command(text, phone, ctx)
        if hard_reply is not None:
            _log_outgoing(phone, hard_reply)
            return hard_reply
    except Exception as exc:
        log.warning(f"Hard command check failed for {phone}: {exc}", exc_info=True)

    # 4. Build system prompt + messages
    try:
        system_prompt = build_system_prompt(ctx)
        messages      = build_messages(ctx, text)
    except Exception as exc:
        log.error(f"Prompt build failed for {phone}: {exc}", exc_info=True)
        return _llm_fallback_reply(ctx)

    # 5. Call LLM
    try:
        raw_result = _call_llm(system_prompt, messages)
    except Exception as exc:
        log.error(f"LLM call failed for {phone}: {exc}", exc_info=True)
        return _llm_fallback_reply(ctx)

    # 6. Validate
    result = _validate_result(raw_result)
    log.info(
        f"LLM action: {result['action']} | "
        f"show_preview={result['show_preview']} | "
        f"phone={phone}"
    )

    # 7. Execute
    try:
        reply = execute_action(result, phone, ctx)
    except Exception as exc:
        log.error(f"execute_action failed for {phone}: {exc}", exc_info=True)
        return "Something went wrong. Please try again or type *help*."

    # 8. Log + return
    _log_outgoing(phone, reply)
    return reply


# ════════════════════════════════════════════════
# HARD COMMAND HANDLER
# ════════════════════════════════════════════════

def _check_hard_command(
    text: str,
    phone: str,
    ctx: ShopContext,
) -> Optional[str]:
    """Route well-known commands without hitting the LLM.

    Returns the reply string (possibly "") if the command was handled,
    or None to signal the message should proceed to the LLM.
    """
    t     = text.strip().lower()
    t_raw = text.strip()

    # ── Confirm ──────────────────────────────────
    if t in _CONFIRM_WORDS:
        return execute_action(_validate_result({"action": "confirm"}), phone, ctx)

    # ── Cancel ───────────────────────────────────
    if t in _CANCEL_WORDS:
        return execute_action(_validate_result({"action": "cancel"}), phone, ctx)

    # ── Edit / redo ───────────────────────────────
    if t in ("edit", "change", "redo"):
        return _handle_edit(phone, ctx)

    # ── Help ─────────────────────────────────────
    if t in ("help", "?", "h"):
        return execute_action(_validate_result({"action": "help"}), phone, ctx)

    # ── My items ─────────────────────────────────
    if t in ("myitems", "my items", "my_items", "items"):
        return _handle_myitems(phone, ctx)

    # ── GST report (with optional range) ─────────
    if t.startswith("gst report") or re.match(r"^report\b", t):
        range_str = _parse_report_range(t)
        return execute_action(
            _validate_result({"action": "report", "report_range": range_str}),
            phone, ctx,
        )

    # ── GST override by index: "GST 1 18" ────────
    gst_idx = re.match(r"^gst\s+(\d+)\s+(\d+)%?$", t)
    if gst_idx:
        return _handle_gst_override(phone, int(gst_idx.group(1)), int(gst_idx.group(2)), ctx)

    # ── GST override by name: "charger gst 18" ───
    gst_name = re.match(r"^(.+?)\s+gst\s+(\d+)%?$", t)
    if gst_name:
        return _handle_gst_override_by_name(
            phone, gst_name.group(1).strip(), int(gst_name.group(2)), ctx
        )

    # ── Customer name: "NAME Ramesh" ─────────────
    name_m = re.match(r"^name\s+(.+)$", t_raw, re.IGNORECASE)
    if name_m:
        new_name = name_m.group(1).strip()
        if len(new_name) >= 2:
            return execute_action(
                _validate_result({
                    "action":       "set_customer",
                    "bill_changes": {"set_customer": new_name},
                }),
                phone, ctx,
            )

    # ── Pricing toggle ────────────────────────────
    if t in ("include", "inclusive", "include gst", "with gst"):
        return execute_action(
            _validate_result({
                "action":       "set_pricing",
                "bill_changes": {"set_pricing_type": "inclusive"},
            }),
            phone, ctx,
        )
    if t in ("exclude", "exclusive", "exclude gst", "without gst"):
        return execute_action(
            _validate_result({
                "action":       "set_pricing",
                "bill_changes": {"set_pricing_type": "exclusive"},
            }),
            phone, ctx,
        )

    return None   # Not a hard command — forward to LLM


# ════════════════════════════════════════════════
# LLM CALLERS
# ════════════════════════════════════════════════

def _call_gemini(system_prompt: str, messages: list) -> dict:
    """Call Gemini 2.0 Flash with JSON-mode output.

    google-generativeai is imported inside this function so the app
    starts even if the package is not installed (Haiku will be used).
    """
    import google.generativeai as genai  # optional dep

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY not configured")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name         = "gemini-2.0-flash",
        system_instruction = system_prompt,
    )

    user_content = messages[-1]["content"] if messages else ""
    response = model.generate_content(
        contents          = user_content,
        generation_config = {
            "response_mime_type": "application/json",
            "temperature":        0.1,
            "max_output_tokens":  1000,
        },
    )

    raw = (response.text or "").strip()
    return _parse_json_response(raw, source="Gemini")


def _call_haiku_fallback(system_prompt: str, messages: list) -> dict:
    """Call Claude Haiku as LLM fallback when Gemini is unavailable."""
    from anthropic.types import TextBlock
    from config import get_anthropic_client

    client   = get_anthropic_client()
    response = client.messages.create(
        model      = "claude-haiku-4-5-20251001",
        max_tokens = 1000,
        system     = system_prompt,
        messages   = messages,
        temperature = 0.1,
    )

    if not response.content:
        raise ValueError("Empty response from Claude Haiku")

    block = response.content[0]
    if not isinstance(block, TextBlock):
        raise ValueError(f"Unexpected Haiku response type: {type(block)}")

    raw = block.text.strip()
    # Strip markdown code fences if Haiku wraps JSON
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```\s*$",        "", raw)
    return _parse_json_response(raw.strip(), source="Haiku")


def _call_llm(system_prompt: str, messages: list) -> dict:
    """Try Gemini first; fall back to Haiku on any failure."""
    try:
        return _call_gemini(system_prompt, messages)
    except Exception as gemini_exc:
        log.warning(
            f"Gemini failed ({type(gemini_exc).__name__}: {gemini_exc}) "
            f"— switching to Haiku fallback"
        )

    # Haiku fallback — allow its exceptions to propagate
    return _call_haiku_fallback(system_prompt, messages)


def _parse_json_response(raw: str, source: str = "LLM") -> dict:
    """Parse a JSON string from an LLM response, with regex salvage on failure."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        raise ValueError(f"{source} returned non-JSON: {raw[:300]}")


# ════════════════════════════════════════════════
# RESULT VALIDATOR
# ════════════════════════════════════════════════

def _validate_result(result: dict) -> dict:
    """Ensure every required field exists with a safe, typed default.

    Coerces wrong types, rejects unknown action strings, and guarantees
    the executor never receives missing or malformed fields.
    """
    if not isinstance(result, dict):
        result = {}

    # ── action ──────────────────────────────────
    action = str(result.get("action") or "unknown").lower().strip()
    result["action"] = action if action in _VALID_ACTIONS else "unknown"

    # ── bill_changes ─────────────────────────────
    bc = result.get("bill_changes")
    if not isinstance(bc, dict):
        bc = {}

    # add_items — list of {name, price, qty}
    ai_raw = bc.get("add_items") or []
    bc["add_items"] = [
        {
            "name":  str(i.get("name") or "").strip(),
            "price": _safe_float(i.get("price"), 0.0),
            "qty":   max(0.01, _safe_float(i.get("qty"), 1.0) or 1.0),
        }
        for i in (ai_raw if isinstance(ai_raw, list) else [])
        if isinstance(i, dict) and str(i.get("name") or "").strip()
    ]

    # update_items — list of {name, price, qty}
    ui_raw = bc.get("update_items") or []
    bc["update_items"] = [
        {
            "name":  str(i.get("name") or "").strip(),
            "price": _safe_float(i.get("price"), 0.0),
            "qty":   _safe_float(i.get("qty"), 0.0),
        }
        for i in (ui_raw if isinstance(ui_raw, list) else [])
        if isinstance(i, dict) and str(i.get("name") or "").strip()
    ]

    # remove_item — string or None
    ri = bc.get("remove_item")
    bc["remove_item"] = (
        str(ri).strip()
        if ri and str(ri).lower().strip() not in ("null", "none", "")
        else None
    )

    # set_customer — string or None
    sc = bc.get("set_customer")
    bc["set_customer"] = (
        str(sc).strip()
        if sc and str(sc).lower().strip() not in ("null", "none", "")
        else None
    )

    # set_customer_phone — string or None
    scp = bc.get("set_customer_phone")
    bc["set_customer_phone"] = (
        str(scp).strip()
        if scp and str(scp).lower().strip() not in ("null", "none", "")
        else None
    )

    # set_discount — {type, value}
    disc = bc.get("set_discount") or {}
    if not isinstance(disc, dict):
        disc = {}
    dtype = str(disc.get("type") or "").lower().strip()
    bc["set_discount"] = {
        "type":  dtype if dtype in ("percent", "flat", "override") else None,
        "value": _safe_float(disc.get("value"), 0.0),
    }

    # set_pricing_type — "inclusive" | "exclusive" | None
    spt = str(bc.get("set_pricing_type") or "").lower().strip()
    bc["set_pricing_type"] = spt if spt in ("inclusive", "exclusive") else None

    # set_bill_type — "tax_invoice" | "bill_of_supply" | None
    sbt = str(bc.get("set_bill_type") or "").lower().strip()
    bc["set_bill_type"] = sbt if sbt in ("tax_invoice", "bill_of_supply") else None

    # load_last_bill — bool
    bc["load_last_bill"] = bool(bc.get("load_last_bill", False))

    result["bill_changes"] = bc

    # ── top-level scalars ─────────────────────────
    result["reply"]                = str(result.get("reply") or "")
    result["show_preview"]         = bool(result.get("show_preview",         False))
    result["needs_confirmation"]   = bool(result.get("needs_confirmation",   False))
    result["is_duplicate_warning"] = bool(result.get("is_duplicate_warning", False))
    result["is_typo_warning"]      = bool(result.get("is_typo_warning",      False))
    result["context_switched"]     = bool(result.get("context_switched",     False))

    # report_range — one of the valid strings or None
    rr = str(result.get("report_range") or "").lower().strip()
    result["report_range"] = rr if rr in _VALID_RANGES else None

    return result


# ════════════════════════════════════════════════
# HELPER FUNCTIONS
# ════════════════════════════════════════════════

def _parse_report_range(text: str) -> str:
    """Map a raw command string to a canonical report_range key."""
    t = text.lower().strip()
    t = re.sub(r"^gst\s*report\s*", "", t).strip()
    t = re.sub(r"^report\s*",        "", t).strip()

    if re.search(r"\btoday\b|aaj\b|ee\s*roju\b|abhi\b|ippudu\b", t):
        return "today"
    if re.search(r"last\s*7|7\s*day|last\s*week|hafte\b|vaarama\b", t):
        return "last_7_days"
    if re.search(r"last\s*month|pichle?\s*mahine?|last\s*nela\b|gata\s*nela\b", t):
        return "last_month"
    if re.search(r"this\s*month|is\s*mahine?|ee\s*nela\b|current\s*month", t):
        return "this_month"
    return "this_month"


def _handle_myitems(phone: str, ctx: ShopContext) -> str:
    """Return a formatted list of the shop's top 20 saved items."""
    try:
        shop_id = _derive_shop_id(phone)
        items   = get_top_items(shop_id, limit=20)
        if not items:
            return (
                "📦 No items saved yet.\n\n"
                "Items are saved automatically when you confirm a bill.\n"
                "The more bills you create, the faster GST lookup becomes! ✓"
            )
        lines = ["📦 *Your Saved Items*\n"]
        for idx, item in enumerate(items, 1):
            status = "✅" if item.get("confirmed") else "⚠️"
            name   = (item.get("item_name") or "unknown").title()
            hsn    = item.get("hsn") or "—"
            rate   = item.get("gst_rate", 18)
            count  = item.get("use_count", 0)
            lines.append(
                f"{idx}. {status} {name} — HSN: {hsn} | GST: {rate}% ({count}x)"
            )
        lines.append("\n✅ = confirmed  ⚠️ = auto-detected")
        lines.append("_To fix rate: *gst charger 18* or *GST 1 18*_")
        return "\n".join(lines)
    except Exception as exc:
        log.error(f"_handle_myitems failed for {phone}: {exc}", exc_info=True)
        return "📦 Could not load items. Please try again."


def _handle_gst_override(
    phone: str,
    item_index: int,
    rate: int,
    ctx: ShopContext,
) -> str:
    """Set GST rate on a pending bill item by 1-based index."""
    from core.entities.shop_profile import VALID_GST_SLABS

    try:
        pending = get_pending_bill(phone)
        if not pending:
            return (
                "⏰ No pending bill found.\n"
                "Send items to create a bill first."
            )
        if rate not in VALID_GST_SLABS:
            return f"❌ Invalid GST rate. Valid: *0%, 5%, 12%, 18%, 28%*"
        if item_index < 1 or item_index > len(pending.items):
            return (
                f"❌ Invalid item number. "
                f"Your bill has {len(pending.items)} item(s).\n"
                f"_Example: GST 1 18 sets item 1 to 18%_"
            )
        name = pending.items[item_index - 1].get("name", f"item {item_index}")
        pending.items[item_index - 1]["gst_rate"]   = rate
        pending.items[item_index - 1]["gst_source"] = "manual"
        pending.created_at = datetime.utcnow()
        store_pending(phone, pending)
        log.info(f"{phone}: GST override — item {item_index} ({name}) → {rate}%")
        return f"✅ *{name}* GST → {rate}%\n\n" + msg_preview(pending)
    except Exception as exc:
        log.error(f"_handle_gst_override failed for {phone}: {exc}", exc_info=True)
        return "❌ Could not update GST rate. Please try again."


def _handle_gst_override_by_name(
    phone: str,
    item_name: str,
    rate: int,
    ctx: ShopContext,
) -> str:
    """Set GST rate on a pending bill item matched by fuzzy name."""
    from core.entities.shop_profile import VALID_GST_SLABS
    from rapidfuzz import fuzz

    try:
        pending = get_pending_bill(phone)
        if not pending:
            return (
                "⏰ No pending bill found.\n"
                "Send items to create a bill first."
            )
        if rate not in VALID_GST_SLABS:
            return f"❌ Invalid GST rate. Valid: *0%, 5%, 12%, 18%, 28%*"
        if not pending.items:
            return "Your bill has no items."

        search     = item_name.lower().strip()
        best_idx   = None
        best_score = 0
        for idx, item in enumerate(pending.items):
            score = fuzz.WRatio(search, (item.get("name") or "").lower())
            if score > best_score:
                best_score = score
                best_idx   = idx

        if best_idx is None or best_score < 60:
            names = ", ".join(i.get("name", "") for i in pending.items)
            return (
                f"❌ No item matching *{item_name}* found.\n"
                f"Items in your bill: {names}\n"
                f"_Tip: use GST 1 {rate} to set by item number_"
            )

        matched = pending.items[best_idx].get("name", item_name)
        pending.items[best_idx]["gst_rate"]   = rate
        pending.items[best_idx]["gst_source"] = "manual"
        pending.created_at = datetime.utcnow()
        store_pending(phone, pending)
        log.info(f"{phone}: GST name override — '{matched}' → {rate}%")
        return f"✅ *{matched}* GST → {rate}%\n\n" + msg_preview(pending)
    except Exception as exc:
        log.error(f"_handle_gst_override_by_name failed for {phone}: {exc}", exc_info=True)
        return "❌ Could not update GST rate. Please try again."


def _handle_edit(phone: str, ctx: ShopContext) -> str:
    """Clear the pending bill and prompt the user to re-enter items."""
    try:
        clear_pending(phone)
        lang = ctx.language or "en"
        if lang == "te":
            return (
                "✏️ Bill cancel chesamu.\n\n"
                "Mee items meeru pamping cheyandi:\n"
                "_charger 499 cover 199 Ramesh kosam_\n\n"
                "Pamping chesina items tho new preview vastundi."
            )
        if lang == "hi":
            return (
                "✏️ Bill hata diya.\n\n"
                "Naaye items bhejiye:\n"
                "_charger 499 cover 199 Ramesh ke liye_\n\n"
                "Aap jo items bhejenge, unka naya preview aayega."
            )
        return (
            "✏️ Bill cleared.\n\n"
            "Send your updated items:\n"
            "_charger 499 cover 199 for Ramesh_\n\n"
            "A fresh preview will be generated."
        )
    except Exception as exc:
        log.error(f"_handle_edit failed for {phone}: {exc}", exc_info=True)
        return "✏️ Bill cleared. Send items to start fresh."


def _log_outgoing(phone: str, message: str) -> None:
    """Persist outgoing message to conversation log (non-fatal)."""
    if not message:
        return
    try:
        from services.registration import log_message
        log_message(phone, "OUT", message)
    except Exception as exc:
        log.warning(f"_log_outgoing failed for {phone}: {exc}")


def _llm_fallback_reply(ctx: ShopContext) -> str:
    """Friendly reply shown when the LLM is completely unavailable."""
    lang = ctx.language or "en"
    if lang == "te":
        return (
            "⚠️ Ippudu message artham cheyatam kashtanga undi.\n\n"
            "Meeru try cheyyandi:\n"
            "_charger 499 cover 199 Ramesh kosam_\n\n"
            "Lekapothe *help* type cheyyandi."
        )
    if lang == "hi":
        return (
            "⚠️ Abhi message samajhne mein thodi takleef ho rahi hai.\n\n"
            "Koshish karein:\n"
            "_charger 499 cover 199 Ramesh ke liye_\n\n"
            "Ya *help* type karein."
        )
    return (
        "⚠️ I'm having trouble understanding that.\n\n"
        "Try: _charger 499 cover 199 for Ramesh_\n"
        "Or type *help* for all options."
    )


def _safe_float(value, default):
    """Safely convert a value to float."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────
# TO WIRE INTO services/router.py:
# Find the ACTIVE shop section and add:
#
# from conversation.manager import handle_message
# reply = handle_message(phone, text)
# if reply:
#     send_text_message(phone, reply)
# return
# ─────────────────────────────────────────────
