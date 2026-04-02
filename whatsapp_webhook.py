"""
whatsapp_webhook.py
BilledUp - Production Grade WhatsApp Integration
-------------------------------------------------
Features:
- Complete self-registration flow
- Multi-step conversation state machine
- Shop registration with GSTIN validation
- 10 day free trial tracking
- Bill generation pipeline
- Bill history and daily summary
- Meta WhatsApp Cloud API (Graph) — verify token + JSON webhook
- Graceful error handling
"""

import os
import re
import hmac
import hashlib
import random
import string
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from flask import Flask, request, Response

from config import (
    PLATFORM_NAME,
    BASE_URL,
    VERIFY_TOKEN,
    WHATSAPP_APP_SECRET,
    get_anthropic_client,
)
from whatsapp_client import (
    send_text_message,
    send_document_by_link,
    parse_meta_webhook_payload,
)
from claude_parser import parse_message
from gst_rates import get_gst_rate_smart, adjust_gst_for_price
from bill_generator import (
    ShopProfile, CustomerInfo, BillItem,
    generate_invoice_number, generate_pdf_bill, calculate_bill,
    PLACEHOLDER_GSTIN, GSTIN_REGEX, VALID_GST_SLABS,
)
from main import (
    init_database, seed_demo_shop,
    get_shop, save_bill,
    get_today_summary,
)
from database import (
    db_session, Registration, ConversationLog, Shop, PendingBillRecord,
    Bill, ReportPDF,
    init_database as init_db,
    generate_api_key, validate_api_key,
)
from reports import (
    get_gst_report, parse_report_range, msg_gst_report,
    export_gst_report_pdf,
)
from return_detector import detect_return_intent, negate_items

# ── Logging ──
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt= "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("billedup.whatsapp")

# ── Flask ──
app = Flask(__name__)

# ════════════════════════════════════════════════
# CONVERSATION STATE MACHINE
# ════════════════════════════════════════════════
# Each shopkeeper goes through these states:
#
# NEW           → never messaged before
# ASKED_NAME    → we asked for shop name
# ASKED_ADDRESS → we asked for address
# ASKED_GSTIN   → we asked for GSTIN (optional)
# ACTIVE        → registered and billing
# EXPIRED       → trial ended
# ════════════════════════════════════════════════

def init_registration_tables():
    """Create all tables via SQLAlchemy."""
    init_db()
    log.info("Registration tables initialised")


def get_registration(phone: str) -> dict | None:
    """Get registration record for a phone number."""
    with db_session() as session:
        row = session.query(Registration).filter_by(phone=phone).first()
        if not row:
            return None
        return {
            "phone": row.phone, "shop_name": row.shop_name,
            "address": row.address, "gstin": row.gstin,
            "invoice_type": row.invoice_type or "TAX_INVOICE",
            "state": row.state, "trial_start": row.trial_start.isoformat() if row.trial_start else None,
            "trial_end": row.trial_end.isoformat() if row.trial_end else None,
            "active": row.active, "bills_count": row.bills_count,
        }


ALLOWED_REG_FIELDS = {
    "shop_name", "address", "gstin", "invoice_type", "state",
    "trial_start", "trial_end", "active", "bills_count",
}

def upsert_registration(phone: str, **fields):
    """Create or update a registration record."""
    for key in fields:
        if key not in ALLOWED_REG_FIELDS:
            raise ValueError(f"Invalid registration field: {key}")

    with db_session() as session:
        reg = session.query(Registration).filter_by(phone=phone).first()
        if not reg:
            reg = Registration(phone=phone)
            session.add(reg)
        for key, val in fields.items():
            # Convert ISO strings to datetime for date fields
            if key in ("trial_start", "trial_end") and isinstance(val, str):
                val = datetime.fromisoformat(val)
            setattr(reg, key, val)


def log_message(phone: str, direction: str, message: str):
    """Log every message for debugging."""
    with db_session() as session:
        session.add(ConversationLog(
            phone=phone, direction=direction, message=message[:1000],
        ))


def is_trial_active(reg: dict) -> bool:
    """Check if shopkeeper's trial is still valid."""
    if not reg.get("trial_end"):
        return False
    trial_end = datetime.fromisoformat(reg["trial_end"])
    return datetime.utcnow() < trial_end


def days_left(reg: dict) -> int:
    """Days remaining in trial."""
    if not reg.get("trial_end"):
        return 0
    trial_end = datetime.fromisoformat(reg["trial_end"])
    delta = trial_end - datetime.utcnow()
    return max(0, delta.days)


def activate_trial(phone: str, shop_name: str, address: str, gstin: str = ""):
    """
    Activate 10 day free trial for a new shopkeeper.
    Creates shop in database and marks registration active.

    Invoice type is derived from GSTIN:
      - Valid GSTIN → TAX_INVOICE (GST applied)
      - No GSTIN    → BILL_OF_SUPPLY (no GST)
    """
    trial_start = datetime.utcnow()
    trial_end   = trial_start + timedelta(days=10)

    has_gstin    = bool(gstin and gstin.strip())
    invoice_type = "TAX_INVOICE" if has_gstin else "BILL_OF_SUPPLY"

    # Generate unique shop_id from phone
    shop_id = "S" + re.sub(r"\D", "", phone)[-8:]

    # Create ShopProfile in shops table
    api_key = None
    with db_session() as session:
        existing = session.query(Shop).filter_by(shop_id=shop_id).first()
        if not existing:
            api_key = generate_api_key()
            session.add(Shop(
                shop_id    = shop_id,
                name       = shop_name,
                address    = address,
                gstin      = gstin or PLACEHOLDER_GSTIN,
                phone      = phone.replace("whatsapp:", ""),
                upi        = "",
                state      = "Telangana",
                state_code = "36",
                api_key    = api_key,
            ))
        else:
            api_key = existing.api_key

    # Update registration
    upsert_registration(
        phone,
        shop_name    = shop_name,
        address      = address,
        gstin        = gstin,
        invoice_type = invoice_type,
        state        = "ACTIVE",
        trial_start  = trial_start.isoformat(),
        trial_end    = trial_end.isoformat(),
        active       = True,
    )

    log.info(f"Trial activated for {phone} — shop_id={shop_id} — {invoice_type} — ends {trial_end.date()}")
    return shop_id, api_key


def get_shop_id(phone: str) -> str:
    """Get shop_id from phone number."""
    return "S" + re.sub(r"\D", "", phone)[-8:]


# ════════════════════════════════════════════════
# MESSAGE TEMPLATES
# ════════════════════════════════════════════════

def msg_welcome() -> str:
    return (
        "👋 *Welcome to BilledUp!*\n\n"
        "Generate GST bills in 10 seconds on WhatsApp.\n"
        "No Tally. No computer. No training needed.\n\n"
        "Let's set up your free 10-day trial. 🚀\n\n"
        "First — *what is your shop name?*\n\n"
        "_Example: Ravi Mobile Accessories_"
    )


def msg_ask_address(shop_name: str) -> str:
    return (
        f"✅ Great! *{shop_name}*\n\n"
        f"Now — *what is your shop address?*\n\n"
        f"_Example: Shop No. 14, Koti Market, Hyderabad - 500095_"
    )


def msg_ask_gstin() -> str:
    return (
        "Almost done! 🎉\n\n"
        "*Do you have a GSTIN number?*\n\n"
        "If yes — type it now.\n"
        "Example: _36AABCU9603R1ZX_\n\n"
        "If no — type *skip*\n\n"
        "_You can add GSTIN later anytime._"
    )


def msg_activated(shop_name: str, days: int, api_key: str = "", invoice_type: str = "TAX_INVOICE") -> str:
    key_line = f"\n🔑 *Your API Key:*\n`{api_key}`\n_Keep this safe — use it for API access._\n" if api_key else ""
    if invoice_type == "BILL_OF_SUPPLY":
        bill_type_line = (
            "✅ Since you are not GST registered, your bills will be *Bill of Supply* (no GST).\n"
            "_You can add GSTIN later to switch to Tax Invoice._\n"
        )
    else:
        bill_type_line = "✅ Your bills will include GST (*Tax Invoice*).\n"
    return (
        f"🎊 *You are all set, {shop_name}!*\n\n"
        f"{bill_type_line}\n"
        f"Your *{days}-day free trial* has started.\n"
        f"After trial: just Rs.299/month.\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"*How to generate a bill:*\n\n"
        f"Just type your items and prices:\n\n"
        f"_phone case 299 charger 499 customer Suresh_\n\n"
        f"Your bill will be ready in 10 seconds! ⚡\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"*Commands:*\n"
        f"• *today* — Today's sales summary\n"
        f"• *history* — Last 5 bills\n"
        f"• *gst report* — Monthly GST summary\n"
        f"• *help* — Show this message\n"
        f"{key_line}\n"
        f"Try generating your first bill now! 👆"
    )


def msg_help(shop_name: str, days: int) -> str:
    return (
        f"📖 *BilledUp Help*\n\n"
        f"Shop: {shop_name}\n"
        f"Trial days left: {days}\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"*Generate a bill:*\n"
        f"Type items and prices naturally:\n\n"
        f"_phone case 299 charger 499 customer Suresh_\n"
        f"_rice 50 dal 80 oil 120 customer Ramesh_\n"
        f"_shirt 599 jeans 999 2 customer Priya_\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"*Commands:*\n"
        f"• *today* — Today's summary\n"
        f"• *history* — Last 5 bills\n"
        f"• *gst report* — This month's GST summary\n"
        f"• *gst report last 7 days* — Custom range\n"
        f"• *help* — This message\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"*Support:*\n"
        f"WhatsApp: +91 7981053846\n"
        f"Mon-Sat, 9am to 7pm"
    )


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


def msg_bill_summary(bill_result, invoice_number: str, customer_name: str,
                     days: int, is_return: bool = False, is_bill_of_supply: bool = False) -> str:
    sign = "-" if is_return else ""
    total_label = "REFUND" if is_return else "TOTAL"

    if is_return:
        doc_label = "Credit Note"
        header = "🔁 *Credit Note Generated!*"
    elif is_bill_of_supply:
        doc_label = "Bill of Supply"
        header = "✅ *Bill of Supply Generated!*"
    else:
        doc_label = "Tax Invoice"
        header = "✅ *Bill Generated!*"

    lines = [
        f"{header}\n",
        f"📋 {doc_label}: *{invoice_number}*",
        f"👤 Customer: *{customer_name}*\n",
        f"*Items:*",
    ]
    for item in bill_result.items:
        qty = int(item.qty) if item.qty == int(item.qty) else item.qty
        if is_bill_of_supply:
            lines.append(f"• {item.name} x{qty} — {sign}Rs.{abs(item.amount):.2f}")
        else:
            lines.append(f"• {item.name} x{qty} — {sign}Rs.{abs(item.total):.2f} ({item.gst_rate}% GST)")

    lines.append(f"\n━━━━━━━━━━━━━━━━━")
    if is_bill_of_supply:
        # No GST breakdown for Bill of Supply
        lines.append(f"*{total_label}: {sign}Rs.{abs(bill_result.subtotal):.2f}*\n")
    else:
        lines.append(f"Subtotal:  {sign}Rs.{abs(bill_result.subtotal):.2f}")
        if bill_result.is_igst:
            lines.append(f"IGST:      {sign}Rs.{abs(bill_result.total_igst):.2f}")
        else:
            lines.append(f"CGST:      {sign}Rs.{abs(bill_result.total_cgst):.2f}")
            lines.append(f"SGST:      {sign}Rs.{abs(bill_result.total_sgst):.2f}")
        lines += [
            f"Total GST: {sign}Rs.{abs(bill_result.total_gst):.2f}",
            f"━━━━━━━━━━━━━━━━━",
            f"*{total_label}: {sign}Rs.{abs(bill_result.grand_total):.2f}*\n",
        ]
    lines += [
        f"_{bill_result.in_words}_\n",
        f"📄 PDF attached below. Forward to customer.",
        f"Trial days left: {days}",
        f"\n_{PLATFORM_NAME}_",
    ]
    return "\n".join(lines)


def msg_trial_expired(shop_name: str) -> str:
    return (
        f"⏰ *{shop_name}, your 10-day free trial has ended.*\n\n"
        f"To continue generating bills — upgrade to BilledUp Standard:\n\n"
        f"*Rs.299/month*\n"
        f"• Unlimited GST bills\n"
        f"• Telugu and Hindi support\n"
        f"• Monthly CA report\n\n"
        f"To upgrade — contact us:\n"
        f"WhatsApp: +91 7981053846\n\n"
        f"_We will activate your account within 5 minutes._"
    )


def msg_invalid_gstin() -> str:
    return (
        f"❌ That GSTIN format looks incorrect.\n\n"
        f"A valid GSTIN has 15 characters like:\n"
        f"_36AABCU9603R1ZX_\n\n"
        f"Please try again — or type *skip* to add GSTIN later."
    )


# ════════════════════════════════════════════════
# GSTIN VALIDATION
# ════════════════════════════════════════════════

# GSTIN_REGEX imported from bill_generator

def is_valid_gstin(gstin: str) -> bool:
    return bool(GSTIN_REGEX.match(gstin.upper().strip()))


# ════════════════════════════════════════════════
# SEND HELPERS
# ════════════════════════════════════════════════

def send(to: str, body: str):
    """Send WhatsApp message via Meta Cloud API."""
    try:
        result = send_text_message(to, body)
        if result.get("error"):
            log.error(f"Send failed to {to}: {result.get('error')}")
            return
        log_message(to, "OUT", body)
        log.info(f"Sent to {to} ({len(body)} chars)")
    except Exception as e:
        log.error(f"Send failed to {to}: {e}")


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
# INDIAN STATES (GST state codes)
# ════════════════════════════════════════════════

INDIAN_STATES = {
    "01": "Jammu & Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "26": "Dadra & Nagar Haveli and Daman & Diu",
    "27": "Maharashtra",
    "29": "Karnataka",
    "30": "Goa",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman & Nicobar",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
}


def resolve_state(input_str: str) -> tuple[str, str] | None:
    """
    Resolve user input to (state_name, state_code).
    Accepts state code ("29"), state name ("Karnataka"), or partial match.
    Returns None if no match found.
    """
    s = input_str.strip()
    if not s:
        return None

    # Exact code match
    if s in INDIAN_STATES:
        return INDIAN_STATES[s], s

    # Zero-padded single digit
    if s.isdigit() and len(s) == 1:
        padded = f"0{s}"
        if padded in INDIAN_STATES:
            return INDIAN_STATES[padded], padded

    # Exact name match (case-insensitive)
    s_lower = s.lower()
    for code, name in INDIAN_STATES.items():
        if name.lower() == s_lower:
            return name, code

    # Partial / substring match (only if input is >= 3 chars to avoid "a" matching "Assam")
    if len(s_lower) >= 3:
        for code, name in INDIAN_STATES.items():
            if s_lower in name.lower():
                return name, code

    return None


# ════════════════════════════════════════════════
# PENDING BILL (preview before confirmation)
# ════════════════════════════════════════════════

PENDING_EXPIRY_MINUTES = 10

@dataclass
class PendingBill:
    phone: str
    shop_id: str
    shop_name: str
    shop_state: str
    shop_state_code: str
    customer_name: str
    customer_state: str
    customer_state_code: str
    items: list
    confidence: float
    warnings: list
    raw_message: str
    created_at: datetime
    awaiting_state: bool = False
    state_assumed: bool = True
    is_return: bool = False
    is_bill_of_supply: bool = False


def _serialize_pending(bill: PendingBill) -> str:
    """Serialize PendingBill to JSON string for DB storage."""
    import json
    data = {
        "phone": bill.phone,
        "shop_id": bill.shop_id,
        "shop_name": bill.shop_name,
        "shop_state": bill.shop_state,
        "shop_state_code": bill.shop_state_code,
        "customer_name": bill.customer_name,
        "customer_state": bill.customer_state,
        "customer_state_code": bill.customer_state_code,
        "items": bill.items,
        "confidence": bill.confidence,
        "warnings": bill.warnings,
        "raw_message": bill.raw_message,
        "created_at": bill.created_at.isoformat(),
        "awaiting_state": bill.awaiting_state,
        "state_assumed": bill.state_assumed,
        "is_return": bill.is_return,
        "is_bill_of_supply": bill.is_bill_of_supply,
    }
    return json.dumps(data)


def _deserialize_pending(json_str: str) -> PendingBill:
    """Deserialize JSON string back to PendingBill."""
    import json
    data = json.loads(json_str)
    data["created_at"] = datetime.fromisoformat(data["created_at"])
    # Backwards compat: old pending bills in DB won't have this field
    data.setdefault("is_bill_of_supply", False)
    return PendingBill(**data)


def store_pending(phone: str, bill: PendingBill):
    """Store a pending bill in DB (replaces any existing)."""
    expires_at = bill.created_at + timedelta(minutes=PENDING_EXPIRY_MINUTES)
    with db_session() as session:
        row = session.query(PendingBillRecord).filter_by(phone=phone).first()
        if row:
            row.data_json = _serialize_pending(bill)
            row.expires_at = expires_at
        else:
            session.add(PendingBillRecord(
                phone=phone,
                data_json=_serialize_pending(bill),
                expires_at=expires_at,
            ))


def get_pending_bill(phone: str) -> PendingBill | None:
    """Get pending bill if exists and not expired. Returns None if expired/missing."""
    with db_session() as session:
        row = session.query(PendingBillRecord).filter_by(phone=phone).first()
        if not row:
            return None
        if datetime.utcnow() > row.expires_at:
            session.delete(row)
            return None
        return _deserialize_pending(row.data_json)


def clear_pending(phone: str):
    """Remove pending bill for a phone number."""
    with db_session() as session:
        row = session.query(PendingBillRecord).filter_by(phone=phone).first()
        if row:
            session.delete(row)


def cleanup_expired_pending():
    """Remove all expired pending bills. Called on each incoming message."""
    with db_session() as session:
        session.query(PendingBillRecord).filter(
            PendingBillRecord.expires_at < datetime.utcnow()
        ).delete()


# ════════════════════════════════════════════════
# PREVIEW + CONFIRMATION MESSAGES
# ════════════════════════════════════════════════

def _compute_preview_totals(pending: PendingBill) -> dict:
    """Run calculate_bill on pending items to get GST breakdown for preview."""
    try:
        items = [
            BillItem(
                name=i["name"], qty=i["qty"], price=abs(i["price"]),
                hsn=i.get("hsn", ""), gst_rate=i.get("gst_rate", 18),
            )
            for i in pending.items
        ]
        br = calculate_bill(
            items,
            gst_client=None,
            shop_state_code=pending.shop_state_code,
            customer_state_code=pending.customer_state_code,
            bill_of_supply=pending.is_bill_of_supply,
        )
        # For credit notes, negate all amounts
        sign = -1 if pending.is_return else 1
        return {
            "subtotal":   br.subtotal * sign,
            "total_cgst": br.total_cgst * sign,
            "total_sgst": br.total_sgst * sign,
            "total_igst": br.total_igst * sign,
            "total_gst":  br.total_gst * sign,
            "grand_total": br.grand_total * sign,
            "is_igst":    br.is_igst,
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

        if pending.is_bill_of_supply:
            # No GST info shown for Bill of Supply
            lines.append(f"  {i}. {item['name']} x{qty} — {sign}Rs.{display_price:.2f}")
        else:
            rate = item.get("gst_rate", 18)
            confidence = item.get("gst_confidence", item.get("gst_source", ""))
            if confidence == "low" or confidence == "default":
                lines.append(f"  {i}. {item['name']} x{qty} — {sign}Rs.{display_price:.2f} ({rate}% GST ⚠️)")
                has_low_confidence = True
            elif confidence == "medium" or confidence == "fuzzy":
                lines.append(f"  {i}. {item['name']} x{qty} — {sign}Rs.{display_price:.2f} ({rate}% GST ~)")
            else:
                lines.append(f"  {i}. {item['name']} x{qty} — {sign}Rs.{display_price:.2f} ({rate}% GST)")

    # ── Single grouped warning for low-confidence items ──
    if has_low_confidence:
        lines.append(f"\n⚠️ GST assumed for some items (default 18%). Verify if needed.")
        lines.append(f"_Fix: *GST 1 12* or *shirt gst 12*_")

    # ── Totals ──
    totals = _compute_preview_totals(pending)
    if totals:
        sign = "-" if pending.is_return else ""
        lines.append(f"\n━━━━━━━━━━━━━━━━━")
        if pending.is_bill_of_supply:
            # Bill of Supply: total = subtotal, no GST breakdown
            lines.append(f"*{'REFUND' if pending.is_return else 'TOTAL'}: {sign}Rs.{abs(totals['subtotal']):.2f}*")
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

    # ── Confidence warning ──
    if pending.confidence < 0.8:
        lines.append(f"\n⚠️ _Some items may be incorrect. Please verify._")

    # ── Ambiguous parse warning ──
    if "ambiguous_parse" in pending.warnings:
        lines.append(f"\n⚠️ _Please verify quantity and price for some items._")

    # ── Commands ──
    lines.append(f"\n━━━━━━━━━━━━━━━━━")
    lines.append(f"Reply:")
    lines.append(f"• *YES* → Confirm")
    lines.append(f"• *EDIT* → Re-enter items")
    if not pending.is_bill_of_supply:
        lines.append(f"• *GST 1 12* or *shirt gst 12* → Fix rate")
    lines.append(f"• *CANCEL* → Discard")
    if not pending.is_return:
        lines.append(f"• *NAME Ravi* → Change name")
        if not pending.is_bill_of_supply:
            lines.append(f"• *STATE* → Change state")
    return "\n".join(lines)


def msg_state_prompt() -> str:
    """Prompt user to enter customer state."""
    return (
        "📍 *Enter customer's state:*\n\n"
        "Type the state name or GST code:\n\n"
        "_Examples:_\n"
        "• *Karnataka* or *29*\n"
        "• *Maharashtra* or *27*\n"
        "• *Tamil Nadu* or *33*\n"
        "• *Delhi* or *07*\n"
        "• *Gujarat* or *24*\n"
        "• *Kerala* or *32*\n"
        "• *UP* or *09*\n\n"
        "Type *BACK* to keep current state."
    )


# ════════════════════════════════════════════════
# ORPHAN COMMAND DETECTION
# ════════════════════════════════════════════════

_CONFIRM_COMMANDS = frozenset({
    "yes", "y", "confirm", "ok", "done",
    "cancel", "no", "discard",
    "edit", "change", "redo",
    "change state", "state", "igst",
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
            shop_state      = shop.state
            shop_state_code = shop.state_code
        else:
            shop_state      = "Telangana"
            shop_state_code = "36"

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
                    rate_info = get_gst_rate_smart(item["name"], get_anthropic_client())
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
    try:
        parsed = parse_message(message)
        if parsed.get("items") and not parsed.get("error"):
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
            pending.confidence  = parsed.get("confidence", 1.0)
            pending.warnings    = parsed.get("warnings", [])
            pending.raw_message = message
            pending.is_return   = is_return
            pending.created_at  = datetime.utcnow()
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


def _generate_confirmed_bill(from_number: str, pending: PendingBill,
                             reg: dict, d_left: int):
    """Generate final bill + PDF from confirmed pending data."""
    try:
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
            state      = pending.customer_state,
            state_code = pending.customer_state_code,
        )
        items = [
            BillItem(
                name=i["name"], qty=i["qty"], price=abs(i["price"]),
                hsn=i.get("hsn", ""), gst_rate=i.get("gst_rate", 18),
            )
            for i in pending.items
        ]

        invoice_number = generate_invoice_number(pending.shop_id, is_return=pending.is_return)
        pdf_data, bill_result = generate_pdf_bill(
            shop           = shop,
            customer       = customer,
            items          = items,
            invoice_number = invoice_number,
            gst_client     = get_anthropic_client(),
            is_return      = pending.is_return,
        )

        # Save to database
        try:
            save_bill(
                shop_id        = pending.shop_id,
                invoice_number = invoice_number,
                customer_name  = pending.customer_name,
                customer_phone = from_number,
                items          = bill_result.items,
                bill_result    = bill_result,
                pdf_data       = pdf_data,
                raw_message    = pending.raw_message,
                confidence     = pending.confidence,
                is_return      = pending.is_return,
            )
        except Exception as e:
            log.error(f"DB save failed (non-fatal): {e}")

        # Update bill count
        upsert_registration(
            from_number,
            bills_count=reg.get("bills_count", 0) + 1,
        )

        # Send bill summary + PDF
        summary = msg_bill_summary(
            bill_result       = bill_result,
            invoice_number    = invoice_number,
            customer_name     = pending.customer_name,
            days              = d_left,
            is_return         = pending.is_return,
            is_bill_of_supply = pending.is_bill_of_supply,
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
# CONVERSATION HANDLER
# ════════════════════════════════════════════════

def handle_message(from_number: str, message: str):
    """
    Main conversation state machine.
    Routes every message based on shopkeeper's current state.
    """
    log_message(from_number, "IN", message)
    msg_lower = message.lower().strip()

    # Load registration
    reg = get_registration(from_number)

    # ── STATE: NEW — never seen this number ──
    if not reg:
        upsert_registration(from_number, state="ASKED_NAME")
        send(from_number, msg_welcome())
        return

    state = reg.get("state", "NEW")

    # ── STATE: ASKED_NAME — waiting for shop name ──
    if state == "ASKED_NAME":
        if len(message.strip()) < 3:
            send(from_number,
                "Please enter your shop name.\n"
                "_Example: Ravi Mobile Accessories_"
            )
            return
        shop_name = message.strip().title()
        upsert_registration(from_number, shop_name=shop_name, state="ASKED_ADDRESS")
        send(from_number, msg_ask_address(shop_name))
        return

    # ── STATE: ASKED_ADDRESS — waiting for address ──
    if state == "ASKED_ADDRESS":
        if len(message.strip()) < 5:
            send(from_number,
                "Please enter your shop address.\n"
                "_Example: Shop No. 14, Koti Market, Hyderabad - 500095_"
            )
            return
        address = message.strip()
        upsert_registration(from_number, address=address, state="ASKED_GSTIN")
        send(from_number, msg_ask_gstin())
        return

    # ── STATE: ASKED_GSTIN — waiting for GSTIN or skip ──
    if state == "ASKED_GSTIN":
        shop_name = reg.get("shop_name", "Your Shop")
        address   = reg.get("address", "")

        if msg_lower == "skip":
            # Skip GSTIN — Bill of Supply (no GST)
            shop_id, api_key = activate_trial(from_number, shop_name, address, "")
            d_left  = days_left(get_registration(from_number))
            send(from_number, msg_activated(shop_name, d_left, api_key, invoice_type="BILL_OF_SUPPLY"))
            return

        gstin = message.strip().upper()
        if not is_valid_gstin(gstin):
            send(from_number, msg_invalid_gstin())
            return

        # Valid GSTIN — Tax Invoice (GST applied)
        shop_id, api_key = activate_trial(from_number, shop_name, address, gstin)
        d_left  = days_left(get_registration(from_number))
        send(from_number, msg_activated(shop_name, d_left, api_key, invoice_type="TAX_INVOICE"))
        return

    # ── STATE: ACTIVE — registered shopkeeper ──
    if state == "ACTIVE":
        # Check trial expiry
        if not is_trial_active(reg):
            upsert_registration(from_number, state="EXPIRED")
            send(from_number, msg_trial_expired(reg.get("shop_name", "Shop")))
            return

        shop_name = reg.get("shop_name", "Shop")
        shop_id   = get_shop_id(from_number)
        d_left    = days_left(reg)

        # Handle commands
        if msg_lower in ("help", "?"):
            send(from_number, msg_help(shop_name, d_left))
            return

        if msg_lower in ("today", "aaj", "summary"):
            send(from_number, msg_today_summary(shop_id, shop_name, d_left))
            return

        if msg_lower in ("history", "bills", "recent"):
            send(from_number, msg_history(shop_id))
            return

        if msg_lower.startswith("gst report"):
            _handle_gst_report(from_number, msg_lower, shop_id, shop_name)
            return

        if msg_lower in ("hi", "hello", "hai", "start"):
            send(from_number, msg_help(shop_name, d_left))
            return

        # ── Cleanup expired pending bills ──
        cleanup_expired_pending()

        # ── Check for pending bill (confirmation mode) ──
        pending = get_pending_bill(from_number)
        if pending:
            if pending.awaiting_state:
                _handle_state_selection(from_number, message, pending, d_left)
            else:
                _handle_confirmation(from_number, msg_lower, message, pending, reg, d_left)
            return

        # ── Catch orphan confirmation commands (no pending bill / expired) ──
        if _is_confirmation_command(msg_lower):
            send(from_number,
                "⏰ Session expired. Please send items again.\n\n"
                "_Example: phone case 299 charger 499 customer Suresh_"
            )
            return

        # ── New bill message → parse and show preview ──
        _handle_new_bill(from_number, message, reg, shop_id, shop_name, d_left)
        return

    # ── STATE: EXPIRED ──
    if state == "EXPIRED":
        send(from_number, msg_trial_expired(reg.get("shop_name", "Shop")))
        return

    # ── Unknown state — reset ──
    upsert_registration(from_number, state="ASKED_NAME")
    send(from_number, msg_welcome())


# ════════════════════════════════════════════════
# FLASK ROUTES
# ════════════════════════════════════════════════

def verify_meta_webhook():
    """GET /webhook — Meta subscription verification (hub.challenge)."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        log.info("WhatsApp webhook verified (GET)")
        return Response(challenge, status=200, mimetype="text/plain")
    log.warning("WhatsApp webhook verification failed")
    return "Forbidden", 403


def _verify_webhook_signature() -> bool:
    """Verify Meta X-Hub-Signature-256 header. Returns True if valid or skipped."""
    if not WHATSAPP_APP_SECRET:
        return True  # skip verification if secret not configured
    signature_header = request.headers.get("X-Hub-Signature-256")
    if not signature_header:
        log.warning("Webhook POST missing X-Hub-Signature-256 header")
        return False
    expected = "sha256=" + hmac.new(
        WHATSAPP_APP_SECRET.encode(), request.get_data(), hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        log.warning("Webhook signature mismatch")
        return False
    return True


def handle_meta_webhook_post():
    """POST /webhook — incoming messages."""
    if not _verify_webhook_signature():
        return "Forbidden", 403

    body = request.get_json(silent=True)
    if body is None:
        return "", 200

    messages = parse_meta_webhook_payload(body)
    for msg in messages:
        from_number = msg["from"]
        incoming_msg = msg["text"]
        log.info(f"Incoming: {from_number} — '{incoming_msg[:80]}'")

        try:
            handle_message(from_number, incoming_msg)
        except Exception as e:
            log.error(f"Webhook error: {e}", exc_info=True)
            send(
                from_number,
                "Something went wrong. Please try again.\n"
                "Support: +91 7981053846",
            )

    return "", 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Meta WhatsApp Cloud API — verify (GET) or receive messages (POST)."""
    if request.method == "GET":
        return verify_meta_webhook()
    return handle_meta_webhook_post()


@app.route("/health", methods=["GET"])
def health():
    """Health check."""
    return {
        "status":  "ok",
        "service": "BilledUp WhatsApp Webhook",
        "time":    datetime.utcnow().isoformat(),
    }, 200


@app.route("/bills/<filename>", methods=["GET"])
def serve_bill(filename):
    """Serve PDF bills from database."""
    if not re.match(r'^[\w\-]+\.pdf$', filename):
        return {"error": "Invalid filename"}, 400
    # Strip .pdf, then strip optional 3-char random suffix (e.g., INV-2026-SHOP-00001-abc.pdf)
    base = filename[:-4]
    m = re.match(r'^(.+)-[a-z]{3}$', base)
    invoice_number = m.group(1) if m else base
    with db_session() as session:
        bill = session.query(Bill).filter_by(invoice_number=invoice_number).first()
        if not bill or not bill.pdf_data:
            return {"error": "Bill not found"}, 404
        return Response(
            bill.pdf_data,
            mimetype="application/pdf",
            headers={"Content-Disposition": f"inline; filename={filename}"},
        )


@app.route("/reports/<filename>", methods=["GET"])
def serve_report(filename):
    """Serve GST report PDFs from database."""
    if not re.match(r'^[\w\-]+\.pdf$', filename):
        return {"error": "Invalid filename"}, 400
    with db_session() as session:
        report = session.query(ReportPDF).filter_by(filename=filename).first()
        if not report:
            return {"error": "Report not found"}, 404
        return Response(
            report.pdf_data,
            mimetype="application/pdf",
            headers={"Content-Disposition": f"inline; filename={filename}"},
        )


@app.route("/admin/registrations", methods=["GET"])
def admin_registrations():
    """
    Admin view — see all registered shopkeepers.
    Requires X-Admin-Key header matching ADMIN_SECRET env var.
    """
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or request.headers.get("X-Admin-Key") != admin_secret:
        return {"error": "Unauthorized"}, 403

    with db_session() as session:
        rows = session.query(Registration).order_by(
            Registration.created_at.desc()
        ).all()

    result = []
    for r in rows:
        reg = {
            "phone": r.phone, "shop_name": r.shop_name,
            "address": r.address, "gstin": r.gstin,
            "invoice_type": r.invoice_type or "TAX_INVOICE",
            "state": r.state, "trial_end": r.trial_end.isoformat() if r.trial_end else None,
            "bills_count": r.bills_count,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        if r.trial_end:
            reg["days_left"] = max(0, (r.trial_end - datetime.utcnow()).days)
        else:
            reg["days_left"] = 0
        result.append(reg)

    return {"total": len(result), "registrations": result}, 200


# ════════════════════════════════════════════════
# API-AUTHENTICATED ENDPOINTS
# ════════════════════════════════════════════════

def _get_api_shop():
    """Extract and validate API key from request. Returns (Shop, None) or (None, error_response)."""
    api_key = request.headers.get("X-API-Key", "")
    if not api_key:
        return None, ({"error": "Missing X-API-Key header"}, 401)
    shop = validate_api_key(api_key)
    if not shop:
        return None, ({"error": "Invalid or inactive API key"}, 403)
    return shop, None


@app.route("/api/bill", methods=["POST"])
def api_generate_bill():
    """
    Generate a bill via API (authenticated with shop API key).

    Headers:
        X-API-Key: bu_xxxx...

    Body (JSON):
        {
            "items": [{"name": "shirt", "qty": 1, "price": 500}],
            "customer_name": "Suresh"
        }

    Returns: JSON with invoice details + PDF URL.
    """
    shop_row, err = _get_api_shop()
    if err:
        return err

    data = request.get_json(silent=True)
    if not data:
        return {"error": "Request body must be JSON"}, 400

    items_data = data.get("items", [])
    if not items_data:
        return {"error": "No items provided"}, 400

    customer_name = data.get("customer_name", "Customer")

    # Build objects
    from bill_generator import ShopProfile, CustomerInfo, BillItem
    shop = ShopProfile(
        shop_id    = shop_row.shop_id,
        name       = shop_row.name,
        address    = shop_row.address,
        gstin      = shop_row.gstin,
        phone      = shop_row.phone,
        upi        = shop_row.upi or "",
        state      = shop_row.state,
        state_code = shop_row.state_code,
    )

    try:
        customer = CustomerInfo(name=customer_name)
        items = [
            BillItem(name=i["name"], qty=i.get("qty", 1), price=i["price"])
            for i in items_data
        ]
    except (KeyError, TypeError) as e:
        return {"error": f"Invalid item data: {e}"}, 400

    # Generate
    try:
        invoice_number = generate_invoice_number(shop.shop_id)
        pdf_data, bill_result = generate_pdf_bill(
            shop=shop, customer=customer, items=items,
            invoice_number=invoice_number, gst_client=get_anthropic_client(),
        )
    except Exception as e:
        log.error(f"API bill generation failed: {e}", exc_info=True)
        return {"error": f"Bill generation failed: {e}"}, 500

    # Save
    try:
        save_bill(
            shop_id=shop.shop_id, invoice_number=invoice_number,
            customer_name=customer_name, customer_phone="",
            items=bill_result.items, bill_result=bill_result,
            pdf_data=pdf_data, raw_message=str(data),
        )
    except Exception as e:
        log.error(f"DB save failed (non-fatal): {e}")

    suffix = ''.join(random.choices(string.ascii_lowercase, k=3))
    pdf_filename = f"{invoice_number}-{suffix}.pdf"
    pdf_url = f"{BASE_URL}/bills/{pdf_filename}" if BASE_URL else pdf_filename

    return {
        "success":        True,
        "invoice_number": invoice_number,
        "customer":       customer_name,
        "items_count":    len(items),
        "subtotal":       bill_result.subtotal,
        "total_gst":      bill_result.total_gst,
        "grand_total":    bill_result.grand_total,
        "in_words":       bill_result.in_words,
        "pdf_url":        pdf_url,
    }, 201


@app.route("/api/history", methods=["GET"])
def api_bill_history():
    """Get bill history for authenticated shop."""
    shop_row, err = _get_api_shop()
    if err:
        return err
    from main import get_bill_history
    history = get_bill_history(shop_row.shop_id, limit=20)
    return {"shop_id": shop_row.shop_id, "bills": history}, 200


@app.route("/api/today", methods=["GET"])
def api_today_summary():
    """Get today's summary for authenticated shop."""
    shop_row, err = _get_api_shop()
    if err:
        return err
    summary = get_today_summary(shop_row.shop_id)
    return {"shop_id": shop_row.shop_id, **summary}, 200


# ════════════════════════════════════════════════
# STARTUP
# ════════════════════════════════════════════════

# Always init tables (works with both gunicorn and python direct)
init_database()
init_registration_tables()

if not WHATSAPP_APP_SECRET:
    log.warning("WHATSAPP_APP_SECRET not set — webhook signature verification disabled")

if __name__ == "__main__":
    # ── PRODUCTION AUTO-SWITCH ──
    # If running on Railway (or any production env), exec into Gunicorn
    # instead of using Flask's dev server. This handles the case where
    # Railway runs "python whatsapp_webhook.py" instead of the Procfile.
    _is_production = bool(
        os.getenv("RAILWAY_ENVIRONMENT")
        or os.getenv("RAILWAY_PROJECT_ID")
    )

    if _is_production:
        _port = os.environ.get("PORT", "5000")
        log.info(f"Production detected — switching to Gunicorn on port {_port}")
        os.execvp("gunicorn", [
            "gunicorn", "whatsapp_webhook:app",
            "--bind", f"0.0.0.0:{_port}",
            "--workers", "4",
            "--threads", "2",
            "--worker-class", "gthread",
            "--timeout", "120",
            "--max-requests", "1000",
            "--max-requests-jitter", "100",
            "--preload",
        ])
        # os.execvp replaces this process — nothing below runs

    # ── LOCAL DEV ONLY ──
    from config import DEBUG as debug_mode

    print("\n" + "="*55)
    print("  BilledUp WhatsApp Webhook — LOCAL DEV SERVER")
    print(f"  Debug mode: {debug_mode}")
    print("  Bill smarter. Grow faster.")
    print("="*55)
    print(f"  Phone number ID: {os.getenv('WHATSAPP_PHONE_NUMBER_ID', '(set in .env)')}")
    print(f"  Webhook URL    : http://localhost:5000/webhook")
    print(f"  Health check   : http://localhost:5000/health")
    print(f"  Admin panel    : http://localhost:5000/admin/registrations")
    print("="*55)
    print("\n  NOTE: For production, use Gunicorn via Procfile.")
    print("  This Flask dev server is for local testing only.")
    print("="*55 + "\n")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
