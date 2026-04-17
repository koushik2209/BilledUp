"""
services.billing — Billing Handlers, Send Helpers, Preview Logic
------------------------------------------------------------------
All billing-related functions extracted from whatsapp_webhook.py.
No logic changes — exact copies with explicit imports.
"""

import re
import random
import string
import logging
from datetime import datetime, timedelta

from config import PLATFORM_NAME, BASE_URL, get_anthropic_client
from whatsapp_client import send_text_message, send_document_by_link
from claude_parser import parse_message
from gst_rates import get_gst_rate_smart, adjust_gst_for_price
from bill_generator import (
    ShopProfile, CustomerInfo, BillItem,
    generate_invoice_number, generate_pdf_bill, calculate_bill,
    PLACEHOLDER_GSTIN, VALID_GST_SLABS,
)
from database import db_session, Bill, ReportPDF, Shop
from reports import (
    get_gst_report, parse_report_range, msg_gst_report,
    export_gst_report_pdf,
)
from return_detector import detect_return_intent, negate_items
from main import get_shop, save_bill, get_today_summary

from services.pending import (
    PendingBill, store_pending, get_pending_bill, clear_pending,
)
from services.registration import (
    log_message, upsert_registration, resolve_state,
)
from api.formatters import (
    msg_bill_summary, msg_state_prompt,
)

log = logging.getLogger("billedup.billing")


# ════════════════════════════════════════════════
# SEND HELPERS
# ════════════════════════════════════════════════

def send(to: str, body: str) -> bool:
    """Send WhatsApp message via Meta Cloud API. Returns True on success."""
    try:
        result = send_text_message(to, body)
        if result.get("error"):
            log.error(f"Send failed to {to}: {result.get('error')}")
            return False
        log_message(to, "OUT", body)
        log.info(f"Sent to {to} ({len(body)} chars)")
        return True
    except Exception as e:
        log.error(f"Send failed to {to}: {e}")
        return False


def send_pdf(to: str, filename: str, caption: str = "", url_prefix: str = "bills"):
    """Send a PDF as a WhatsApp document (public HTTPS URL required).

    url_prefix: "bills" for invoices, "reports" for GST reports.
    """
    if not BASE_URL:
        log.warning("BASE_URL not set — cannot send PDF media. Sending text fallback.")
        send(
            to,
            f"📄 Your PDF is ready: {filename}\n(Configure BASE_URL for document delivery)",
        )
        return

    media_url = f"{BASE_URL.rstrip('/')}/{url_prefix}/{filename}"
    log.info(f"Sending PDF: {media_url} to {to}")
    try:
        result = send_document_by_link(
            to,
            media_url,
            filename,
            caption or f"📄 {filename}",
        )
        if result.get("error"):
            log.error(f"PDF send failed to {to}: {result.get('error')}")
            send(
                to,
                f"📄 Your bill PDF is ready but could not be attached.\nFilename: {filename}",
            )
            return
        log_message(to, "OUT", f"[PDF] {media_url}")
        log.info(f"PDF sent to {to}")
    except Exception as e:
        log.error(f"PDF send failed to {to}: {e}", exc_info=True)
        send(to, f"📄 Your bill PDF is ready but could not be attached.\nFilename: {filename}")


# ════════════════════════════════════════════════
# DB-DEPENDENT MESSAGE FORMATTERS
# ════════════════════════════════════════════════

def msg_today_summary(shop_id: str, shop_name: str, days: int) -> str:
    try:
        summary = get_today_summary(shop_id)
        cgst = summary.get('total_cgst', 0)
        sgst = summary.get('total_sgst', 0)
        igst = summary.get('total_igst', 0)
        gst_lines = ""
        if cgst or sgst:
            gst_lines += f"CGST: Rs.{cgst:.2f} | SGST: Rs.{sgst:.2f}\n"
        if igst:
            gst_lines += f"IGST: Rs.{igst:.2f}\n"
        return (
            f"📊 *Today's Summary*\n\n"
            f"Shop: {shop_name}\n"
            f"Date: {summary['date']}\n\n"
            f"Bills generated: *{summary['bill_count']}*\n"
            f"Total sales: *Rs.{summary['total_value']:.2f}*\n"
            f"{gst_lines}"
            f"Total GST: *Rs.{summary['total_gst']:.2f}*\n\n"
            f"Trial days left: {days}\n\n"
            f"_{PLATFORM_NAME} — Bill smarter. Grow faster._"
        )
    except Exception as e:
        log.error(f"Today summary error: {e}")
        return "Could not fetch today's summary. Please try again."


def msg_history(shop_id: str) -> str:
    try:
        from main import get_bill_history
        bills = get_bill_history(shop_id, limit=5)
        if not bills:
            return "No bills generated yet. Send your first bill message now!"

        lines = ["📋 *Recent Bills*\n"]
        for b in bills:
            dt = b["created_at"][:16]
            lines.append(
                f"• *{b['invoice_number']}*\n"
                f"  {b['customer_name']} — Rs.{b['grand_total']:.2f}\n"
                f"  {dt}"
            )
        return "\n\n".join(lines)
    except Exception as e:
        log.error(f"History error: {e}")
        return "Could not fetch history. Please try again."


# ════════════════════════════════════════════════
# PREVIEW + CONFIRMATION MESSAGES
# ════════════════════════════════════════════════

def _build_bill_items(pending: PendingBill) -> list:
    """Build BillItem list from PendingBill item dicts."""
    return [
        BillItem(
            name=i["name"], qty=i["qty"], price=abs(i["price"]),
            hsn=i.get("hsn", ""), gst_rate=i.get("gst_rate", 18),
            item_discount_type=i.get("item_discount_type", "none") or "none",
            item_discount_value=float(i.get("item_discount_value", 0) or 0),
        )
        for i in pending.items
    ]


def _compute_preview_totals(pending: PendingBill) -> dict:
    """Run calculate_bill on pending items to get GST breakdown for preview."""
    try:
        items = _build_bill_items(pending)
        br = calculate_bill(
            items,
            gst_client=None,
            shop_state_code=pending.shop_state_code,
            customer_state_code=pending.customer_state_code,
            bill_of_supply=pending.is_bill_of_supply,
            is_inclusive=pending.is_inclusive,
            bill_discount_type=pending.bill_discount_type or "none",
            bill_discount_value=float(pending.bill_discount_value or 0.0),
        )
        # For credit notes, negate all amounts
        sign = -1 if pending.is_return else 1
        raw_subtotal = round(sum(i.raw_amount for i in br.items), 2)
        item_discount_total = round(raw_subtotal - br.subtotal_before_bill_discount, 2)
        return {
            "subtotal":       br.subtotal * sign,
            "total_cgst":     br.total_cgst * sign,
            "total_sgst":     br.total_sgst * sign,
            "total_igst":     br.total_igst * sign,
            "total_gst":      br.total_gst * sign,
            "grand_total":    br.grand_total * sign,
            "is_igst":        br.is_igst,
            "raw_subtotal":   raw_subtotal * sign,
            "item_discount_total": item_discount_total * sign,
            "subtotal_before_bill_discount": br.subtotal_before_bill_discount * sign,
            "discount_total": br.discount_total * sign,
            "taxable_amount": br.taxable_amount * sign,
            "bill_discount_type":  br.bill_discount_type,
            "bill_discount_value": br.bill_discount_value,
        }
    except Exception as e:
        log.warning(f"Preview totals failed: {e}")
        return None


def msg_preview(pending: PendingBill) -> str:
    """Format bill preview message shown before confirmation."""
    if pending.is_return:
        lines = [
            "🔁 *Credit Note (Return)*\n",
            f"👤 Customer: *{pending.customer_name}*",
        ]
    else:
        lines = [
            "📋 *Bill Preview*\n",
            f"👤 Customer: *{pending.customer_name}*",
        ]
    if getattr(pending, "needs_confirmation", False):
        lines.insert(0, "⚠️ *Please double-check this bill before confirming.*")
    if pending.customer_phone:
        lines.append(f"📞 Phone: {pending.customer_phone}")

    # ── Invoice type + state/tax type ──
    if pending.is_bill_of_supply:
        lines.append(f"📄 Type: *Bill of Supply* (no GST)")
    else:
        is_intra = pending.customer_state_code == pending.shop_state_code
        assumed_tag = " _(assumed)_" if pending.state_assumed else ""

        if is_intra:
            lines.append(f"📍 State: {pending.customer_state}{assumed_tag}")
            lines.append(f"💰 Tax: CGST + SGST (intra-state)")
        else:
            lines.append(f"📍 State: {pending.customer_state} (Code: {pending.customer_state_code}){assumed_tag}")
            lines.append(f"💰 Tax: IGST (inter-state)")

        if pending.state_assumed:
            lines.append(f"_If different, reply:_ *STATE*")

    # ── Items ──
    lines.append(f"\n*{'Return Items' if pending.is_return else 'Items'}:*")
    has_low_confidence = False
    for i, item in enumerate(pending.items, 1):
        qty = int(item["qty"]) if item["qty"] == int(item["qty"]) else item["qty"]
        display_price = abs(item["price"])
        sign = "-" if pending.is_return else ""

        # Item-level discount marker (percent or flat)
        i_disc_type = (item.get("item_discount_type") or "none").lower()
        i_disc_val  = float(item.get("item_discount_value") or 0.0)
        if i_disc_type == "percent" and i_disc_val > 0:
            disc_tag = f" (-{i_disc_val:g}% off)"
        elif i_disc_type == "flat" and i_disc_val > 0:
            disc_tag = f" (-Rs.{i_disc_val:g})"
        else:
            disc_tag = ""

        if pending.is_bill_of_supply:
            # No GST info shown for Bill of Supply
            lines.append(f"  {i}. {item['name']} x{qty} — {sign}Rs.{display_price:.2f}{disc_tag}")
        else:
            rate = item.get("gst_rate", 18)
            confidence = item.get("gst_confidence", item.get("gst_source", ""))
            if confidence == "low" or confidence == "default":
                lines.append(f"  {i}. {item['name']} x{qty} — {sign}Rs.{display_price:.2f}{disc_tag} ({rate}% GST ⚠️)")
                has_low_confidence = True
            elif confidence == "medium" or confidence == "fuzzy":
                lines.append(f"  {i}. {item['name']} x{qty} — {sign}Rs.{display_price:.2f}{disc_tag} ({rate}% GST ~)")
            else:
                lines.append(f"  {i}. {item['name']} x{qty} — {sign}Rs.{display_price:.2f}{disc_tag} ({rate}% GST)")

    # ── Single grouped warning for low-confidence items ──
    if has_low_confidence:
        lines.append(f"\n⚠️ GST assumed for some items (default 18%). Verify if needed.")
        lines.append(f"_Fix: *GST 1 12* or *shirt gst 12*_")

    # ── Totals ──
    totals = _compute_preview_totals(pending)
    if totals:
        sign = "-" if pending.is_return else ""
        bdt = (pending.bill_discount_type or "none").lower()
        bdv = float(pending.bill_discount_value or 0)
        if bdt == "percent" and bdv > 0:
            pre_bill = abs(totals.get("subtotal_before_bill_discount", 0))
            disc_detail = f" ({bdv:g}% on Rs.{pre_bill:.2f})"
        elif bdt == "flat" and bdv > 0:
            disc_detail = " (flat)"
        elif bdt == "override" and bdv > 0:
            disc_detail = f" (final Rs.{bdv:.2f})"
        else:
            disc_detail = ""
        lines.append(f"\n━━━━━━━━━━━━━━━━━")
        if pending.is_bill_of_supply:
            item_disc = totals.get("item_discount_total", 0) or 0
            bill_disc = totals.get("discount_total", 0) or 0
            if item_disc or bill_disc:
                lines.append(f"Subtotal:  {sign}Rs.{abs(totals['raw_subtotal']):.2f}")
                if item_disc:
                    lines.append(f"Item Discount: -Rs.{abs(item_disc):.2f}")
                if bill_disc:
                    lines.append(f"Bill Discount: -Rs.{abs(bill_disc):.2f}{disc_detail}")
            lines.append(f"*{'REFUND' if pending.is_return else 'TOTAL'}: {sign}Rs.{abs(totals['subtotal']):.2f}*")
        elif pending.is_inclusive:
            # Inclusive: show grand total first, then backed-out base + GST
            lines.append(f"*{'REFUND' if pending.is_return else 'TOTAL'} (incl GST): {sign}Rs.{abs(totals['grand_total']):.2f}*")
            item_disc = totals.get("item_discount_total", 0) or 0
            bill_disc = totals.get("discount_total", 0) or 0
            if item_disc or bill_disc:
                lines.append(f"Subtotal:  {sign}Rs.{abs(totals['raw_subtotal']):.2f}")
                if item_disc:
                    lines.append(f"Item Discount: -Rs.{abs(item_disc):.2f}")
                if bill_disc:
                    lines.append(f"Bill Discount: -Rs.{abs(bill_disc):.2f}{disc_detail}")
            lines.append(f"Base:      {sign}Rs.{abs(totals['subtotal']):.2f}")
            if totals["is_igst"]:
                lines.append(f"IGST:      {sign}Rs.{abs(totals['total_igst']):.2f}")
            else:
                lines.append(f"CGST:      {sign}Rs.{abs(totals['total_cgst']):.2f}")
                lines.append(f"SGST:      {sign}Rs.{abs(totals['total_sgst']):.2f}")
            lines.append(f"Total GST: {sign}Rs.{abs(totals['total_gst']):.2f}")
        else:
            item_disc = totals.get("item_discount_total", 0) or 0
            bill_disc = totals.get("discount_total", 0) or 0
            if item_disc or bill_disc:
                lines.append(f"Subtotal:  {sign}Rs.{abs(totals['raw_subtotal']):.2f}")
                if item_disc:
                    lines.append(f"Item Discount: -Rs.{abs(item_disc):.2f}")
                if bill_disc:
                    lines.append(f"Bill Discount: -Rs.{abs(bill_disc):.2f}{disc_detail}")
                lines.append(f"Taxable:   {sign}Rs.{abs(totals['taxable_amount']):.2f}")
            else:
                lines.append(f"Subtotal: {sign}Rs.{abs(totals['subtotal']):.2f}")
            if totals["is_igst"]:
                lines.append(f"IGST:     {sign}Rs.{abs(totals['total_igst']):.2f}")
            else:
                lines.append(f"CGST:     {sign}Rs.{abs(totals['total_cgst']):.2f}")
                lines.append(f"SGST:     {sign}Rs.{abs(totals['total_sgst']):.2f}")
            lines.append(f"Total GST: {sign}Rs.{abs(totals['total_gst']):.2f}")
            lines.append(f"━━━━━━━━━━━━━━━━━")
            lines.append(f"*{'REFUND' if pending.is_return else 'TOTAL'}: {sign}Rs.{abs(totals['grand_total']):.2f}*")
    else:
        lines.append(f"\n⚠️ _Totals could not be calculated. Final bill will have correct totals._")

    # ── Confidence warning ──
    if pending.confidence < 0.8:
        lines.append(f"\n⚠️ _Some items may be incorrect. Please verify._")

    # ── Ambiguous parse warning ──
    if "ambiguous_parse" in pending.warnings:
        lines.append(f"\n⚠️ _Please verify quantity and price for some items._")

    # ── Commands ──
    lines.append(f"\n━━━━━━━━━━━━━━━━━")
    lines.append(f"Reply:")
    lines.append(f"• *CONFIRM* → Create bill")
    if not pending.is_bill_of_supply:
        if pending.is_inclusive:
            lines.append(f"• *EXCLUDE* → Prices are BEFORE GST")
        else:
            lines.append(f"• *INCLUDE* → Prices already INCLUDE GST")
    lines.append(f"• *EDIT* → Re-enter items")
    if not pending.is_bill_of_supply:
        lines.append(f"• *GST 1 12* or *shirt gst 12* → Fix rate")
    lines.append(f"• *CANCEL* → Discard")
    if not pending.is_return:
        lines.append(f"• *NAME Ravi* → Change name")
        if not pending.is_bill_of_supply:
            lines.append(f"• *STATE* → Change state")
    return "\n".join(lines)


# ════════════════════════════════════════════════
# ORPHAN COMMAND DETECTION
# ════════════════════════════════════════════════

_CONFIRM_COMMANDS = frozenset({
    "yes", "y", "confirm", "ok", "done",
    "cancel", "no", "discard",
    "edit", "change", "redo",
    "change state", "state", "igst",
    "include", "inclusive", "exclude", "exclusive",
})

def _is_confirmation_command(msg_lower: str) -> bool:
    """Check if message looks like a confirmation-flow command with no pending bill."""
    if msg_lower in _CONFIRM_COMMANDS:
        return True
    if msg_lower.startswith("name "):
        return True
    # "gst 1 12" (index-based) — NOT "gst report" (already handled earlier)
    if re.match(r"gst\s+\d+\s+\d+%?$", msg_lower):
        return True
    # "shirt gst 12" (name-based)
    if re.match(r".+\s+gst\s+\d+%?$", msg_lower):
        return True
    return False


# ════════════════════════════════════════════════
# GST REPORT HANDLER
# ════════════════════════════════════════════════

def _handle_gst_report(from_number: str, msg_lower: str, shop_id: str, shop_name: str):
    """Handle 'gst report' command with optional date range."""
    try:
        # Strip the command prefix to get the range text
        range_text = msg_lower.replace("gst report", "", 1).strip()
        start_date, end_date, label = parse_report_range(range_text)

        report = get_gst_report(shop_id, start_date, end_date)
        send(from_number, msg_gst_report(report, label))

        # Generate and send PDF if there are invoices
        if report.total_invoices > 0:
            pdf_bytes, report_filename = export_gst_report_pdf(report, label, shop_name)
            with db_session() as session:
                existing = session.query(ReportPDF).filter_by(filename=report_filename).first()
                if existing:
                    existing.pdf_data = pdf_bytes
                else:
                    session.add(ReportPDF(
                        filename=report_filename, shop_id=shop_id, pdf_data=pdf_bytes,
                    ))
            send_pdf(from_number, report_filename, f"📊 GST Report — {label}", url_prefix="reports")

    except Exception as e:
        log.error(f"GST report error for {from_number}: {e}", exc_info=True)
        send(from_number, "Could not generate GST report. Please try again.")


# ════════════════════════════════════════════════
# SHOP PRICING PREFERENCE HELPERS
# ════════════════════════════════════════════════

def _get_shop_default_inclusive(shop_id: str) -> bool:
    """Return True if the shop's default_pricing is 'inclusive'."""
    try:
        with db_session() as session:
            row = session.query(Shop).filter_by(shop_id=shop_id.upper()).first()
            if row and (row.default_pricing or "").lower() == "inclusive":
                return True
    except Exception as e:
        log.warning(f"Shop pricing lookup failed for {shop_id}: {e}")
    return False


def _build_pending_from_parser(
    phone: str, shop, parser_result: dict, raw_message: str,
) -> PendingBill:
    """Map a sanitized parser result to a PendingBill.

    Pricing precedence:
      1. Message-explicit pricing_type ("inclusive" | "exclusive") wins.
      2. Otherwise fall back to the shop's default_pricing.
      3. Fall back to "exclusive" if neither is valid.
    Item/bill discount fields + needs_confirmation pass through verbatim.
    """
    pt = parser_result.get("pricing_type")
    if pt in ("inclusive", "exclusive"):
        pricing_type = pt
    else:
        pricing_type = (getattr(shop, "default_pricing", None) or "exclusive").lower()
        if pricing_type not in ("inclusive", "exclusive"):
            pricing_type = "exclusive"
    is_inclusive = (pricing_type == "inclusive")

    items = [
        {
            **i,
            "item_discount_type":  i.get("item_discount_type", "none"),
            "item_discount_value": float(i.get("item_discount_value", 0) or 0),
        }
        for i in parser_result.get("items", [])
    ]

    return PendingBill(
        phone=phone,
        shop_id=shop.shop_id, shop_name=shop.name,
        shop_state=shop.state or "", shop_state_code=shop.state_code or "",
        customer_name=parser_result.get("customer_name", "Customer"),
        customer_phone=parser_result.get("customer_phone") or "",
        customer_state=shop.state or "",
        customer_state_code=shop.state_code or "",
        items=items,
        confidence=float(parser_result.get("confidence", 0.5)),
        warnings=list(parser_result.get("warnings", [])),
        raw_message=raw_message,
        created_at=datetime.utcnow(),
        is_return=False,
        is_bill_of_supply=False,
        is_inclusive=is_inclusive,
        pricing_type=pricing_type,
        bill_discount_type=parser_result.get("bill_discount_type", "none") or "none",
        bill_discount_value=float(parser_result.get("bill_discount_value", 0) or 0),
        needs_confirmation=bool(parser_result.get("needs_confirmation", False)),
    )


def _compute_bill_from_pending(pb: PendingBill):
    """Run calculate_bill over a confirmed PendingBill."""
    return calculate_bill(
        _build_bill_items(pb),
        shop_state_code=pb.shop_state_code,
        customer_state_code=pb.customer_state_code,
        bill_of_supply=pb.is_bill_of_supply,
        is_inclusive=pb.is_inclusive,
        bill_discount_type=pb.bill_discount_type or "none",
        bill_discount_value=float(pb.bill_discount_value or 0.0),
    )


def _toggle_pricing_mode(shop_id: str, mode: str) -> None:
    """Persist Shop.default_pricing immediately so the next bill auto-uses
    this mode, without waiting for the YES confirmation."""
    if mode not in ("inclusive", "exclusive"):
        return
    try:
        with db_session() as s:
            shop = s.query(Shop).filter_by(shop_id=shop_id.upper()).first()
            if shop and (shop.default_pricing or "") != mode:
                shop.default_pricing = mode
    except Exception as e:
        log.warning(f"_toggle_pricing_mode failed for {shop_id}: {e}")


def _save_shop_default_pricing(shop_id: str, is_inclusive: bool):
    """Persist the last-used pricing mode as the shop's default for next bill."""
    pref = "inclusive" if is_inclusive else "exclusive"
    try:
        with db_session() as session:
            row = session.query(Shop).filter_by(shop_id=shop_id.upper()).first()
            if row and (row.default_pricing or "") != pref:
                row.default_pricing = pref
    except Exception as e:
        log.warning(f"Shop pricing save failed for {shop_id}: {e}")


# ════════════════════════════════════════════════
# CONFIRMATION FLOW HANDLERS
# ════════════════════════════════════════════════

def _handle_new_bill(from_number: str, message: str, reg: dict,
                     shop_id: str, shop_name: str, d_left: int):
    """Parse message → store as pending → show preview."""
    try:
        parsed = parse_message(message)

        # Rate limit hit — parse_message returns error, don't show loading msg
        if parsed.get("error") and "wait" in str(parsed.get("error", "")).lower():
            send(from_number, f"⏳ {parsed['error']}")
            return

        if parsed.get("error") or not parsed.get("items"):
            error = parsed.get("error", "No items found")
            send(from_number,
                f"❌ Could not understand your message.\n\n"
                f"Reason: {error}\n\n"
                f"Please try like this:\n"
                f"_phone case 299 charger 499 customer Suresh_\n\n"
                f"Type *help* for more examples."
            )
            return

        # Load shop for state defaults
        shop = get_shop(shop_id)
        if shop:
            shop_state      = shop.state or reg.get("state_name", "")
            shop_state_code = shop.state_code or reg.get("state_code", "")
        else:
            shop_state      = reg.get("state_name", "")
            shop_state_code = reg.get("state_code", "")

        # Determine pricing: parser-explicit wins, then shop default
        parser_pt = parsed.get("pricing_type")
        if parser_pt in ("inclusive", "exclusive"):
            pricing_type = parser_pt
        else:
            default_inclusive = _get_shop_default_inclusive(shop_id)
            pricing_type = "inclusive" if default_inclusive else "exclusive"

        # Determine invoice type from registration
        is_bos = reg.get("invoice_type") == "BILL_OF_SUPPLY"

        # Resolve GST rates (skip for Bill of Supply — no GST applied)
        for item in parsed["items"]:
            if is_bos:
                item["hsn"]            = "9999"
                item["gst_rate"]       = 0
                item["gst_source"]     = "bill_of_supply"
                item["gst_confidence"] = "high"
            else:
                try:
                    rate_info = get_gst_rate_smart(item["name"], get_anthropic_client(), shop_id=shop_id)
                except Exception as e:
                    log.warning(f"GST lookup failed for '{item['name']}': {e}")
                    rate_info = {"hsn": "9999", "gst": 18, "source": "default", "confidence": "low"}
                # Apply price-based slab (clothing/footwear)
                rate_info = adjust_gst_for_price(item["name"], item["price"], rate_info)
                item["hsn"]            = rate_info.get("hsn", "9999")
                item["gst_rate"]       = rate_info.get("gst", 18)
                item["gst_source"]     = rate_info.get("source", "default")
                item["gst_confidence"] = rate_info.get("confidence", "low")

        # Detect return/credit note intent
        is_return = detect_return_intent(message, parsed["items"])
        bill_items = parsed["items"]
        if is_return:
            bill_items = negate_items(bill_items)
            # Re-attach resolved GST rates to negated items
            for neg, orig in zip(bill_items, parsed["items"]):
                neg["hsn"]           = orig.get("hsn", "9999")
                neg["gst_rate"]      = orig.get("gst_rate", 18)
                neg["gst_source"]    = orig.get("gst_source", "default")
                neg["gst_confidence"] = orig.get("gst_confidence", "low")

        pending = PendingBill(
            phone              = from_number,
            shop_id            = shop_id,
            shop_name          = shop_name,
            shop_state         = shop_state,
            shop_state_code    = shop_state_code,
            customer_name      = parsed["customer_name"],
            customer_state     = shop_state,       # default: same as shop
            customer_state_code= shop_state_code,  # default: intra-state
            items              = bill_items,
            confidence         = parsed.get("confidence", 1.0),
            warnings           = parsed.get("warnings", []),
            raw_message        = message,
            created_at         = datetime.utcnow(),
            is_return          = is_return,
            is_bill_of_supply  = is_bos,
            is_inclusive       = pricing_type == "inclusive" and not is_bos,
            customer_phone     = parsed.get("customer_phone") or "",
            pricing_type       = pricing_type if not is_bos else "exclusive",
            bill_discount_type = parsed.get("bill_discount_type", "none") or "none",
            bill_discount_value= float(parsed.get("bill_discount_value", 0) or 0),
            needs_confirmation = bool(parsed.get("needs_confirmation", False)),
        )

        store_pending(from_number, pending)
        send(from_number, msg_preview(pending))

    except Exception as e:
        log.error(f"Preview failed: {e}", exc_info=True)
        send(from_number,
            f"❌ Something went wrong. Please try again.\n\n"
            f"Support: +91 7981053846"
        )


def _match_item_by_name(search: str, items: list) -> int | None:
    """Match a search string to a pending bill item by name.

    Returns the 0-based index of the best match, or None.
    Tries: exact match → substring → token overlap.
    """
    search_lower = search.lower().strip()
    if not search_lower:
        return None

    # Exact match (case-insensitive)
    for i, item in enumerate(items):
        if item["name"].lower() == search_lower:
            return i

    # Substring match
    for i, item in enumerate(items):
        if search_lower in item["name"].lower() or item["name"].lower() in search_lower:
            return i

    # Token overlap: any word in search matches any word in item name
    search_tokens = set(search_lower.split())
    for i, item in enumerate(items):
        item_tokens = set(item["name"].lower().split())
        if search_tokens & item_tokens:
            return i

    return None


def _handle_confirmation(from_number: str, msg_lower: str, message: str,
                         pending: PendingBill, reg: dict, d_left: int):
    """Handle user commands during bill preview/confirmation."""

    # YES → generate bill
    if msg_lower in ("yes", "y", "confirm", "ok", "done"):
        clear_pending(from_number)
        _generate_confirmed_bill(from_number, pending, reg, d_left)
        return

    # CANCEL
    if msg_lower in ("cancel", "no", "discard"):
        clear_pending(from_number)
        send(from_number, "❌ Bill discarded.\n\nSend a new message to create another bill.")
        return

    # INCLUDE / EXCLUDE — toggle GST pricing mode on the pending bill
    if msg_lower in ("include", "inclusive", "exclude", "exclusive"):
        if pending.is_bill_of_supply:
            send(from_number,
                "ℹ️ This is a Bill of Supply — no GST is applied, "
                "so inclusive/exclusive does not apply."
            )
            return
        new_mode = msg_lower in ("include", "inclusive")
        pending.is_inclusive = new_mode
        pending.pricing_type = "inclusive" if new_mode else "exclusive"
        pending.created_at = datetime.utcnow()  # refresh expiry
        store_pending(from_number, pending)
        _toggle_pricing_mode(pending.shop_id, pending.pricing_type)
        header = (
            "✅ Prices marked as *GST inclusive*."
            if new_mode else
            "✅ Prices marked as *GST exclusive*."
        )
        send(from_number, f"{header}\n\n{msg_preview(pending)}")
        return

    # NAME <name>
    if msg_lower.startswith("name "):
        new_name = message[5:].strip()
        if len(new_name) < 2:
            send(from_number, "Please enter a valid name.\n_Example: NAME Ravi Kumar_")
            return
        pending.customer_name = new_name.title()
        pending.created_at = datetime.utcnow()  # refresh expiry
        store_pending(from_number, pending)
        send(from_number, msg_preview(pending))
        return

    # CHANGE STATE / STATE
    if msg_lower in ("change state", "state", "igst"):
        pending.awaiting_state = True
        pending.created_at = datetime.utcnow()
        store_pending(from_number, pending)
        send(from_number, msg_state_prompt())
        return

    # GST rate override: "GST 1 12" or "GST 1 12%" (index-based)
    gst_idx_match = re.match(r"gst\s+(\d+)\s+(\d+)%?$", msg_lower)
    if gst_idx_match:
        item_idx = int(gst_idx_match.group(1))
        new_rate = int(gst_idx_match.group(2))
        if new_rate not in VALID_GST_SLABS:
            send(from_number, f"❌ Invalid GST rate.\nValid: *0%, 5%, 12%, 18%, 28%*")
            return
        if item_idx < 1 or item_idx > len(pending.items):
            send(from_number, f"❌ Invalid item number. You have {len(pending.items)} item(s).")
            return
        pending.items[item_idx - 1]["gst_rate"] = new_rate
        pending.items[item_idx - 1]["gst_source"] = "manual"
        pending.created_at = datetime.utcnow()
        store_pending(from_number, pending)
        send(from_number, f"✅ Item {item_idx} GST rate → {new_rate}%\n\n{msg_preview(pending)}")
        return

    # GST rate override: "shirt gst 12" or "phone case gst 5%" (name-based)
    gst_name_match = re.match(r"(.+?)\s+gst\s+(\d+)%?$", msg_lower)
    if gst_name_match:
        search_name = gst_name_match.group(1).strip()
        new_rate = int(gst_name_match.group(2))
        if new_rate not in VALID_GST_SLABS:
            send(from_number, f"❌ Invalid GST rate.\nValid: *0%, 5%, 12%, 18%, 28%*")
            return
        matched_idx = _match_item_by_name(search_name, pending.items)
        if matched_idx is None:
            send(from_number,
                f"❌ No item matching \"{search_name}\".\n"
                f"_Try: *GST <item#> <rate>* (e.g., GST 1 12)_"
            )
            return
        pending.items[matched_idx]["gst_rate"] = new_rate
        pending.items[matched_idx]["gst_source"] = "manual"
        pending.created_at = datetime.utcnow()
        store_pending(from_number, pending)
        send(from_number, f"✅ \"{pending.items[matched_idx]['name']}\" GST rate → {new_rate}%\n\n{msg_preview(pending)}")
        return

    # EDIT
    if msg_lower in ("edit", "change", "redo"):
        clear_pending(from_number)
        send(from_number,
            "✏️ *Bill discarded. Send updated items:*\n\n"
            "_Example:_\n"
            "_shirt 500 pant 700 customer Suresh_\n\n"
            "Your message will be re-parsed and a new preview shown."
        )
        return

    # ── Natural correction: if message looks like items, re-parse and replace ──
    # Guard: only accept if message has digits (prices) AND parser is confident.
    # This prevents casual text ("ok nice", "thanks") from replacing the bill.
    _has_digits = bool(re.search(r"\d", message))
    if not _has_digits:
        send(from_number, f"❓ Unknown command. See options below:\n\n{msg_preview(pending)}")
        return

    try:
        parsed = parse_message(message)
        if parsed.get("items") and not parsed.get("error") and parsed.get("confidence", 0) >= 0.5:
            # Looks like new items — treat as automatic EDIT
            shop = get_shop(pending.shop_id)
            shop_state      = shop.state if shop else pending.shop_state
            shop_state_code = shop.state_code if shop else pending.shop_state_code

            for item in parsed["items"]:
                if pending.is_bill_of_supply:
                    item["hsn"]            = "9999"
                    item["gst_rate"]       = 0
                    item["gst_source"]     = "bill_of_supply"
                    item["gst_confidence"] = "high"
                else:
                    try:
                        rate_info = get_gst_rate_smart(item["name"], get_anthropic_client())
                    except Exception:
                        rate_info = {"hsn": "9999", "gst": 18, "source": "default", "confidence": "low"}
                    rate_info = adjust_gst_for_price(item["name"], item["price"], rate_info)
                    item["hsn"]           = rate_info.get("hsn", "9999")
                    item["gst_rate"]      = rate_info.get("gst", 18)
                    item["gst_source"]    = rate_info.get("source", "default")
                    item["gst_confidence"] = rate_info.get("confidence", "low")

            is_return = detect_return_intent(message, parsed["items"])
            bill_items = parsed["items"]
            if is_return:
                bill_items = negate_items(bill_items)
                for neg, orig in zip(bill_items, parsed["items"]):
                    neg["hsn"]           = orig.get("hsn", "9999")
                    neg["gst_rate"]      = orig.get("gst_rate", 18)
                    neg["gst_source"]    = orig.get("gst_source", "default")
                    neg["gst_confidence"] = orig.get("gst_confidence", "low")

            customer_name = parsed.get("customer_name", pending.customer_name)
            pending.items       = bill_items
            pending.customer_name = customer_name
            pending.customer_phone = parsed.get("customer_phone") or pending.customer_phone
            pending.confidence  = parsed.get("confidence", 1.0)
            pending.warnings    = parsed.get("warnings", [])
            pending.raw_message = message
            pending.is_return   = is_return
            pending.created_at  = datetime.utcnow()
            pending.bill_discount_type  = parsed.get("bill_discount_type", "none") or "none"
            pending.bill_discount_value = float(parsed.get("bill_discount_value", 0) or 0)
            pending.needs_confirmation  = bool(parsed.get("needs_confirmation", False))
            re_pt = parsed.get("pricing_type")
            if re_pt in ("inclusive", "exclusive"):
                pending.pricing_type = re_pt
                pending.is_inclusive = re_pt == "inclusive"
            store_pending(from_number, pending)
            send(from_number, msg_preview(pending))
            return
    except Exception as e:
        log.debug(f"Natural correction parse failed: {e}")

    # Truly unknown command → re-show preview
    send(from_number, f"❓ Unknown command. See options below:\n\n{msg_preview(pending)}")


def _handle_state_selection(from_number: str, message: str,
                            pending: PendingBill, d_left: int):
    """Handle state input after user chose CHANGE STATE."""
    msg_stripped = message.strip()

    # BACK / cancel state change
    if msg_stripped.lower() in ("back", "cancel", "skip"):
        pending.awaiting_state = False
        pending.created_at = datetime.utcnow()
        store_pending(from_number, pending)
        send(from_number, msg_preview(pending))
        return

    result = resolve_state(msg_stripped)
    if not result:
        send(from_number,
            f"❌ Could not find state: *{msg_stripped}*\n\n"
            f"Try again with state name or code.\n"
            f"_Example: Karnataka or 29_\n\n"
            f"Type *BACK* to keep current state."
        )
        return

    state_name, state_code = result
    pending.customer_state      = state_name
    pending.customer_state_code = state_code
    pending.awaiting_state      = False
    pending.state_assumed       = False
    pending.created_at          = datetime.utcnow()
    store_pending(from_number, pending)

    send(from_number, f"✅ State set to *{state_name}* (Code: {state_code})\n\n{msg_preview(pending)}")


def _check_recent_duplicate(shop_id: str, customer_name: str, raw_message: str) -> str | None:
    """Check if a bill with the same content was created in the last 60 seconds.
    Returns the invoice_number if duplicate found, None otherwise."""
    cutoff = datetime.utcnow() - timedelta(seconds=60)
    with db_session() as session:
        recent = session.query(Bill).filter(
            Bill.shop_id == shop_id,
            Bill.customer_name == customer_name,
            Bill.raw_message == raw_message,
            Bill.created_at >= cutoff,
        ).first()
        if recent:
            return recent.invoice_number
    return None


def _generate_confirmed_bill(from_number: str, pending: PendingBill,
                             reg: dict, d_left: int):
    """Generate final bill + PDF from confirmed pending data."""
    try:
        # Duplicate protection: same shop + customer + message within 60s
        dup_invoice = _check_recent_duplicate(
            pending.shop_id, pending.customer_name, pending.raw_message,
        )
        if dup_invoice:
            log.warning(f"Duplicate bill blocked: {dup_invoice} for {from_number}")
            send(from_number,
                f"⚠️ This bill was already generated: *{dup_invoice}*\n\n"
                f"Send a new message to create a different bill."
            )
            return

        send(from_number, "⏳ Generating your bill... 10 seconds.")

        # Load shop profile
        shop = get_shop(pending.shop_id)
        if not shop:
            shop = ShopProfile(
                shop_id    = pending.shop_id,
                name       = pending.shop_name,
                address    = reg.get("address", "Hyderabad"),
                gstin      = reg.get("gstin") or PLACEHOLDER_GSTIN,
                phone      = from_number.replace("whatsapp:", ""),
                state      = pending.shop_state,
                state_code = pending.shop_state_code,
                upi        = "",
            )

        customer = CustomerInfo(
            name       = pending.customer_name,
            phone      = pending.customer_phone or "",
            state      = pending.customer_state,
            state_code = pending.customer_state_code,
        )
        items = [
            BillItem(
                name=i["name"], qty=i["qty"], price=abs(i["price"]),
                hsn=i.get("hsn", ""), gst_rate=i.get("gst_rate", 18),
                item_discount_type=i.get("item_discount_type", "none") or "none",
                item_discount_value=float(i.get("item_discount_value", 0) or 0),
            )
            for i in pending.items
        ]

        invoice_number = generate_invoice_number(pending.shop_id, is_return=pending.is_return)
        pdf_data, bill_result = generate_pdf_bill(
            shop                = shop,
            customer            = customer,
            items               = items,
            invoice_number      = invoice_number,
            gst_client          = get_anthropic_client(),
            is_return           = pending.is_return,
            is_inclusive        = pending.is_inclusive,
            bill_discount_type  = pending.bill_discount_type or "none",
            bill_discount_value = float(pending.bill_discount_value or 0.0),
        )

        # Save to database (retry once, warn user on failure)
        db_saved = False
        for _attempt in range(2):
            try:
                save_bill(
                    shop_id        = pending.shop_id,
                    invoice_number = invoice_number,
                    customer_name  = pending.customer_name,
                    customer_phone = pending.customer_phone or "",
                    items          = bill_result.items,
                    bill_result    = bill_result,
                    pdf_data       = pdf_data,
                    raw_message    = pending.raw_message,
                    confidence     = pending.confidence,
                    is_return      = pending.is_return,
                )
                db_saved = True
                break
            except Exception as e:
                log.error(f"DB save attempt {_attempt + 1} failed: {e}")

        if not db_saved:
            log.critical(f"BILL LOST — {invoice_number} not saved to DB")
            send(from_number,
                f"⚠️ Bill {invoice_number} was generated but could not be saved to our records. "
                f"Please keep this invoice number and contact support: +91 7981053846"
            )

        # Remember the pricing mode the user just used as their default
        if db_saved and not pending.is_bill_of_supply:
            _save_shop_default_pricing(pending.shop_id, pending.is_inclusive)

        # Auto-save items to shop item master (confirmed=True)
        try:
            from database import save_item_master
            for item in bill_result.items:
                save_item_master(
                    pending.shop_id, item.name,
                    item.hsn, item.gst_rate,
                    confirmed=True,
                )
        except Exception as e:
            log.error(f"Item master save failed (non-fatal): {e}")

        # Update bill count
        try:
            upsert_registration(
                from_number,
                bills_count=reg.get("bills_count", 0) + 1,
            )
        except Exception as e:
            log.error(f"Bill count update failed (non-fatal): {e}")

        # Send bill summary + PDF
        summary = msg_bill_summary(
            bill_result       = bill_result,
            invoice_number    = invoice_number,
            customer_name     = pending.customer_name,
            days              = d_left,
            is_return         = pending.is_return,
            is_bill_of_supply = pending.is_bill_of_supply,
            customer_phone    = pending.customer_phone or "",
        )
        send(from_number, summary)

        doc_label = "Credit Note" if pending.is_return else ("Bill of Supply" if pending.is_bill_of_supply else "Invoice")
        sign = "-" if pending.is_return else ""
        suffix = ''.join(random.choices(string.ascii_lowercase, k=3))
        send_pdf(
            to       = from_number,
            filename = f"{invoice_number}-{suffix}.pdf",
            caption  = f"📄 {doc_label} {invoice_number} — {sign}Rs.{abs(bill_result.grand_total):.2f}",
        )

        log.info(
            f"{'Credit note' if pending.is_return else 'Bill'} generated: {invoice_number} "
            f"for {pending.shop_name} "
            f"total={sign}Rs.{abs(bill_result.grand_total):.2f}"
            f"{' [IGST]' if bill_result.is_igst else ''}"
        )

    except Exception as e:
        log.error(f"Bill generation failed: {e}", exc_info=True)
        send(from_number,
            f"❌ Something went wrong. Please try again.\n\n"
            f"Support: +91 7981053846"
        )


# ════════════════════════════════════════════════
# ITEM MASTER COMMANDS
# ════════════════════════════════════════════════

def _handle_myitems(from_number: str, shop_id: str):
    """Show top 20 saved items for the shop."""
    from database import get_top_items
    items = get_top_items(shop_id, limit=20)
    if not items:
        send(from_number,
            "📦 No items saved yet.\n\n"
            "Items are saved automatically when you confirm a bill.\n"
            "The more bills you generate, the faster & more accurate your GST becomes!"
        )
        return

    lines = ["📦 *Your Saved Items*\n"]
    for i, item in enumerate(items, 1):
        status = "✅" if item["confirmed"] else "⚠️"
        lines.append(
            f"{i}. {status} {item['item_name'].title()} — "
            f"HSN: {item['hsn']} | GST: {item['gst_rate']}% "
            f"({item['use_count']}x)"
        )
    lines.append(
        "\n✅ = confirmed  ⚠️ = auto-detected\n"
        "To fix GST: type *gst <item> <rate>*\n"
        "_Example: gst shirt 5_"
    )
    send(from_number, "\n".join(lines))


def _handle_gst_update(from_number: str, message: str, shop_id: str):
    """Handle 'gst <item> <rate>' command to update an item's GST rate."""
    from database import update_item_gst, save_item_master
    # Parse: "gst shirt 5" or "gst phone case 18"
    parts = message.strip().split()
    if len(parts) < 3:
        send(from_number,
            "Usage: *gst <item name> <rate>*\n"
            "_Example: gst shirt 5_\n"
            "_Example: gst phone case 18_"
        )
        return

    try:
        rate = int(parts[-1])
    except ValueError:
        send(from_number, "❌ Rate must be a number.\n_Example: gst shirt 5_")
        return

    valid_slabs = [0, 3, 5, 12, 18, 28]
    if rate not in valid_slabs:
        send(from_number,
            f"❌ Invalid GST rate: {rate}%\n"
            f"Valid rates: {', '.join(str(s) + '%' for s in valid_slabs)}"
        )
        return

    item_name = " ".join(parts[1:-1])
    if update_item_gst(shop_id, item_name, rate):
        send(from_number,
            f"✅ Updated *{item_name.title()}* → GST {rate}%\n"
            f"Future bills will use this rate automatically."
        )
    else:
        # Item not in master yet — create it confirmed with default HSN
        from gst_rates import get_gst_rate
        existing = get_gst_rate(item_name)
        hsn = existing.get("hsn", "9999")
        save_item_master(shop_id, item_name, hsn, rate, confirmed=True)
        send(from_number,
            f"✅ Saved *{item_name.title()}* — HSN: {hsn} | GST: {rate}%\n"
            f"Future bills will use this rate automatically."
        )
