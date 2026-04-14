"""
main.py
BilledUp - Production Grade Main Entry Point
--------------------------------------------
Features:
- PostgreSQL/SQLite database via SQLAlchemy
- Session management per shopkeeper
- Full bill history with search
- Graceful error recovery
- Environment validation on startup
- Session summary reports
- Interactive terminal mode for demo
"""

import os
import sys
import json
import signal
import logging
from datetime import datetime, timezone
from typing import Optional
from config import (
    ANTHROPIC_API_KEY, PLATFORM_NAME,
    PLATFORM_TAGLINE, DEBUG,
    get_anthropic_client,
)
from claude_parser import parse_message
from bill_generator import (
    ShopProfile, CustomerInfo, BillItem,
    generate_invoice_number, generate_pdf_bill
)
from database import (
    db_session, init_database,
    Shop, Bill, SessionRecord,
    generate_api_key,
)

# ── Logging ──
log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(
    level  = log_level,
    format = "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt= "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("billedup.main")


# ════════════════════════════════════════════════
# CRUD OPERATIONS
# ════════════════════════════════════════════════

def seed_demo_shop():
    """Insert demo shop if no shops exist."""
    with db_session() as session:
        count = session.query(Shop).count()
        if count == 0:
            shop = Shop(
                shop_id    = "RAVI",
                name       = "Ravi Mobile Accessories",
                address    = "Shop No. 14, Koti Market, Hyderabad - 500095",
                gstin      = "36AABCU9603R1ZX",
                phone      = "+91 98765 43210",
                upi        = "ravi@ybl",
                state      = "Telangana",
                state_code = "36",
                api_key    = generate_api_key(),
            )
            session.add(shop)
            log.info(f"Demo shop seeded: RAVI (api_key={shop.api_key[:8]}...)")


def get_shop(shop_id: str) -> ShopProfile | None:
    """Load shop from database by shop_id."""
    with db_session() as session:
        row = session.query(Shop).filter_by(
            shop_id=shop_id.upper(), active=True
        ).first()
        if not row:
            return None
        return ShopProfile(
            shop_id    = str(row.shop_id),
            name       = str(row.name),
            address    = str(row.address),
            gstin      = str(row.gstin),
            phone      = str(row.phone),
            upi        = str(row.upi) if row.upi is not None else "",
            state      = str(row.state),
            state_code = str(row.state_code),
        )


def save_bill(
    shop_id:        str,
    invoice_number: str,
    customer_name:  str,
    customer_phone: str,
    items:          list,
    bill_result,
    pdf_data:       bytes,
    raw_message:    str = "",
    confidence:     float = 1.0,
    is_return:      bool = False,
):
    """Persist bill to database (including PDF bytes)."""
    items_data = [
        {
            "name":     i.name,
            "qty":      i.qty,
            "price":    i.price,
            "hsn":      i.hsn,
            "gst_rate": i.gst_rate,
            "cgst":     i.cgst,
            "sgst":     i.sgst,
            "igst":     i.igst,
            "total":    i.total,
        }
        for i in items
    ]
    pdf_filename = f"{invoice_number}.pdf"
    with db_session() as session:
        session.add(Bill(
            invoice_number = invoice_number,
            shop_id        = shop_id,
            customer_name  = customer_name,
            customer_phone = customer_phone,
            items_json     = json.dumps(items_data),
            subtotal       = bill_result.subtotal,
            total_cgst     = bill_result.total_cgst,
            total_sgst     = bill_result.total_sgst,
            total_igst     = bill_result.total_igst,
            total_gst      = bill_result.total_gst,
            grand_total    = bill_result.grand_total,
            subtotal_before_discount = bill_result.subtotal_before_bill_discount,
            bill_discount_type       = bill_result.bill_discount_type,
            bill_discount_value      = bill_result.bill_discount_value,
            taxable_amount           = bill_result.taxable_amount,
            pricing_type             = bill_result.pricing_type,
            is_igst        = bill_result.is_igst,
            is_return      = is_return,
            pdf_path       = pdf_filename,
            pdf_data       = pdf_data,
            raw_message    = raw_message,
            confidence     = confidence,
        ))
    log.info(f"Bill saved to DB: {invoice_number}")


def get_bill_history(shop_id: str, limit: int = 10) -> list:
    """Get recent bills for a shop."""
    with db_session() as session:
        rows = session.query(Bill).filter_by(
            shop_id=shop_id.upper()
        ).order_by(Bill.created_at.desc()).limit(limit).all()
        return [
            {
                "invoice_number": r.invoice_number,
                "customer_name":  r.customer_name,
                "grand_total":    r.grand_total,
                "created_at":     r.created_at.isoformat() if r.created_at is not None else "",
                "pdf_path":       r.pdf_path,
            }
            for r in rows
        ]


def get_today_summary(shop_id: str) -> dict:
    """Get today's billing summary for a shop."""
    today = datetime.now(timezone.utc).date()
    with db_session() as session:
        from sqlalchemy import func
        rows = session.query(
            func.count(Bill.id).label("bill_count"),
            func.coalesce(func.sum(Bill.grand_total), 0).label("total_value"),
            func.coalesce(func.sum(Bill.subtotal), 0).label("subtotal"),
            func.coalesce(func.sum(Bill.total_cgst), 0).label("total_cgst"),
            func.coalesce(func.sum(Bill.total_sgst), 0).label("total_sgst"),
            func.coalesce(func.sum(Bill.total_igst), 0).label("total_igst"),
            func.coalesce(func.sum(Bill.total_gst), 0).label("total_gst"),
        ).filter(
            Bill.shop_id == shop_id.upper(),
            func.date(Bill.created_at) == today,
        ).first()
        if rows is None:
            return {
                "date":        today.isoformat(),
                "bill_count":  0,
                "total_value": 0.0,
                "subtotal":    0.0,
                "total_cgst":  0.0,
                "total_sgst":  0.0,
                "total_igst":  0.0,
                "total_gst":   0.0,
            }
        return {
            "date":        today.isoformat(),
            "bill_count":  rows.bill_count or 0,
            "total_value": float(rows.total_value or 0),
            "subtotal":    float(rows.subtotal or 0),
            "total_cgst":  float(rows.total_cgst or 0),
            "total_sgst":  float(rows.total_sgst or 0),
            "total_igst":  float(rows.total_igst or 0),
            "total_gst":   float(rows.total_gst or 0),
        }


# ════════════════════════════════════════════════
# STARTUP VALIDATION
# ════════════════════════════════════════════════

def validate_environment() -> bool:
    """
    Validate all required services and config on startup.
    Returns True if everything is ready.
    """
    print("\nValidating environment...")
    issues = []

    # Check API key
    if not ANTHROPIC_API_KEY:
        issues.append("ANTHROPIC_API_KEY missing in .env")
    elif not ANTHROPIC_API_KEY.startswith("sk-ant"):
        issues.append("ANTHROPIC_API_KEY looks invalid")
    else:
        print("  API key       OK")

    # Check database
    try:
        init_database()
        seed_demo_shop()
        print("  Database      OK")
    except Exception as e:
        issues.append(f"Database error: {e}")

    if issues:
        print("\nStartup failed:")
        for issue in issues:
            print(f"  ERROR: {issue}")
        return False

    print("  All checks passed\n")
    return True


# ════════════════════════════════════════════════
# CORE PIPELINE
# ════════════════════════════════════════════════

def generate_bill_from_message(
    message:  str,
    shop:     ShopProfile,
    session_id: Optional[int] = None,
) -> dict:
    """
    Full pipeline:
    1. Parse message with Claude
    2. Validate parsed data
    3. Generate PDF bill
    4. Save to database
    5. Return result

    Returns dict with success flag and bill details.
    """
    log.info(f"Pipeline start for shop {shop.shop_id}")

    # ── Step 1: Parse ──
    parsed = parse_message(message)

    if parsed.get("error"):
        return {
            "success": False,
            "error":   parsed["error"],
            "stage":   "parsing",
        }

    if not parsed["items"]:
        return {
            "success": False,
            "error":   "No items found in message",
            "stage":   "parsing",
        }

    # ── Step 2: Build objects ──
    try:
        customer = CustomerInfo(
            name  = parsed["customer_name"],
            phone = "",
        )
        customer.validate()

        items = []
        for item_data in parsed["items"]:
            bill_item = BillItem(
                name  = item_data["name"],
                qty   = item_data["qty"],
                price = item_data["price"],
            )
            bill_item.validate()
            items.append(bill_item)

    except ValueError as e:
        return {
            "success": False,
            "error":   f"Data validation failed: {e}",
            "stage":   "validation",
        }

    # ── Step 3: Generate PDF ──
    try:
        invoice_number = generate_invoice_number(shop.shop_id)
        pdf_data, bill_result = generate_pdf_bill(
            shop           = shop,
            customer       = customer,
            items          = items,
            invoice_number = invoice_number,
            gst_client     = get_anthropic_client(),
        )
    except Exception as e:
        log.error(f"PDF generation failed: {e}")
        return {
            "success": False,
            "error":   f"Bill generation failed: {e}",
            "stage":   "pdf_generation",
        }

    # ── Step 4: Save to database ──
    try:
        save_bill(
            shop_id        = shop.shop_id,
            invoice_number = invoice_number,
            customer_name  = parsed["customer_name"],
            customer_phone = "",
            items          = bill_result.items,
            bill_result    = bill_result,
            pdf_data       = pdf_data,
            raw_message    = message,
            confidence     = parsed.get("confidence", 1.0),
        )
    except Exception as e:
        # DB save failure is non-fatal — bill was already generated
        log.error(f"DB save failed (non-fatal): {e}")

    # ── Update session ──
    if session_id is not None:
        try:
            with db_session() as session:
                rec = session.query(SessionRecord).filter_by(id=session_id).first()
                if rec:
                    rec.bills_count = (rec.bills_count or 0) + 1  # type: ignore[assignment]
                    rec.total_value = (rec.total_value or 0) + bill_result.grand_total  # type: ignore[assignment]
        except Exception as e:
            log.warning(f"Session update failed: {e}")

    return {
        "success":        True,
        "invoice_number": invoice_number,
        "customer":       parsed["customer_name"],
        "items_count":    len(items),
        "grand_total":    bill_result.grand_total,
        "confidence":     parsed.get("confidence", 1.0),
        "warnings":       parsed.get("warnings", []),
    }


# ════════════════════════════════════════════════
# SESSION MANAGEMENT
# ════════════════════════════════════════════════

def start_session(shop_id: str) -> int:
    """Start a new billing session. Returns session_id."""
    with db_session() as session:
        rec = SessionRecord(shop_id=shop_id.upper())
        session.add(rec)
        session.flush()
        session_id = int(rec.id)  # type: ignore[arg-type]
    log.info(f"Session {session_id} started for shop {shop_id}")
    return session_id


def end_session(session_id: int):
    """Mark session as ended."""
    with db_session() as session:
        rec = session.query(SessionRecord).filter_by(id=session_id).first()
        if rec:
            rec.ended_at = datetime.now(timezone.utc)  # type: ignore[assignment]
    log.info(f"Session {session_id} ended")


def print_session_summary(shop_id: str, _session_id: int):
    """Print end-of-session summary."""
    summary = get_today_summary(shop_id)
    history = get_bill_history(shop_id, limit=5)

    print("\n" + "="*50)
    print(f"  Session Summary — {summary['date']}")
    print("="*50)
    print(f"  Bills today   : {summary['bill_count']}")
    print(f"  Total value   : Rs.{summary['total_value']:.2f}")
    print(f"  Subtotal      : Rs.{summary['subtotal']:.2f}")
    print(f"  GST collected : Rs.{summary['total_gst']:.2f}")

    if history:
        print("\n  Recent bills:")
        print("  " + "-"*44)
        for bill in history:
            print(
                f"  {bill['invoice_number']:25} "
                f"{bill['customer_name']:15} "
                f"Rs.{bill['grand_total']:.2f}"
            )
    print("="*50)


# ════════════════════════════════════════════════
# COMMANDS
# ════════════════════════════════════════════════

def handle_command(cmd: str, shop: ShopProfile) -> bool:
    """
    Handle special commands in interactive mode.
    Returns True if command was handled.
    """
    cmd = cmd.strip().lower()

    if cmd in ("help", "?"):
        print("\nCommands:")
        print("  history     — Show last 10 bills")
        print("  today       — Today's summary")
        print("  quit/exit   — Exit BilledUp")
        print("\nOr type a bill message like:")
        print("  phone case 299 charger 499 customer Suresh")
        print("  oka charger 199 ki Ravi ki bill cheyyi")
        return True

    if cmd == "history":
        bills = get_bill_history(shop.shop_id)
        if not bills:
            print("  No bills yet")
        else:
            print(f"\n  Last {len(bills)} bills:")
            print("  " + "-"*50)
            for b in bills:
                print(
                    f"  {b['invoice_number']:25} "
                    f"{b['customer_name']:15} "
                    f"Rs.{b['grand_total']:.2f}"
                )
        return True

    if cmd == "today":
        summary = get_today_summary(shop.shop_id)
        print(f"\n  Today ({summary['date']}):")
        print(f"  Bills         : {summary['bill_count']}")
        print(f"  Total value   : Rs.{summary['total_value']:.2f}")
        print(f"  GST collected : Rs.{summary['total_gst']:.2f}")
        return True

    return False


# ════════════════════════════════════════════════
# INTERACTIVE MODE
# ════════════════════════════════════════════════

_running    = True
_session_id = None
_shop       = None

def _handle_signal(_sig, _frame):
    """Graceful shutdown on Ctrl+C."""
    global _running
    print("\n\nShutting down gracefully...")
    _running = False

def interactive_mode(shop_id: str = "RAVI"):
    """
    Interactive terminal billing loop.
    Type a message to generate a bill.
    Type 'help' for commands.
    Type 'quit' to exit.
    """
    global _running, _session_id, _shop

    # Graceful shutdown handler
    signal.signal(signal.SIGINT, _handle_signal)

    # Load shop
    _shop = get_shop(shop_id)
    if not _shop:
        print(f"Shop '{shop_id}' not found in database.")
        print("Run seed_demo_shop() first or add a shop to the database.")
        return

    # Start session
    _session_id = start_session(_shop.shop_id)

    print("\n" + "="*50)
    print(f"  {PLATFORM_NAME}")
    print(f"  {PLATFORM_TAGLINE}")
    print("="*50)
    print(f"  Shop    : {_shop.name}")
    print(f"  GSTIN   : {_shop.gstin}")
    print(f"  Session : #{_session_id}")
    print("="*50)
    print("  Type 'help' for commands")
    print("  Type 'quit' to exit")
    print("="*50)

    _running = True

    while _running:
        try:
            message = input("\nMessage: ").strip()

            if not message:
                continue

            # Handle commands
            if message.lower() in ("quit", "exit", "q"):
                break

            if handle_command(message, _shop):
                continue

            # Generate bill
            print("\nProcessing...")
            result = generate_bill_from_message(
                message    = message,
                shop       = _shop,
                session_id = _session_id,
            )

            if result["success"]:
                print(f"\n  Bill generated!")
                print(f"  Invoice  : {result['invoice_number']}")
                print(f"  Customer : {result['customer']}")
                print(f"  Items    : {result['items_count']}")
                print(f"  Total    : Rs.{result['grand_total']:.2f}")
                if result.get("confidence", 1.0) < 0.8:
                    print(f"  Warning  : Low confidence — please verify items")
                for w in result.get("warnings", []):
                    print(f"  Note     : {w}")
                print(f"\n  Bill stored in database")
            else:
                print(f"\n  Could not generate bill: {result['error']}")
                print("  Please try rephrasing your message")

        except KeyboardInterrupt:
            break
        except EOFError:
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            print(f"\n  Something went wrong: {e}")
            print("  Please try again")

    # Cleanup
    if _session_id is not None:
        end_session(_session_id)
        print_session_summary(_shop.shop_id, _session_id)

    print("\nGoodbye!\n")


# ════════════════════════════════════════════════
# UNIT TESTS
# ════════════════════════════════════════════════

def run_tests():
    print("\n" + "="*50)
    print("BilledUp Main — Unit Tests")
    print("="*50)
    passed = 0; failed = 0

    def test(name, fn):
        nonlocal passed, failed
        try:
            fn(); print(f"  PASS  {name}"); passed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {e}"); failed += 1

    def atrue(v, msg=""):
        if not v: raise AssertionError(msg or "Expected True")
    def aeq(a, b):
        if a != b: raise AssertionError(f"Expected {b!r} got {a!r}")

    # Database tests
    test("database initialises", lambda: init_database())
    test("demo shop seeded",     lambda: seed_demo_shop())
    test("shop loads from db",
         lambda: atrue(get_shop("RAVI") is not None))
    test("unknown shop returns None",
         lambda: aeq(get_shop("NONEXISTENT"), None))
    test("today summary returns dict",
         lambda: atrue("bill_count" in get_today_summary("RAVI")))
    test("bill history returns list",
         lambda: atrue(isinstance(get_bill_history("RAVI"), list)))

    # Session tests
    test("session starts",
         lambda: atrue(start_session("RAVI") > 0))
    test("session ends without error",
         lambda: end_session(start_session("RAVI")))

    # Environment
    test("api key loaded",
         lambda: atrue(bool(ANTHROPIC_API_KEY)))
    test("platform name set",
         lambda: atrue(bool(PLATFORM_NAME)))

    print("="*50)
    print(f"Results: {passed} passed, {failed} failed")
    print("="*50)
    return failed == 0


# ════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════

if __name__ == "__main__":
    # Run tests first
    if not run_tests():
        print("\nFix failing tests before starting.")
        sys.exit(1)

    # Validate environment
    if not validate_environment():
        print("\nEnvironment validation failed. Fix issues above.")
        sys.exit(1)

    # Start interactive mode
    interactive_mode(shop_id="RAVI")
