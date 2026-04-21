"""
conversation.executor — LLM Result Executor
---------------------------------------------
Maps every LLM action JSON to the appropriate billing engine call.
Handles pending bill mutations, confirmation, cancellation,
load-last-bill, discounts, pricing, returns, reports, and help.

Design contract:
  - Returns a non-empty string → caller (manager) sends it to WhatsApp
  - Returns ""                 → messages already sent internally
                                 (confirm, report — multi-send operations)
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from rapidfuzz import fuzz

from config import get_anthropic_client
from gst_rates import get_gst_rate_smart, adjust_gst_for_price
from services.pending import (
    get_pending_bill, store_pending, clear_pending, PendingBill,
)
from services.billing import (
    _generate_confirmed_bill,
    _handle_gst_report,
    msg_preview,
)
from services.registration import get_registration, get_shop_id as _derive_shop_id
from conversation.context import ShopContext

log = logging.getLogger("billedup.conversation.executor")

_PENDING_EXPIRY_MINS = 10
_FUZZY_REMOVE_THRESH = 70
_FUZZY_UPDATE_THRESH = 60
_PLACEHOLDER_GSTIN   = "GSTIN00000000000"


# ════════════════════════════════════════════════
# MAIN ENTRY POINT
# ════════════════════════════════════════════════

def execute_action(result: dict, phone: str, ctx: ShopContext) -> str:
    """Route an LLM result dict to the correct billing handler.

    Returns the WhatsApp reply string, or "" if the handler already
    sent its own messages (confirm, report).
    """
    action       = (result.get("action") or "unknown").lower().strip()
    bill_changes = result.get("bill_changes") or {}
    reply        = result.get("reply") or ""
    show_preview = bool(result.get("show_preview", False))
    report_range = result.get("report_range") or None
    is_dup_warn  = bool(result.get("is_duplicate_warning", False))
    is_typo_warn = bool(result.get("is_typo_warning", False))

    # Prepend any LLM-generated warnings to the reply
    prefix = ""
    if is_dup_warn:
        prefix += "⚠️ *Duplicate warning:* " + (reply or "") + "\n\n"
        reply   = ""
    if is_typo_warn:
        prefix += "⚠️ *Price check:* " + (reply or "") + "\n\n"
        reply   = ""
    if prefix:
        reply = prefix.strip()

    try:
        if action == "billing":
            return _handle_billing(phone, bill_changes, ctx, reply, show_preview)
        if action == "add_item":
            return _handle_add_item(phone, bill_changes, ctx, reply)
        if action == "remove_item":
            return _handle_remove_item(phone, bill_changes, ctx, reply)
        if action == "update_item":
            return _handle_update_item(phone, bill_changes, ctx, reply)
        if action == "confirm":
            return _handle_confirm(phone, ctx, reply)
        if action == "confirm_with_change":
            return _handle_confirm_with_change(phone, bill_changes, ctx, reply)
        if action == "cancel":
            return _handle_cancel(phone, ctx, reply)
        if action == "load_last_bill":
            return _handle_load_last_bill(phone, bill_changes, ctx, reply)
        if action == "set_customer":
            return _handle_set_customer(phone, bill_changes, ctx, reply)
        if action == "set_discount":
            return _handle_set_discount(phone, bill_changes, ctx, reply)
        if action == "set_pricing":
            return _handle_set_pricing(phone, bill_changes, ctx, reply)
        if action == "set_bill_type":
            return _handle_set_bill_type(phone, bill_changes, ctx, reply)
        if action == "return":
            return _handle_return(phone, bill_changes, ctx, reply)
        if action == "report":
            return _handle_report(phone, report_range, ctx)
        if action == "complaint":
            return _handle_complaint(phone, reply, ctx)
        if action == "help":
            return _handle_help(ctx, reply)
        if action in ("greeting", "question", "settings", "unknown"):
            r = reply or _get_fallback_reply(ctx)
            return _with_pending_reminder(r, ctx)
        # Fallthrough — unknown action
        r = reply or _get_fallback_reply(ctx)
        return _with_pending_reminder(r, ctx)

    except Exception as exc:
        log.error(
            f"execute_action unhandled error for {phone} action={action}: {exc}",
            exc_info=True,
        )
        return _get_fallback_reply(ctx)


# ════════════════════════════════════════════════
# BILLING HANDLERS
# ════════════════════════════════════════════════

def _handle_billing(
    phone: str,
    bill_changes: dict,
    ctx: ShopContext,
    reply: str,
    show_preview: bool,
) -> str:
    """Start a new bill or merge into a fresh pending bill.

    If a pending bill younger than 10 minutes already exists, new items
    are appended to it (safety net for LLM misclassifying add_item as billing).
    """
    try:
        add_items: list = bill_changes.get("add_items") or []
        if not add_items:
            return reply or _get_fallback_reply(ctx)

        # Check for a live pending bill to merge into
        existing = get_pending_bill(phone)
        if existing and _pending_age_mins(existing) < _PENDING_EXPIRY_MINS:
            log.info(f"{phone}: billing action — merging into existing pending")
            return _add_items_to_pending(phone, add_items, existing, bill_changes, ctx)

        # No live pending — start fresh
        shop_id = _derive_shop_id(phone)
        is_bos  = _is_bill_of_supply(ctx)

        if bill_changes.get("set_bill_type") == "bill_of_supply":
            is_bos = True

        items = _resolve_gst_for_items(add_items, shop_id, is_bos)

        pricing_type = _resolve_pricing_type(bill_changes, ctx, is_bos)
        is_inclusive = (pricing_type == "inclusive") and not is_bos

        customer_name  = (bill_changes.get("set_customer") or "").strip() or "Customer"
        customer_phone = (bill_changes.get("set_customer_phone") or "").strip()

        disc_type, disc_value = _extract_discount(bill_changes)

        pending = PendingBill(
            phone               = phone,
            shop_id             = shop_id,
            shop_name           = ctx.shop_name or "Shop",
            shop_state          = ctx.state or "",
            shop_state_code     = ctx.state_code or "",
            customer_name       = customer_name,
            customer_state      = ctx.state or "",
            customer_state_code = ctx.state_code or "",
            items               = items,
            confidence          = 1.0,
            warnings            = [],
            raw_message         = "",
            created_at          = datetime.utcnow(),
            is_return           = False,
            is_bill_of_supply   = is_bos,
            is_inclusive        = is_inclusive,
            customer_phone      = customer_phone,
            pricing_type        = pricing_type if not is_bos else "exclusive",
            bill_discount_type  = disc_type,
            bill_discount_value = disc_value,
            needs_confirmation  = False,
        )
        store_pending(phone, pending)
        log.info(f"{phone}: new pending bill — {len(items)} item(s)")
        return msg_preview(pending)

    except Exception as exc:
        log.error(f"_handle_billing failed for {phone}: {exc}", exc_info=True)
        return _get_fallback_reply(ctx)


def _handle_add_item(
    phone: str,
    bill_changes: dict,
    ctx: ShopContext,
    reply: str,
) -> str:
    """Append items to the current pending bill."""
    try:
        add_items: list = bill_changes.get("add_items") or []
        if not add_items:
            return reply or _get_fallback_reply(ctx)

        existing = get_pending_bill(phone)
        if not existing or _pending_age_mins(existing) >= _PENDING_EXPIRY_MINS:
            log.info(f"{phone}: add_item — no live pending, starting fresh")
            return _handle_billing(phone, bill_changes, ctx, reply, show_preview=True)

        return _add_items_to_pending(phone, add_items, existing, bill_changes, ctx)

    except Exception as exc:
        log.error(f"_handle_add_item failed for {phone}: {exc}", exc_info=True)
        return _get_fallback_reply(ctx)


def _handle_remove_item(
    phone: str,
    bill_changes: dict,
    ctx: ShopContext,
    reply: str,
) -> str:
    """Remove an item from the pending bill by fuzzy name match."""
    try:
        remove_name = (bill_changes.get("remove_item") or "").strip()
        if not remove_name:
            return reply or "Which item should I remove? Please specify the item name."

        pending = get_pending_bill(phone)
        if not pending or _pending_age_mins(pending) >= _PENDING_EXPIRY_MINS:
            return _no_pending_reply(ctx)
        if not pending.items:
            return "Your bill has no items to remove."

        idx = _fuzzy_find_item(remove_name, pending.items, _FUZZY_REMOVE_THRESH)
        if idx is None:
            names = ", ".join(i.get("name", "") for i in pending.items)
            return (
                f"❌ Could not find *{remove_name}* in your bill.\n"
                f"Items: {names}\n"
                f"Please try again with the exact item name."
            )

        removed = pending.items[idx].get("name", remove_name)
        pending.items = [item for i, item in enumerate(pending.items) if i != idx]
        pending.created_at = datetime.utcnow()

        if not pending.items:
            clear_pending(phone)
            return (
                f"🗑️ Removed *{removed}*.\n"
                f"Your bill is now empty — send items to start a new bill."
            )

        store_pending(phone, pending)
        return f"✅ Removed *{removed}*.\n\n" + msg_preview(pending)

    except Exception as exc:
        log.error(f"_handle_remove_item failed for {phone}: {exc}", exc_info=True)
        return _get_fallback_reply(ctx)


def _handle_update_item(
    phone: str,
    bill_changes: dict,
    ctx: ShopContext,
    reply: str,
) -> str:
    """Update price/qty of one or more items in the pending bill."""
    try:
        update_items: list = bill_changes.get("update_items") or []
        if not update_items:
            return reply or _get_fallback_reply(ctx)

        pending = get_pending_bill(phone)
        if not pending or _pending_age_mins(pending) >= _PENDING_EXPIRY_MINS:
            return _no_pending_reply(ctx)
        if not pending.items:
            return "Your bill has no items to update."

        changed: list[str] = []
        not_found: list[str] = []

        for upd in update_items:
            search = (upd.get("name") or "").strip()
            if not search:
                continue
            idx = _fuzzy_find_item(search, pending.items, _FUZZY_UPDATE_THRESH)
            if idx is None:
                not_found.append(search)
                continue
            new_price = _safe_float(upd.get("price"), None)
            new_qty   = _safe_float(upd.get("qty"),   None)
            if new_price and new_price > 0:
                pending.items[idx]["price"] = new_price
            if new_qty and new_qty > 0:
                pending.items[idx]["qty"] = new_qty
            changed.append(pending.items[idx]["name"])

        # Also handle add_items that came along with update (RULE 9 multi-change)
        extra_adds = bill_changes.get("add_items") or []
        if extra_adds:
            shop_id = _derive_shop_id(phone)
            new_items = _resolve_gst_for_items(extra_adds, shop_id, pending.is_bill_of_supply)
            pending.items.extend(new_items)

        pending.created_at = datetime.utcnow()
        store_pending(phone, pending)

        parts: list[str] = []
        if changed:
            parts.append(f"✅ Updated: {', '.join(f'*{n}*' for n in changed)}")
        if not_found:
            parts.append(f"⚠️ Not found: {', '.join(not_found)}")
        header = "\n".join(parts) + "\n\n" if parts else ""
        return header + msg_preview(pending)

    except Exception as exc:
        log.error(f"_handle_update_item failed for {phone}: {exc}", exc_info=True)
        return _get_fallback_reply(ctx)


# ════════════════════════════════════════════════
# CONFIRM / CANCEL
# ════════════════════════════════════════════════

def _handle_confirm(phone: str, ctx: ShopContext, reply: str) -> str:
    """Confirm the pending bill — generates PDF + sends summary.

    Delegates to _generate_confirmed_bill which handles all sends
    internally. Returns "" so the manager does NOT send an extra message.
    """
    try:
        pending = get_pending_bill(phone)
        if not pending:
            return _no_pending_reply(ctx)
        if _pending_age_mins(pending) >= _PENDING_EXPIRY_MINS:
            clear_pending(phone)
            return (
                "⏰ Your bill session expired (10 min limit).\n"
                "Please send the items again to start a new bill."
            )
        if not pending.items:
            return "Your bill has no items. Please add items first."

        reg    = get_registration(phone) or {}
        d_left = ctx.trial_days_left

        clear_pending(phone)
        _generate_confirmed_bill(phone, pending, reg, d_left)
        return ""  # _generate_confirmed_bill sends everything

    except Exception as exc:
        log.error(f"_handle_confirm failed for {phone}: {exc}", exc_info=True)
        return (
            "❌ Could not generate your bill. Please try again.\n"
            "Support: +91 7981053846"
        )


def _handle_confirm_with_change(
    phone: str,
    bill_changes: dict,
    ctx: ShopContext,
    reply: str,
) -> str:
    """Apply changes to pending bill then immediately confirm."""
    try:
        pending = get_pending_bill(phone)
        if not pending or _pending_age_mins(pending) >= _PENDING_EXPIRY_MINS:
            return _no_pending_reply(ctx)

        # Apply all changes first
        pending = _apply_bill_changes(pending, bill_changes)

        # Handle extra add_items
        extra_adds = bill_changes.get("add_items") or []
        if extra_adds:
            shop_id   = _derive_shop_id(phone)
            new_items = _resolve_gst_for_items(
                extra_adds, shop_id, pending.is_bill_of_supply
            )
            pending.items.extend(new_items)

        # Handle update_items
        for upd in (bill_changes.get("update_items") or []):
            search = (upd.get("name") or "").strip()
            if not search:
                continue
            idx = _fuzzy_find_item(search, pending.items, _FUZZY_UPDATE_THRESH)
            if idx is not None:
                new_price = _safe_float(upd.get("price"), None)
                new_qty   = _safe_float(upd.get("qty"),   None)
                if new_price and new_price > 0:
                    pending.items[idx]["price"] = new_price
                if new_qty and new_qty > 0:
                    pending.items[idx]["qty"] = new_qty

        # Handle remove_item
        remove_name = (bill_changes.get("remove_item") or "").strip()
        if remove_name:
            idx = _fuzzy_find_item(remove_name, pending.items, _FUZZY_REMOVE_THRESH)
            if idx is not None:
                pending.items = [i for j, i in enumerate(pending.items) if j != idx]

        if not pending.items:
            clear_pending(phone)
            return "Your bill is empty after the change. Please send items to start again."

        pending.created_at = datetime.utcnow()
        store_pending(phone, pending)
        # Now confirm
        return _handle_confirm(phone, ctx, reply)

    except Exception as exc:
        log.error(f"_handle_confirm_with_change failed for {phone}: {exc}", exc_info=True)
        return _get_fallback_reply(ctx)


def _handle_cancel(phone: str, ctx: ShopContext, reply: str) -> str:
    """Discard the current pending bill."""
    try:
        pending = get_pending_bill(phone)
        clear_pending(phone)
        if pending and pending.items:
            count = len(pending.items)
            return (
                reply
                or f"❌ Bill cancelled ({count} item{'s' if count != 1 else ''} discarded).\n"
                   f"Send items any time to start a new bill."
            )
        return reply or "❌ No pending bill to cancel."
    except Exception as exc:
        log.error(f"_handle_cancel failed for {phone}: {exc}", exc_info=True)
        return "❌ Bill cancelled."


# ════════════════════════════════════════════════
# LOAD LAST BILL
# ════════════════════════════════════════════════

def _handle_load_last_bill(
    phone: str,
    bill_changes: dict,
    ctx: ShopContext,
    reply: str,
) -> str:
    """Create a new pending bill by copying items from the last completed bill."""
    try:
        if not ctx.last_bill:
            return (
                "📋 No previous bill found.\n"
                "Send items to create your first bill!"
            )

        lb       = ctx.last_bill
        shop_id  = _derive_shop_id(phone)
        is_bos   = _is_bill_of_supply(ctx)

        raw_items = lb.get("items") or []
        if not raw_items:
            return (
                f"📋 Last bill ({lb.get('invoice_number', 'N/A')}) had no items.\n"
                "Send items to start a new bill."
            )

        # Re-resolve GST rates for each copied item
        items = _resolve_gst_for_last_bill_items(raw_items, shop_id, is_bos)

        # Customer: use last bill's customer UNLESS bill_changes overrides
        override_customer = (bill_changes.get("set_customer") or "").strip()
        customer_name  = override_customer if override_customer else (lb.get("customer_name") or "Customer")
        customer_phone = (bill_changes.get("set_customer_phone") or "").strip()

        # Pricing: copy from last bill unless overridden
        pricing_raw  = lb.get("pricing_type") or ctx.default_pricing or "exclusive"
        override_pt  = (bill_changes.get("set_pricing_type") or "").lower()
        pricing_type = override_pt if override_pt in ("inclusive", "exclusive") else pricing_raw
        is_inclusive = (pricing_type == "inclusive") and not is_bos

        disc_type, disc_value = _extract_discount(bill_changes)

        pending = PendingBill(
            phone               = phone,
            shop_id             = shop_id,
            shop_name           = ctx.shop_name or "Shop",
            shop_state          = ctx.state or "",
            shop_state_code     = ctx.state_code or "",
            customer_name       = customer_name,
            customer_state      = ctx.state or "",
            customer_state_code = ctx.state_code or "",
            items               = items,
            confidence          = 1.0,
            warnings            = [],
            raw_message         = f"[copy of {lb.get('invoice_number', 'last bill')}]",
            created_at          = datetime.utcnow(),
            is_return           = False,
            is_bill_of_supply   = is_bos,
            is_inclusive        = is_inclusive,
            customer_phone      = customer_phone,
            pricing_type        = pricing_type if not is_bos else "exclusive",
            bill_discount_type  = disc_type,
            bill_discount_value = disc_value,
            needs_confirmation  = False,
        )
        store_pending(phone, pending)
        log.info(
            f"{phone}: loaded last bill {lb.get('invoice_number', '?')} "
            f"→ {len(items)} item(s) for {customer_name}"
        )
        return (
            f"📋 Loaded last bill ({lb.get('invoice_number', 'N/A')}).\n\n"
            + msg_preview(pending)
        )

    except Exception as exc:
        log.error(f"_handle_load_last_bill failed for {phone}: {exc}", exc_info=True)
        return _get_fallback_reply(ctx)


# ════════════════════════════════════════════════
# BILL PROPERTY SETTERS
# ════════════════════════════════════════════════

def _handle_set_customer(
    phone: str,
    bill_changes: dict,
    ctx: ShopContext,
    reply: str,
) -> str:
    """Set or update customer name / phone on the pending bill."""
    try:
        pending = get_pending_bill(phone)
        if not pending or _pending_age_mins(pending) >= _PENDING_EXPIRY_MINS:
            return _no_pending_reply(ctx)

        new_name  = (bill_changes.get("set_customer") or "").strip()
        new_phone = (bill_changes.get("set_customer_phone") or "").strip()

        if new_name:
            pending.customer_name = new_name
        if new_phone:
            pending.customer_phone = new_phone

        pending.created_at = datetime.utcnow()
        store_pending(phone, pending)

        parts: list[str] = []
        if new_name:
            parts.append(f"customer set to *{new_name}*")
        if new_phone:
            parts.append(f"phone *{new_phone}*")
        header = f"✅ Updated: {', '.join(parts)}.\n\n" if parts else ""
        return header + msg_preview(pending)

    except Exception as exc:
        log.error(f"_handle_set_customer failed for {phone}: {exc}", exc_info=True)
        return _get_fallback_reply(ctx)


def _handle_set_discount(
    phone: str,
    bill_changes: dict,
    ctx: ShopContext,
    reply: str,
) -> str:
    """Apply a bill-level discount to the pending bill."""
    try:
        pending = get_pending_bill(phone)
        if not pending or _pending_age_mins(pending) >= _PENDING_EXPIRY_MINS:
            return _no_pending_reply(ctx)

        disc_info  = bill_changes.get("set_discount") or {}
        disc_type  = _safe_discount_type(disc_info.get("type"))
        disc_value = _safe_float(disc_info.get("value"), 0.0)

        if disc_type == "none" or disc_type is None:
            return reply or "No discount specified. Send '10% off' or 'discount 500'."

        pending.bill_discount_type  = disc_type
        pending.bill_discount_value = disc_value
        pending.created_at          = datetime.utcnow()
        store_pending(phone, pending)

        if disc_type == "percent":
            disc_label = f"{disc_value:g}% off"
        elif disc_type == "flat":
            disc_label = f"₹{disc_value:g} off"
        else:
            disc_label = f"final ₹{disc_value:g}"

        return f"✅ Discount applied: *{disc_label}*\n\n" + msg_preview(pending)

    except Exception as exc:
        log.error(f"_handle_set_discount failed for {phone}: {exc}", exc_info=True)
        return _get_fallback_reply(ctx)


def _handle_set_pricing(
    phone: str,
    bill_changes: dict,
    ctx: ShopContext,
    reply: str,
) -> str:
    """Switch between inclusive and exclusive GST pricing."""
    try:
        pending = get_pending_bill(phone)
        if not pending or _pending_age_mins(pending) >= _PENDING_EXPIRY_MINS:
            return _no_pending_reply(ctx)

        pt = (bill_changes.get("set_pricing_type") or "").lower().strip()
        if pt not in ("inclusive", "exclusive"):
            return reply or "Please specify 'inclusive' or 'exclusive' pricing."

        if pending.is_bill_of_supply:
            return (
                "ℹ️ This is a Bill of Supply — no GST applies, "
                "so inclusive/exclusive mode has no effect."
            )

        pending.pricing_type = pt
        pending.is_inclusive = (pt == "inclusive")
        pending.created_at   = datetime.utcnow()
        store_pending(phone, pending)

        label = "GST *inclusive* (prices already include GST)" if pt == "inclusive" \
                else "GST *exclusive* (GST added on top of prices)"
        return f"✅ Switched to {label}.\n\n" + msg_preview(pending)

    except Exception as exc:
        log.error(f"_handle_set_pricing failed for {phone}: {exc}", exc_info=True)
        return _get_fallback_reply(ctx)


def _handle_set_bill_type(
    phone: str,
    bill_changes: dict,
    ctx: ShopContext,
    reply: str,
) -> str:
    """Switch between Tax Invoice and Bill of Supply."""
    try:
        pending = get_pending_bill(phone)
        if not pending or _pending_age_mins(pending) >= _PENDING_EXPIRY_MINS:
            return _no_pending_reply(ctx)

        bt = (bill_changes.get("set_bill_type") or "").lower().strip()
        if bt not in ("tax_invoice", "bill_of_supply"):
            return reply or "Please specify 'tax invoice' or 'bill of supply'."

        is_bos = (bt == "bill_of_supply")
        pending.is_bill_of_supply = is_bos
        if is_bos:
            pending.is_inclusive = False
            pending.pricing_type = "exclusive"
            # Zero out GST rates for Bill of Supply
            for item in pending.items:
                item["gst_rate"]   = 0
                item["gst_source"] = "bill_of_supply"
        pending.created_at = datetime.utcnow()
        store_pending(phone, pending)

        label = "*Bill of Supply* (no GST)" if is_bos else "*Tax Invoice* (GST applies)"
        return f"✅ Switched to {label}.\n\n" + msg_preview(pending)

    except Exception as exc:
        log.error(f"_handle_set_bill_type failed for {phone}: {exc}", exc_info=True)
        return _get_fallback_reply(ctx)


# ════════════════════════════════════════════════
# RETURN / REPORT / COMPLAINT / HELP
# ════════════════════════════════════════════════

def _handle_return(
    phone: str,
    bill_changes: dict,
    ctx: ShopContext,
    reply: str,
) -> str:
    """Return / credit note intent — pass through LLM reply.

    Full return processing is handled by the existing billing flow
    (detect_return_intent + negate_items) when the user confirms.
    The LLM reply should already contain the return bill preview.
    """
    try:
        pending = get_pending_bill(phone)
        if pending and _pending_age_mins(pending) < _PENDING_EXPIRY_MINS:
            pending.is_return  = True
            pending.created_at = datetime.utcnow()
            store_pending(phone, pending)
            return (
                (reply + "\n\n" if reply else "")
                + msg_preview(pending)
            )
        return _with_pending_reminder(
            reply or (
                "To process a return, please send the items being returned.\n"
                "_Example: return charger 499 Ramesh_"
            ),
            ctx,
        )
    except Exception as exc:
        log.error(f"_handle_return failed for {phone}: {exc}", exc_info=True)
        return _get_fallback_reply(ctx)


def _handle_report(
    phone: str,
    report_range: Optional[str],
    ctx: ShopContext,
) -> str:
    """Trigger a GST report and return "" (report is sent internally)."""
    try:
        shop_id   = _derive_shop_id(phone)
        shop_name = ctx.shop_name or "Shop"

        range_map = {
            "this_month":  "gst report",
            "last_month":  "gst report last month",
            "last_7_days": "gst report last 7 days",
            "today":       "gst report today",
        }
        command = range_map.get((report_range or "").lower(), "gst report")

        _handle_gst_report(phone, command, shop_id, shop_name)
        return ""  # _handle_gst_report calls send() internally

    except Exception as exc:
        log.error(f"_handle_report failed for {phone}: {exc}", exc_info=True)
        return "❌ Could not generate report. Please try again."


def _handle_complaint(
    phone: str,
    reply: str,
    ctx: ShopContext,
) -> str:
    """Acknowledge a complaint and optionally show last bill details."""
    try:
        if not reply:
            reply = (
                "I'm sorry about that! Let me help you fix it.\n"
                "Could you tell me what was wrong?"
            )
        if ctx.last_bill:
            lb = ctx.last_bill
            items_str = _format_items_mini(lb.get("items") or [])
            reply += (
                f"\n\n📋 *Your last bill:*\n"
                f"Invoice: {lb.get('invoice_number', 'N/A')}\n"
                f"Customer: {lb.get('customer_name', 'N/A')}\n"
                f"Items: {items_str}\n"
                f"Total: ₹{float(lb.get('grand_total') or 0):.2f}"
            )
        return reply
    except Exception as exc:
        log.error(f"_handle_complaint failed for {phone}: {exc}", exc_info=True)
        return reply or _get_fallback_reply(ctx)


def _handle_help(ctx: ShopContext, llm_reply: str) -> str:
    """Return help menu in the shopkeeper's preferred language."""
    try:
        if llm_reply:
            return llm_reply

        lang = ctx.language or "en"
        if lang == "te":
            return (
                "📘 *BilledUp — Help*\n\n"
                "*Bill cheyyatam:*\n"
                "_charger 499 cover 199 Ramesh kosam_\n\n"
                "*Confirm:*  avunu / yes / ok\n"
                "*Cancel:*   vaddhu / cancel\n"
                "*Customer:* NAME Ramesh\n"
                "*Discount:* 10% off / 500 takkuva\n"
                "*GST report:* gst report\n"
                "*Return bill:* return charger 499\n"
                "*Repeat bill:* last bill same cheyyandi\n"
                "*My items:*  myitems\n\n"
                "_Ela use cheyyalo telusukovalante 'help' type cheyyandi._"
            )
        if lang == "hi":
            return (
                "📘 *BilledUp — Help*\n\n"
                "*Bill banane ke liye:*\n"
                "_charger 499 cover 199 Ramesh ke liye_\n\n"
                "*Confirm:*  haan / yes / ok\n"
                "*Cancel:*   nahi / cancel\n"
                "*Customer:* NAME Ramesh\n"
                "*Discount:* 10% off / 500 kam karo\n"
                "*GST report:* gst report\n"
                "*Return bill:* return charger 499\n"
                "*Repeat bill:* same bill karo\n"
                "*My items:*  myitems\n\n"
                "_Aur help ke liye 'help' likhiye._"
            )
        return (
            "📘 *BilledUp — Help*\n\n"
            "*Create a bill:*\n"
            "_charger 499 cover 199 for Ramesh_\n\n"
            "*Confirm:*  yes / ok / 👍\n"
            "*Cancel:*   no / cancel / 👎\n"
            "*Customer:* NAME Ramesh\n"
            "*Discount:* 10% off / discount 500\n"
            "*GST report:* gst report\n"
            "*Return bill:* return charger 499\n"
            "*Repeat bill:* same as last bill\n"
            "*My items:*  myitems\n\n"
            "_Type 'help' any time for this menu._"
        )
    except Exception as exc:
        log.error(f"_handle_help failed: {exc}", exc_info=True)
        return llm_reply or "Type 'help' for usage instructions."


# ════════════════════════════════════════════════
# SHARED HELPERS
# ════════════════════════════════════════════════

def _apply_bill_changes(pending: PendingBill, bill_changes: dict) -> PendingBill:
    """Apply customer, discount, pricing, and bill-type fields from bill_changes.

    Skips fields whose value is None or the string "null".
    Returns the mutated pending object (same reference).
    """
    def _is_set(v) -> bool:
        return v is not None and str(v).lower() not in ("null", "none", "")

    customer = bill_changes.get("set_customer")
    if _is_set(customer):
        pending.customer_name = str(customer).strip()

    cust_phone = bill_changes.get("set_customer_phone")
    if _is_set(cust_phone):
        pending.customer_phone = str(cust_phone).strip()

    disc_info = bill_changes.get("set_discount") or {}
    disc_type  = _safe_discount_type(disc_info.get("type"))
    disc_value = _safe_float(disc_info.get("value"), None)
    if disc_type and disc_type != "none" and disc_value is not None:
        pending.bill_discount_type  = disc_type
        pending.bill_discount_value = disc_value

    pt = (bill_changes.get("set_pricing_type") or "").lower()
    if pt in ("inclusive", "exclusive") and not pending.is_bill_of_supply:
        pending.pricing_type = pt
        pending.is_inclusive = (pt == "inclusive")

    bt = (bill_changes.get("set_bill_type") or "").lower()
    if bt == "bill_of_supply":
        pending.is_bill_of_supply = True
        pending.is_inclusive      = False
        pending.pricing_type      = "exclusive"
    elif bt == "tax_invoice":
        pending.is_bill_of_supply = False

    return pending


def _with_pending_reminder(reply: str, ctx: ShopContext) -> str:
    """Append a pending-bill reminder to a reply if a live pending bill exists."""
    try:
        pb = ctx.pending_bill
        if not pb:
            return reply
        n = pb.get("item_count", 0) or len(pb.get("items") or [])
        if n == 0:
            return reply

        lang = ctx.language or "en"
        if lang == "te":
            reminder = (
                f"\n\n📋 Mee {n} items tho bill open ga undi "
                f"— *YES* cheppandi confirm ki, *vaddhu* cancel ki."
            )
        elif lang == "hi":
            reminder = (
                f"\n\n📋 Aapka {n} items ka bill pending hai "
                f"— confirm ke liye *YES* bhejo, cancel ke liye *nahi*."
            )
        else:
            reminder = (
                f"\n\n📋 Your bill with {n} item{'s' if n != 1 else ''} is still open "
                f"— reply *YES* to confirm or *cancel* to discard."
            )
        return reply + reminder
    except Exception:
        return reply


def _get_fallback_reply(ctx: ShopContext) -> str:
    """Return a language-appropriate fallback when something goes wrong."""
    lang = ctx.language or "en"
    if lang == "te":
        return (
            "📱 Meeru bill cheyyatam start cheyyatam ki:\n"
            "_charger 499 cover 199 Ramesh kosam_\n\n"
            "Sahayam kavali ante *help* type cheyyandi."
        )
    if lang == "hi":
        return (
            "📱 Bill banane ke liye likhiye:\n"
            "_charger 499 cover 199 Ramesh ke liye_\n\n"
            "Help ke liye *help* type karein."
        )
    return (
        "📱 To create a bill, send items like:\n"
        "_charger 499 cover 199 for Ramesh_\n\n"
        "Type *help* for all commands."
    )


# ════════════════════════════════════════════════
# INTERNAL UTILITIES
# ════════════════════════════════════════════════

def _pending_age_mins(pending: PendingBill) -> int:
    """Return age of a pending bill in minutes (handles tz-aware datetimes)."""
    try:
        created = pending.created_at
        if created is None:
            return 0
        if created.tzinfo is not None:
            created = created.astimezone(timezone.utc).replace(tzinfo=None)
        delta = datetime.utcnow() - created
        return max(0, int(delta.total_seconds() / 60))
    except Exception:
        return 0


def _is_bill_of_supply(ctx: ShopContext) -> bool:
    """True if the shop has no valid GSTIN (Bill of Supply shop)."""
    gstin = ctx.gstin or ""
    return not gstin or gstin == _PLACEHOLDER_GSTIN


def _resolve_pricing_type(
    bill_changes: dict,
    ctx: ShopContext,
    is_bos: bool,
) -> str:
    """Determine the correct pricing type from changes, context, or default."""
    if is_bos:
        return "exclusive"
    override = (bill_changes.get("set_pricing_type") or "").lower().strip()
    if override in ("inclusive", "exclusive"):
        return override
    return (ctx.default_pricing or "exclusive").lower()


def _extract_discount(bill_changes: dict) -> tuple[str, float]:
    """Extract discount type and value from bill_changes, with safe defaults."""
    disc_info  = bill_changes.get("set_discount") or {}
    disc_type  = _safe_discount_type(disc_info.get("type"))
    disc_value = _safe_float(disc_info.get("value"), 0.0)
    return disc_type, disc_value


def _safe_discount_type(raw) -> str:
    """Normalize discount type to one of: none/percent/flat/override."""
    if raw is None:
        return "none"
    v = str(raw).lower().strip()
    if v in ("percent", "flat", "override"):
        return v
    return "none"


def _safe_float(value, default) -> Optional[float]:
    """Safely convert value to float, returning default on failure."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fuzzy_find_item(
    search: str,
    items: list,
    threshold: int,
) -> Optional[int]:
    """Find the best-matching item index using rapidfuzz WRatio.

    Returns the 0-based index of the best match, or None if below threshold.
    Also checks exact/substring match before fuzzy to avoid false positives.
    """
    if not search or not items:
        return None
    search_lower = search.lower().strip()

    # 1. Exact match
    for i, item in enumerate(items):
        if (item.get("name") or "").lower() == search_lower:
            return i

    # 2. Substring
    for i, item in enumerate(items):
        name = (item.get("name") or "").lower()
        if search_lower in name or name in search_lower:
            return i

    # 3. Token overlap
    search_tokens = set(search_lower.split())
    for i, item in enumerate(items):
        item_tokens = set((item.get("name") or "").lower().split())
        if search_tokens & item_tokens:
            return i

    # 4. Fuzzy WRatio
    best_idx   = None
    best_score = 0
    for i, item in enumerate(items):
        name  = item.get("name") or ""
        score = fuzz.WRatio(search_lower, name.lower())
        if score > best_score:
            best_score = score
            best_idx   = i

    return best_idx if best_score >= threshold else None


def _resolve_gst_for_items(
    add_items: list,
    shop_id: str,
    is_bos: bool,
) -> list:
    """Convert LLM add_items list to pending-bill item dicts with GST rates."""
    result: list[dict] = []
    client = get_anthropic_client() if not is_bos else None

    for raw in add_items:
        name  = str(raw.get("name") or "item").strip()
        price = _safe_float(raw.get("price"), 0.0) or 0.0
        qty   = _safe_float(raw.get("qty"),   1.0) or 1.0
        qty   = max(0.01, qty)

        if is_bos:
            item = _make_item_dict(name, price, qty, 0, "9999", "bill_of_supply", "high")
        else:
            try:
                rate_info = get_gst_rate_smart(name, client, shop_id=shop_id)
                rate_info = adjust_gst_for_price(name, price, rate_info)
            except Exception as exc:
                log.warning(f"GST lookup failed for '{name}': {exc}")
                rate_info = {"gst": 18, "hsn": "9999", "source": "default", "confidence": "low"}
            item = _make_item_dict(
                name, price, qty,
                int(rate_info.get("gst", 18)),
                str(rate_info.get("hsn", "9999")),
                str(rate_info.get("source", "default")),
                str(rate_info.get("confidence", "low")),
            )
        result.append(item)
    return result


def _resolve_gst_for_last_bill_items(
    raw_items: list,
    shop_id: str,
    is_bos: bool,
) -> list:
    """Re-resolve GST rates for items copied from the last completed bill.

    Uses stored gst_rate if available; refreshes via get_gst_rate_smart
    to pick up any rate corrections the shop has made since.
    """
    result: list[dict] = []
    client = get_anthropic_client() if not is_bos else None

    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name  = str(raw.get("name") or "item").strip()
        price = abs(_safe_float(raw.get("price"), 0.0) or 0.0)
        qty   = abs(_safe_float(raw.get("qty"),   1.0) or 1.0)
        qty   = max(0.01, qty)

        if is_bos:
            item = _make_item_dict(name, price, qty, 0, "9999", "bill_of_supply", "high")
        else:
            try:
                rate_info = get_gst_rate_smart(name, client, shop_id=shop_id)
                rate_info = adjust_gst_for_price(name, price, rate_info)
            except Exception as exc:
                log.warning(f"GST lookup failed for copied item '{name}': {exc}")
                stored_rate = int(raw.get("gst_rate", 18) or 18)
                stored_hsn  = str(raw.get("hsn", "9999") or "9999")
                rate_info   = {"gst": stored_rate, "hsn": stored_hsn, "source": "stored", "confidence": "medium"}
            item = _make_item_dict(
                name, price, qty,
                int(rate_info.get("gst", 18)),
                str(rate_info.get("hsn", "9999")),
                str(rate_info.get("source", "stored")),
                str(rate_info.get("confidence", "medium")),
            )
        result.append(item)
    return result


def _make_item_dict(
    name: str,
    price: float,
    qty: float,
    gst_rate: int,
    hsn: str,
    source: str,
    confidence: str,
) -> dict:
    """Build a pending-bill item dict in the canonical format."""
    return {
        "name":               name,
        "price":              price,
        "qty":                qty,
        "gst_rate":           gst_rate,
        "hsn":                hsn,
        "gst_source":         source,
        "gst_confidence":     confidence,
        "item_discount_type": "none",
        "item_discount_value": 0.0,
    }


def _add_items_to_pending(
    phone: str,
    add_items: list,
    pending: PendingBill,
    bill_changes: dict,
    ctx: ShopContext,
) -> str:
    """Append new items to an existing pending bill and re-show preview."""
    shop_id   = _derive_shop_id(phone)
    new_items = _resolve_gst_for_items(add_items, shop_id, pending.is_bill_of_supply)
    pending.items.extend(new_items)
    pending = _apply_bill_changes(pending, bill_changes)
    pending.created_at = datetime.utcnow()
    store_pending(phone, pending)
    names = ", ".join(i["name"] for i in new_items)
    return f"✅ Added: {names}\n\n" + msg_preview(pending)


def _no_pending_reply(ctx: ShopContext) -> str:
    """Standard reply when a handler requires a pending bill but none exists."""
    lang = ctx.language or "en"
    if lang == "te":
        return (
            "⏰ Pending bill ledu.\n"
            "Items type cheyyandi: _charger 499 cover 199 Ramesh kosam_"
        )
    if lang == "hi":
        return (
            "⏰ Koi pending bill nahi hai.\n"
            "Items bhejiye: _charger 499 cover 199 Ramesh ke liye_"
        )
    return (
        "⏰ No pending bill found.\n"
        "Send items to start: _charger 499 cover 199 for Ramesh_"
    )


def _format_items_mini(items: list) -> str:
    """Compact inline item summary for complaint/complaint context."""
    if not items:
        return "none"
    parts = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        name  = item.get("name") or "item"
        qty   = item.get("qty", 1)
        price = float(item.get("price", 0))
        parts.append(f"{name} ×{qty} ₹{price:.0f}")
    suffix = f" (+{len(items)-5} more)" if len(items) > 5 else ""
    return ", ".join(parts) + suffix
