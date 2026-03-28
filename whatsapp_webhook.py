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
- Twilio request signature validation
- Graceful error handling
"""

import os
import re
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator

from config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_WHATSAPP_NUMBER,
    PLATFORM_NAME,
    BASE_URL,
    get_anthropic_client,
)
from claude_parser import parse_message
from bill_generator import (
    ShopProfile, CustomerInfo, BillItem,
    generate_invoice_number, generate_pdf_bill,
    PLACEHOLDER_GSTIN, GSTIN_REGEX,
)
from main import (
    init_database, seed_demo_shop,
    get_shop, save_bill,
    get_today_summary,
)
from database import (
    db_session, Registration, ConversationLog, Shop,
    init_database as init_db,
    generate_api_key, validate_api_key,
)

# ── Logging ──
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt= "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("billedup.whatsapp")

# ── Flask ──
app = Flask(__name__)

# ── Lazy Twilio client (avoids crash when creds are missing) ──
_twilio_client = None

def get_twilio_client():
    global _twilio_client
    if _twilio_client is None:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
            raise RuntimeError(
                "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set"
            )
        from twilio.rest import Client
        _twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client

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
            "state": row.state, "trial_start": row.trial_start.isoformat() if row.trial_start else None,
            "trial_end": row.trial_end.isoformat() if row.trial_end else None,
            "active": row.active, "bills_count": row.bills_count,
        }


ALLOWED_REG_FIELDS = {
    "shop_name", "address", "gstin", "state",
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
    return datetime.now() < trial_end


def days_left(reg: dict) -> int:
    """Days remaining in trial."""
    if not reg.get("trial_end"):
        return 0
    trial_end = datetime.fromisoformat(reg["trial_end"])
    delta = trial_end - datetime.now()
    return max(0, delta.days)


def activate_trial(phone: str, shop_name: str, address: str, gstin: str = ""):
    """
    Activate 10 day free trial for a new shopkeeper.
    Creates shop in database and marks registration active.
    """
    trial_start = datetime.now()
    trial_end   = trial_start + timedelta(days=10)

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
        shop_name   = shop_name,
        address     = address,
        gstin       = gstin,
        state       = "ACTIVE",
        trial_start = trial_start.isoformat(),
        trial_end   = trial_end.isoformat(),
        active      = 1,
    )

    log.info(f"Trial activated for {phone} — shop_id={shop_id} — ends {trial_end.date()}")
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


def msg_activated(shop_name: str, days: int, api_key: str = "") -> str:
    key_line = f"\n🔑 *Your API Key:*\n`{api_key}`\n_Keep this safe — use it for API access._\n" if api_key else ""
    return (
        f"🎊 *You are all set, {shop_name}!*\n\n"
        f"Your *{days}-day free trial* has started.\n"
        f"After trial: just Rs.299/month.\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"*How to generate a bill:*\n\n"
        f"Just type your items and prices:\n\n"
        f"_phone case 299 charger 499 customer Suresh_\n\n"
        f"Your GST bill will be ready in 10 seconds! ⚡\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"*Commands:*\n"
        f"• *today* — Today's sales summary\n"
        f"• *history* — Last 5 bills\n"
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


def msg_bill_summary(bill_result, invoice_number: str, customer_name: str, days: int) -> str:
    lines = [
        f"✅ *Bill Generated!*\n",
        f"📋 Invoice: *{invoice_number}*",
        f"👤 Customer: *{customer_name}*\n",
        f"*Items:*",
    ]
    for item in bill_result.items:
        qty = int(item.qty) if item.qty == int(item.qty) else item.qty
        lines.append(f"• {item.name} x{qty} — Rs.{item.total:.2f} ({item.gst_rate}% GST)")

    lines.append(f"\n━━━━━━━━━━━━━━━━━")
    lines.append(f"Subtotal:  Rs.{bill_result.subtotal:.2f}")
    if bill_result.is_igst:
        lines.append(f"IGST:      Rs.{bill_result.total_igst:.2f}")
    else:
        lines.append(f"CGST:      Rs.{bill_result.total_cgst:.2f}")
        lines.append(f"SGST:      Rs.{bill_result.total_sgst:.2f}")
    lines += [
        f"Total GST: Rs.{bill_result.total_gst:.2f}",
        f"━━━━━━━━━━━━━━━━━",
        f"*TOTAL: Rs.{bill_result.grand_total:.2f}*\n",
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
    """Send WhatsApp message via Twilio."""
    try:
        get_twilio_client().messages.create(
            from_ = TWILIO_WHATSAPP_NUMBER,
            to    = to,
            body  = body,
        )
        log_message(to, "OUT", body)
        log.info(f"Sent to {to} ({len(body)} chars)")
    except Exception as e:
        log.error(f"Send failed to {to}: {e}")


def send_pdf(to: str, pdf_path: str, caption: str = ""):
    """Send a PDF bill as a WhatsApp media message via Twilio.

    Requires BASE_URL (NGROK_URL) to be set so Twilio can fetch the file.
    Falls back to a text-only notice if BASE_URL is not configured.
    """
    filename = os.path.basename(pdf_path)

    if not BASE_URL:
        log.warning("BASE_URL not set — cannot send PDF media. Sending text fallback.")
        send(to, f"📄 Your bill PDF is ready: {filename}\n(Ask shop to share the file)")
        return

    media_url = f"{BASE_URL}/bills/{filename}"
    log.info(f"Sending PDF: {media_url} to {to}")
    try:
        msg = get_twilio_client().messages.create(
            from_     = TWILIO_WHATSAPP_NUMBER,
            to        = to,
            body      = caption or f"📄 Invoice: {filename}",
            media_url = [media_url],
        )
        log_message(to, "OUT", f"[PDF] {media_url}")
        log.info(f"PDF sent to {to}: sid={msg.sid} status={msg.status}")
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

    # Partial / substring match
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


_pending_bills: dict[str, PendingBill] = {}
_pending_lock = threading.Lock()


def store_pending(phone: str, bill: PendingBill):
    """Store a pending bill for a phone number (replaces any existing)."""
    with _pending_lock:
        _pending_bills[phone] = bill


def get_pending_bill(phone: str) -> PendingBill | None:
    """Get pending bill if exists and not expired. Returns None if expired/missing."""
    with _pending_lock:
        bill = _pending_bills.get(phone)
        if not bill:
            return None
        age = (datetime.now() - bill.created_at).total_seconds()
        if age > PENDING_EXPIRY_MINUTES * 60:
            del _pending_bills[phone]
            return None
        return bill


def clear_pending(phone: str):
    """Remove pending bill for a phone number."""
    with _pending_lock:
        _pending_bills.pop(phone, None)


def cleanup_expired_pending():
    """Remove all expired pending bills. Called on each incoming message."""
    cutoff = datetime.now() - timedelta(minutes=PENDING_EXPIRY_MINUTES)
    with _pending_lock:
        expired = [p for p, b in _pending_bills.items() if b.created_at < cutoff]
        for p in expired:
            del _pending_bills[p]


# ════════════════════════════════════════════════
# PREVIEW + CONFIRMATION MESSAGES
# ════════════════════════════════════════════════

def msg_preview(pending: PendingBill) -> str:
    """Format bill preview message shown before confirmation."""
    lines = [
        "📋 *Bill Preview*\n",
        f"👤 Customer: *{pending.customer_name}*",
    ]

    if pending.customer_state_code == pending.shop_state_code:
        lines.append(f"📍 State: {pending.customer_state} (same as shop)")
        lines.append(f"💰 Tax: CGST + SGST (intra-state)")
    else:
        lines.append(f"📍 State: {pending.customer_state} (Code: {pending.customer_state_code})")
        lines.append(f"💰 Tax: IGST (inter-state)")

    lines.append(f"\n*Items:*")
    for i, item in enumerate(pending.items, 1):
        qty = int(item["qty"]) if item["qty"] == int(item["qty"]) else item["qty"]
        lines.append(f"  {i}. {item['name']} x{qty} — Rs.{item['price']:.2f}")

    if pending.confidence < 0.8:
        lines.append(f"\n⚠️ _Low confidence ({pending.confidence:.0%}) — please verify_")

    lines += [
        f"\n━━━━━━━━━━━━━━━━━",
        f"Reply:",
        f"• *YES* → Generate bill",
        f"• *NAME Ravi* → Change customer name",
        f"• *STATE* → Change customer state",
        f"• *EDIT* → Re-enter items",
        f"• *CANCEL* → Discard",
    ]
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
# CONFIRMATION FLOW HANDLERS
# ════════════════════════════════════════════════

def _handle_new_bill(from_number: str, message: str, reg: dict,
                     shop_id: str, shop_name: str, d_left: int):
    """Parse message → store as pending → show preview."""
    try:
        send(from_number, "⏳ Understanding your message...")

        parsed = parse_message(message)

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

        pending = PendingBill(
            phone              = from_number,
            shop_id            = shop_id,
            shop_name          = shop_name,
            shop_state         = shop_state,
            shop_state_code    = shop_state_code,
            customer_name      = parsed["customer_name"],
            customer_state     = shop_state,       # default: same as shop
            customer_state_code= shop_state_code,  # default: intra-state
            items              = parsed["items"],
            confidence         = parsed.get("confidence", 1.0),
            warnings           = parsed.get("warnings", []),
            raw_message        = message,
            created_at         = datetime.now(),
        )

        store_pending(from_number, pending)
        send(from_number, msg_preview(pending))

    except Exception as e:
        log.error(f"Preview failed: {e}", exc_info=True)
        send(from_number,
            f"❌ Something went wrong. Please try again.\n\n"
            f"Support: +91 7981053846"
        )


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
        pending.created_at = datetime.now()  # refresh expiry
        store_pending(from_number, pending)
        send(from_number, msg_preview(pending))
        return

    # CHANGE STATE / STATE
    if msg_lower in ("change state", "state", "igst"):
        pending.awaiting_state = True
        pending.created_at = datetime.now()
        store_pending(from_number, pending)
        send(from_number, msg_state_prompt())
        return

    # EDIT
    if msg_lower in ("edit", "change", "redo"):
        clear_pending(from_number)
        send(from_number,
            "✏️ Send your bill message again.\n\n"
            "_Example: phone case 299 charger 499 customer Suresh_"
        )
        return

    # Unknown command → re-show preview with hint
    send(from_number,
        f"❓ Didn't understand *{message[:30].strip()}*\n\n"
        + msg_preview(pending)
    )


def _handle_state_selection(from_number: str, message: str,
                            pending: PendingBill, d_left: int):
    """Handle state input after user chose CHANGE STATE."""
    msg_stripped = message.strip()

    # BACK / cancel state change
    if msg_stripped.lower() in ("back", "cancel", "skip"):
        pending.awaiting_state = False
        pending.created_at = datetime.now()
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
    pending.created_at          = datetime.now()
    store_pending(from_number, pending)

    send(from_number, f"✅ State set to *{state_name}* (Code: {state_code})\n")
    send(from_number, msg_preview(pending))


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
            BillItem(name=i["name"], qty=i["qty"], price=i["price"])
            for i in pending.items
        ]

        invoice_number = generate_invoice_number(pending.shop_id)
        pdf_path, bill_result = generate_pdf_bill(
            shop           = shop,
            customer       = customer,
            items          = items,
            invoice_number = invoice_number,
            gst_client     = get_anthropic_client(),
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
                pdf_path       = pdf_path,
                raw_message    = pending.raw_message,
                confidence     = pending.confidence,
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
            bill_result    = bill_result,
            invoice_number = invoice_number,
            customer_name  = pending.customer_name,
            days           = d_left,
        )
        send(from_number, summary)

        send_pdf(
            to       = from_number,
            pdf_path = pdf_path,
            caption  = f"📄 Invoice {invoice_number} — Rs.{bill_result.grand_total:.2f}",
        )

        log.info(
            f"Bill generated: {invoice_number} "
            f"for {pending.shop_name} "
            f"total=Rs.{bill_result.grand_total:.2f}"
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
            # Skip GSTIN — activate with placeholder
            shop_id, api_key = activate_trial(from_number, shop_name, address, "")
            d_left  = days_left(get_registration(from_number))
            send(from_number, msg_activated(shop_name, d_left, api_key))
            return

        gstin = message.strip().upper()
        if not is_valid_gstin(gstin):
            send(from_number, msg_invalid_gstin())
            return

        # Valid GSTIN — activate trial
        shop_id, api_key = activate_trial(from_number, shop_name, address, gstin)
        d_left  = days_left(get_registration(from_number))
        send(from_number, msg_activated(shop_name, d_left, api_key))
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

@app.route("/webhook", methods=["POST"])
def webhook():
    """Main WhatsApp webhook — receives all incoming messages."""
    # Validate Twilio request signature
    if TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        signature = request.headers.get("X-Twilio-Signature", "")
        # Use public URL for validation (Railway proxies HTTPS → HTTP internally)
        url = BASE_URL + "/webhook" if BASE_URL else request.url
        if not validator.validate(url, request.form, signature):
            log.warning(f"Invalid Twilio signature from {request.remote_addr}")
            return "Forbidden", 403

    incoming_msg = request.values.get("Body", "").strip()
    from_number  = request.values.get("From", "")
    num_media    = int(request.values.get("NumMedia", 0))

    log.info(f"Incoming: {from_number} — '{incoming_msg[:80]}'")

    # Ignore media
    if num_media > 0:
        send(from_number, "Please send text messages only.")
        return str(MessagingResponse())

    if not incoming_msg:
        return str(MessagingResponse())

    # Handle message
    try:
        handle_message(from_number, incoming_msg)
    except Exception as e:
        log.error(f"Webhook error: {e}", exc_info=True)
        send(from_number,
            "Something went wrong. Please try again.\n"
            "Support: +91 7981053846"
        )

    return str(MessagingResponse())


@app.route("/health", methods=["GET"])
def health():
    """Health check."""
    return {
        "status":  "ok",
        "service": "BilledUp WhatsApp Webhook",
        "time":    datetime.now().isoformat(),
    }, 200


@app.route("/bills/<filename>", methods=["GET"])
def serve_bill(filename):
    """Serve PDF bills (validated filename)."""
    if not re.match(r'^[\w\-]+\.pdf$', filename):
        return {"error": "Invalid filename"}, 400
    from flask import send_from_directory
    from config import BILLS_FOLDER
    try:
        return send_from_directory(
            os.path.abspath(BILLS_FOLDER),
            filename,
            mimetype="application/pdf",
        )
    except Exception:
        return {"error": "Bill not found"}, 404


@app.route("/admin/registrations", methods=["GET"])
def admin_registrations():
    """
    Simple admin view — see all registered shopkeepers.
    Visit: http://localhost:5000/admin/registrations
    """
    with db_session() as session:
        rows = session.query(Registration).order_by(
            Registration.created_at.desc()
        ).all()

    result = []
    for r in rows:
        reg = {
            "phone": r.phone, "shop_name": r.shop_name,
            "address": r.address, "gstin": r.gstin,
            "state": r.state, "trial_end": r.trial_end.isoformat() if r.trial_end else None,
            "bills_count": r.bills_count,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        if r.trial_end:
            reg["days_left"] = max(0, (r.trial_end - datetime.now()).days)
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
        pdf_path, bill_result = generate_pdf_bill(
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
            pdf_path=pdf_path, raw_message=str(data),
        )
    except Exception as e:
        log.error(f"DB save failed (non-fatal): {e}")

    filename = os.path.basename(pdf_path)
    pdf_url = f"{BASE_URL}/bills/{filename}" if BASE_URL else filename

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

if __name__ == "__main__":

    print("\n" + "="*55)
    print("  BilledUp WhatsApp Webhook — Production")
    print("  Bill smarter. Grow faster.")
    print("="*55)
    print(f"  Twilio number  : {TWILIO_WHATSAPP_NUMBER}")
    print(f"  Webhook URL    : http://localhost:5000/webhook")
    print(f"  Health check   : http://localhost:5000/health")
    print(f"  Admin panel    : http://localhost:5000/admin/registrations")
    print("="*55)
    print("\n  Shopkeeper self-registration flow:")
    print("  1. Shopkeeper clicks button on website")
    print("  2. WhatsApp opens — sends message to Twilio number")
    print("  3. Bot asks shop name → address → GSTIN")
    print("  4. 10 day trial activated automatically")
    print("  5. Shopkeeper starts billing immediately")
    print("="*55)
    print("\n  ngrok setup (run in second terminal):")
    print("  .\\ngrok.exe http 5000")
    print("  Then set webhook in Twilio console to:")
    print("  https://YOUR-NGROK-URL/webhook")
    print("="*55 + "\n")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
