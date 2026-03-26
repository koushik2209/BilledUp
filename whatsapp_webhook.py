"""
whatsapp_webhook.py
BillEasy - Production Grade WhatsApp Integration
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
    PLACEHOLDER_GSTIN,
)
from main import (
    init_database, seed_demo_shop,
    get_shop, save_bill,
    get_today_summary,
)
import sqlite3
from contextlib import contextmanager

# ── Logging ──
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt= "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("billeasy.whatsapp")

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

# ── Database ──
from config import DATABASE_URL
DB_PATH = DATABASE_URL.replace("sqlite:///", "")

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

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
    """Create registration and conversation state tables."""
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS registrations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                phone         TEXT UNIQUE NOT NULL,
                shop_name     TEXT DEFAULT '',
                address       TEXT DEFAULT '',
                gstin         TEXT DEFAULT '',
                state         TEXT DEFAULT 'NEW',
                trial_start   TEXT,
                trial_end     TEXT,
                active        INTEGER DEFAULT 0,
                bills_count   INTEGER DEFAULT 0,
                created_at    TEXT DEFAULT (datetime('now')),
                updated_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS conversation_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                phone      TEXT NOT NULL,
                direction  TEXT NOT NULL,
                message    TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
    log.info("Registration tables initialised")


def get_registration(phone: str) -> dict | None:
    """Get registration record for a phone number."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM registrations WHERE phone = ?",
            (phone,)
        ).fetchone()
        return dict(row) if row else None


ALLOWED_REG_COLUMNS = {
    "phone", "shop_name", "address", "gstin", "state",
    "trial_start", "trial_end", "active", "bills_count", "updated_at",
}

def upsert_registration(phone: str, **fields):
    """Create or update a registration record (column-name whitelist enforced)."""
    fields["updated_at"] = datetime.now().isoformat()

    for key in fields:
        if key not in ALLOWED_REG_COLUMNS:
            raise ValueError(f"Invalid registration field: {key}")

    existing = get_registration(phone)

    with db() as conn:
        if not existing:
            fields["phone"] = phone
            cols = ", ".join(fields.keys())
            placeholders = ", ".join(["?" for _ in fields])
            conn.execute(
                f"INSERT INTO registrations ({cols}) VALUES ({placeholders})",
                list(fields.values())
            )
        else:
            sets = ", ".join([f"{k} = ?" for k in fields])
            conn.execute(
                f"UPDATE registrations SET {sets} WHERE phone = ?",
                list(fields.values()) + [phone]
            )


def log_message(phone: str, direction: str, message: str):
    """Log every message for debugging."""
    with db() as conn:
        conn.execute(
            "INSERT INTO conversation_log (phone, direction, message) VALUES (?, ?, ?)",
            (phone, direction, message[:1000])
        )


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
    with db() as conn:
        # Check if shop already exists
        existing = conn.execute(
            "SELECT id FROM shops WHERE shop_id = ?", (shop_id,)
        ).fetchone()

        if not existing:
            conn.execute("""
                INSERT INTO shops
                    (shop_id, name, address, gstin, phone, upi, state, state_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                shop_id,
                shop_name,
                address,
                gstin or PLACEHOLDER_GSTIN,
                phone.replace("whatsapp:", ""),
                "",
                "Telangana",
                "36",
            ))

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
    return shop_id


def get_shop_id(phone: str) -> str:
    """Get shop_id from phone number."""
    return "S" + re.sub(r"\D", "", phone)[-8:]


# ════════════════════════════════════════════════
# MESSAGE TEMPLATES
# ════════════════════════════════════════════════

def msg_welcome() -> str:
    return (
        "👋 *Welcome to BillEasy!*\n\n"
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


def msg_activated(shop_name: str, days: int) -> str:
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
        f"• *help* — Show this message\n\n"
        f"Try generating your first bill now! 👆"
    )


def msg_help(shop_name: str, days: int) -> str:
    return (
        f"📖 *BillEasy Help*\n\n"
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
        return (
            f"📊 *Today's Summary*\n\n"
            f"Shop: {shop_name}\n"
            f"Date: {summary['date']}\n\n"
            f"Bills generated: *{summary['bill_count']}*\n"
            f"Total sales: *Rs.{summary['total_value']:.2f}*\n"
            f"GST collected: *Rs.{summary['total_gst']:.2f}*\n\n"
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

    lines += [
        f"\n━━━━━━━━━━━━━━━━━",
        f"Subtotal:  Rs.{bill_result.subtotal:.2f}",
        f"CGST:      Rs.{bill_result.total_cgst:.2f}",
        f"SGST:      Rs.{bill_result.total_sgst:.2f}",
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
        f"To continue generating bills — upgrade to BillEasy Standard:\n\n"
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

GSTIN_REGEX = re.compile(
    r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"
)

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
    try:
        get_twilio_client().messages.create(
            from_     = TWILIO_WHATSAPP_NUMBER,
            to        = to,
            body      = caption or f"📄 Invoice: {filename}",
            media_url = [media_url],
        )
        log_message(to, "OUT", f"[PDF] {media_url}")
        log.info(f"PDF sent to {to}: {media_url}")
    except Exception as e:
        log.error(f"PDF send failed to {to}: {e}")
        send(to, f"📄 Your bill PDF is ready but could not be attached.\nFilename: {filename}")


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
            shop_id = activate_trial(from_number, shop_name, address, "")
            d_left  = days_left(get_registration(from_number))
            send(from_number, msg_activated(shop_name, d_left))
            return

        gstin = message.strip().upper()
        if not is_valid_gstin(gstin):
            send(from_number, msg_invalid_gstin())
            return

        # Valid GSTIN — activate trial
        shop_id = activate_trial(from_number, shop_name, address, gstin)
        d_left  = days_left(get_registration(from_number))
        send(from_number, msg_activated(shop_name, d_left))
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

        # ── Generate bill ──
        try:
            # Acknowledge immediately
            send(from_number, "⏳ Generating your bill... 10 seconds.")

            # Parse message
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

            # Load shop profile
            shop = get_shop(shop_id)
            if not shop:
                # Rebuild shop from registration
                shop = ShopProfile(
                    shop_id    = shop_id,
                    name       = reg.get("shop_name", "Shop"),
                    address    = reg.get("address", "Hyderabad"),
                    gstin      = reg.get("gstin") or PLACEHOLDER_GSTIN,
                    phone      = from_number.replace("whatsapp:", ""),
                    state      = "Telangana",
                    state_code = "36",
                    upi        = "",
                )

            # Build bill objects
            customer = CustomerInfo(name=parsed["customer_name"])
            items = [
                BillItem(name=i["name"], qty=i["qty"], price=i["price"])
                for i in parsed["items"]
            ]

            # Generate invoice and PDF (returns path + BillResult)
            invoice_number = generate_invoice_number(shop_id)
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
                    shop_id        = shop_id,
                    invoice_number = invoice_number,
                    customer_name  = parsed["customer_name"],
                    customer_phone = from_number,
                    items          = bill_result.items,
                    bill_result    = bill_result,
                    pdf_path       = pdf_path,
                    raw_message    = message,
                    confidence     = parsed.get("confidence", 1.0),
                )
            except Exception as e:
                log.error(f"DB save failed (non-fatal): {e}")

            # Update bill count
            upsert_registration(
                from_number,
                bills_count=reg.get("bills_count", 0) + 1
            )

            # Send bill summary + PDF
            summary = msg_bill_summary(
                bill_result    = bill_result,
                invoice_number = invoice_number,
                customer_name  = parsed["customer_name"],
                days           = d_left,
            )
            send(from_number, summary)

            # Send PDF as media attachment
            send_pdf(
                to       = from_number,
                pdf_path = pdf_path,
                caption  = f"📄 Invoice {invoice_number} — Rs.{bill_result.grand_total:.2f}",
            )

            log.info(
                f"Bill generated: {invoice_number} "
                f"for {shop_name} "
                f"total=Rs.{bill_result.grand_total:.2f}"
            )

        except Exception as e:
            log.error(f"Bill generation failed: {e}", exc_info=True)
            send(from_number,
                f"❌ Something went wrong. Please try again.\n\n"
                f"Support: +91 7981053846"
            )
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
        if not validator.validate(request.url, request.form, signature):
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
        "service": "BillEasy WhatsApp Webhook",
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
    with db() as conn:
        rows = conn.execute("""
            SELECT phone, shop_name, address, gstin,
                   state, trial_end, bills_count, created_at
            FROM registrations
            ORDER BY created_at DESC
        """).fetchall()

    result = []
    for r in rows:
        reg = dict(r)
        if reg.get("trial_end"):
            trial_end = datetime.fromisoformat(reg["trial_end"])
            reg["days_left"] = max(0, (trial_end - datetime.now()).days)
        else:
            reg["days_left"] = 0
        result.append(reg)

    return {"total": len(result), "registrations": result}, 200


# ════════════════════════════════════════════════
# STARTUP
# ════════════════════════════════════════════════

if __name__ == "__main__":
    # Init all tables
    init_database()
    init_registration_tables()

    print("\n" + "="*55)
    print("  BillEasy WhatsApp Webhook — Production")
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
