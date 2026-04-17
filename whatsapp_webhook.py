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
    try_claim_message, maybe_cleanup_processed_messages,
    ensure_schema,
)
from reports import (
    get_gst_report, parse_report_range, msg_gst_report,
    export_gst_report_pdf,
)
from return_detector import detect_return_intent, negate_items
from api.formatters import (
    msg_welcome,
    msg_ask_address,
    msg_ask_gstin,
    msg_ask_state,
    msg_activated,
    msg_help,
    msg_bill_summary,
    msg_trial_expired,
    msg_invalid_gstin,
    msg_state_prompt,
    _STATE_MENU,
)
from services.pending import (
    PendingBill,
    PENDING_EXPIRY_MINUTES,
    _serialize_pending,
    _deserialize_pending,
    store_pending,
    get_pending_bill,
    clear_pending,
    cleanup_expired_pending,
)
from services.registration import (
    init_registration_tables,
    get_registration,
    ALLOWED_REG_FIELDS,
    upsert_registration,
    log_message,
    is_trial_active,
    days_left,
    activate_trial,
    get_shop_id,
    is_valid_gstin,
    INDIAN_STATES,
    resolve_state,
)
from services.billing import (
    send,
    send_pdf,
    msg_today_summary,
    msg_history,
    _compute_preview_totals,
    msg_preview,
    _CONFIRM_COMMANDS,
    _is_confirmation_command,
    _handle_gst_report,
    _handle_new_bill,
    _match_item_by_name,
    _handle_confirmation,
    _handle_state_selection,
    _check_recent_duplicate,
    _generate_confirmed_bill,
    _handle_myitems,
    _handle_gst_update,
)
from services.router import handle_message

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
# ASKED_STATE   → we asked which Indian state the shop is in
# ACTIVE        → registered and billing
# EXPIRED       → trial ended
# ════════════════════════════════════════════════

# ── init_registration_tables, get_registration, upsert_registration,
#    log_message, is_trial_active, days_left, activate_trial,
#    get_shop_id → moved to services/registration.py ──



# ════════════════════════════════════════════════
# BUSINESS LOGIC — moved to services/
# ════════════════════════════════════════════════
# msg_today_summary, msg_history        → services/billing.py
# send, send_pdf                        → services/billing.py
# _compute_preview_totals, msg_preview  → services/billing.py
# _is_confirmation_command              → services/billing.py
# _handle_gst_report                    → services/billing.py
# _handle_new_bill, _match_item_by_name → services/billing.py
# _handle_confirmation                  → services/billing.py
# _handle_state_selection               → services/billing.py
# _check_recent_duplicate               → services/billing.py
# _generate_confirmed_bill              → services/billing.py
# _handle_myitems, _handle_gst_update   → services/billing.py
# handle_message                        → services/router.py

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


def verify_whatsapp_signature(req) -> bool:
    """
    Verify Meta X-Hub-Signature-256 header.

    STRICT: rejects the request if WHATSAPP_APP_SECRET is not configured,
    if the signature header is missing, or if the HMAC does not match.
    Uses hmac.compare_digest for constant-time comparison.
    """
    if not WHATSAPP_APP_SECRET:
        log.error(
            "WHATSAPP_APP_SECRET not configured — refusing to process webhook. "
            "Set this env var to enable signature verification."
        )
        return False

    signature_header = req.headers.get("X-Hub-Signature-256")
    if not signature_header:
        log.warning("Webhook POST missing X-Hub-Signature-256 header")
        return False

    expected = "sha256=" + hmac.new(
        WHATSAPP_APP_SECRET.encode(), req.get_data(), hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        log.warning("Webhook signature mismatch — rejecting request")
        return False

    return True


def handle_meta_webhook_post():
    """POST /webhook — incoming messages."""
    if not verify_whatsapp_signature(request):
        return "Forbidden", 403

    body = request.get_json(silent=True)
    if body is None:
        return "", 200

    # Throttled cleanup — runs once every ~100 webhook calls, not every request
    maybe_cleanup_processed_messages()

    messages = parse_meta_webhook_payload(body)
    for msg in messages:
        from_number = msg["from"]
        incoming_msg = msg["text"]
        message_id = msg.get("message_id", "")

        # ── Dedup: INSERT-FIRST pattern ──
        if not message_id:
            log.warning(f"[DEDUP] Missing message_id from {from_number} — skipping dedup")
        elif not try_claim_message(message_id):
            log.info(f"[DEDUP] Duplicate ignored: message_id={message_id} from={from_number}")
            continue

        log.info(f"[DEDUP] New message: message_id={message_id[:20] if message_id else '(none)'} from={from_number}")

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

# Validate schema matches models — auto-reset in DEV_MODE, log-only in production
from config import DEV_MODE
ensure_schema(dev_mode=DEV_MODE)

if not WHATSAPP_APP_SECRET:
    log.error(
        "WHATSAPP_APP_SECRET not set — ALL webhook POST requests will be rejected. "
        "Set this env var before deploying to production."
    )

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
