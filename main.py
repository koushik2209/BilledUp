"""
main.py
BillEasy - Production Grade Main Entry Point
--------------------------------------------
Features:
- SQLite database for multi-shop support
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
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from config import (
    ANTHROPIC_API_KEY, PLATFORM_NAME,
    PLATFORM_TAGLINE, DATABASE_URL, DEBUG,
    get_anthropic_client,
)
from claude_parser import parse_message, format_result
from bill_generator import (
    ShopProfile, CustomerInfo, BillItem,
    generate_invoice_number, generate_pdf_bill
)
 
# ── Logging ──
log_level = logging.DEBUG if DEBUG else logging.WARNING
logging.basicConfig(
    level  = log_level,
    format = "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt= "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("billeasy.main")
 
 
 
# ════════════════════════════════════════════════
# DATABASE
# ════════════════════════════════════════════════
 
DB_PATH = DATABASE_URL.replace("sqlite:///", "")
 
def get_db() -> sqlite3.Connection:
    """Get database connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
 
@contextmanager
def db_session():
    """Context manager for safe database transactions."""
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"DB transaction failed: {e}")
        raise
    finally:
        conn.close()
 
def init_database():
    """Create all tables if they do not exist."""
    with db_session() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS shops (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id     TEXT UNIQUE NOT NULL,
                name        TEXT NOT NULL,
                address     TEXT NOT NULL,
                gstin       TEXT NOT NULL,
                phone       TEXT NOT NULL,
                upi         TEXT DEFAULT '',
                state       TEXT DEFAULT 'Telangana',
                state_code  TEXT DEFAULT '36',
                active      INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );
 
            CREATE TABLE IF NOT EXISTS bills (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number  TEXT UNIQUE NOT NULL,
                shop_id         TEXT NOT NULL,
                customer_name   TEXT NOT NULL,
                customer_phone  TEXT DEFAULT '',
                items_json      TEXT NOT NULL,
                subtotal        REAL NOT NULL,
                total_gst       REAL NOT NULL,
                grand_total     REAL NOT NULL,
                pdf_path        TEXT NOT NULL,
                raw_message     TEXT DEFAULT '',
                confidence      REAL DEFAULT 1.0,
                created_at      TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (shop_id) REFERENCES shops(shop_id)
            );
 
            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id     TEXT NOT NULL,
                started_at  TEXT DEFAULT (datetime('now')),
                ended_at    TEXT,
                bills_count INTEGER DEFAULT 0,
                total_value REAL DEFAULT 0.0,
                notes       TEXT DEFAULT ''
            );
 
            CREATE INDEX IF NOT EXISTS idx_bills_shop
                ON bills(shop_id);
            CREATE INDEX IF NOT EXISTS idx_bills_date
                ON bills(created_at);
            CREATE INDEX IF NOT EXISTS idx_bills_customer
                ON bills(customer_name);
        """)
    log.info(f"Database initialised: {DB_PATH}")
 
 
def seed_demo_shop():
    """Insert demo shop if no shops exist."""
    with db_session() as conn:
        count = conn.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
        if count == 0:
            conn.execute("""
                INSERT INTO shops
                    (shop_id, name, address, gstin, phone, upi, state, state_code)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "RAVI",
                "Ravi Mobile Accessories",
                "Shop No. 14, Koti Market, Hyderabad - 500095",
                "36AABCU9603R1ZX",
                "+91 98765 43210",
                "ravi@ybl",
                "Telangana",
                "36",
            ))
            log.info("Demo shop seeded: RAVI")
 
 
def get_shop(shop_id: str) -> ShopProfile | None:
    """Load shop from database by shop_id."""
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM shops WHERE shop_id=? AND active=1",
            (shop_id.upper(),)
        ).fetchone()
        if not row:
            return None
        return ShopProfile(
            shop_id    = row["shop_id"],
            name       = row["name"],
            address    = row["address"],
            gstin      = row["gstin"],
            phone      = row["phone"],
            upi        = row["upi"] or "",
            state      = row["state"],
            state_code = row["state_code"],
        )
 
 
def save_bill(
    shop_id:        str,
    invoice_number: str,
    customer_name:  str,
    customer_phone: str,
    items:          list,
    bill_result,
    pdf_path:       str,
    raw_message:    str = "",
    confidence:     float = 1.0,
):
    """Persist bill to database."""
    items_data = [
        {
            "name":     i.name,
            "qty":      i.qty,
            "price":    i.price,
            "hsn":      i.hsn,
            "gst_rate": i.gst_rate,
            "total":    i.total,
        }
        for i in items
    ]
    with db_session() as conn:
        conn.execute("""
            INSERT INTO bills
                (invoice_number, shop_id, customer_name, customer_phone,
                 items_json, subtotal, total_gst, grand_total,
                 pdf_path, raw_message, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            invoice_number,
            shop_id,
            customer_name,
            customer_phone,
            json.dumps(items_data),
            bill_result.subtotal,
            bill_result.total_gst,
            bill_result.grand_total,
            pdf_path,
            raw_message,
            confidence,
        ))
    log.info(f"Bill saved to DB: {invoice_number}")
 
 
def get_bill_history(shop_id: str, limit: int = 10) -> list:
    """Get recent bills for a shop."""
    with db_session() as conn:
        rows = conn.execute("""
            SELECT invoice_number, customer_name,
                   grand_total, created_at, pdf_path
            FROM bills
            WHERE shop_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (shop_id.upper(), limit)).fetchall()
        return [dict(r) for r in rows]
 
 
def get_today_summary(shop_id: str) -> dict:
    """Get today's billing summary for a shop."""
    today = datetime.now().strftime("%Y-%m-%d")
    with db_session() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)        as bill_count,
                SUM(grand_total) as total_value,
                SUM(subtotal)    as subtotal,
                SUM(total_gst)   as total_gst
            FROM bills
            WHERE shop_id = ?
            AND DATE(created_at) = ?
        """, (shop_id.upper(), today)).fetchone()
        return {
            "date":        today,
            "bill_count":  row["bill_count"]  or 0,
            "total_value": row["total_value"] or 0.0,
            "subtotal":    row["subtotal"]    or 0.0,
            "total_gst":   row["total_gst"]   or 0.0,
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
 
    # Check bills folder writable
    from config import BILLS_FOLDER
    try:
        os.makedirs(BILLS_FOLDER, exist_ok=True)
        test_file = os.path.join(BILLS_FOLDER, ".write_test")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        print("  Bills folder  OK")
    except Exception as e:
        issues.append(f"Bills folder not writable: {e}")
 
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
    session_id: int = None,
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
        pdf_path, bill_result = generate_pdf_bill(
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
            pdf_path       = pdf_path,
            raw_message    = message,
            confidence     = parsed.get("confidence", 1.0),
        )
    except Exception as e:
        # DB save failure is non-fatal — bill was already generated
        log.error(f"DB save failed (non-fatal): {e}")
 
    # ── Update session ──
    if session_id:
        try:
            with db_session() as conn:
                conn.execute("""
                    UPDATE sessions
                    SET bills_count = bills_count + 1,
                        total_value = total_value + ?
                    WHERE id = ?
                """, (bill_result.grand_total, session_id))
        except Exception as e:
            log.warning(f"Session update failed: {e}")
 
    return {
        "success":        True,
        "invoice_number": invoice_number,
        "customer":       parsed["customer_name"],
        "items_count":    len(items),
        "grand_total":    bill_result.grand_total,
        "pdf_path":       pdf_path,
        "confidence":     parsed.get("confidence", 1.0),
        "warnings":       parsed.get("warnings", []),
    }
 
 
# ════════════════════════════════════════════════
# SESSION MANAGEMENT
# ════════════════════════════════════════════════
 
def start_session(shop_id: str) -> int:
    """Start a new billing session. Returns session_id."""
    with db_session() as conn:
        cursor = conn.execute(
            "INSERT INTO sessions (shop_id) VALUES (?)",
            (shop_id.upper(),)
        )
        session_id = cursor.lastrowid
    log.info(f"Session {session_id} started for shop {shop_id}")
    return session_id
 
 
def end_session(session_id: int):
    """Mark session as ended."""
    with db_session() as conn:
        conn.execute("""
            UPDATE sessions
            SET ended_at = datetime('now')
            WHERE id = ?
        """, (session_id,))
    log.info(f"Session {session_id} ended")
 
 
def print_session_summary(shop_id: str, session_id: int):
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
            dt = bill["created_at"][:16]
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
        print("  quit/exit   — Exit BillEasy")
        print("\nOr type a bill message like:")
        print("  phone case 299 charger 499 customer Suresh")
        print("  oka charger 199 ki Ravi ki bill cheyyi")
        return True
 
    if cmd == "history":
        bills = get_bill_history(shop.shop_id)
        if not bills:
            print("  No bills yet today")
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
 
def _handle_signal(sig, frame):
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
                print(f"\n  Open bill: {result['pdf_path']}")
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
    if _session_id:
        end_session(_session_id)
        print_session_summary(_shop.shop_id, _session_id)
 
    print("\nGoodbye!\n")
 
 
# ════════════════════════════════════════════════
# UNIT TESTS
# ════════════════════════════════════════════════
 
def run_tests():
    print("\n" + "="*50)
    print("BillEasy Main — Unit Tests")
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
 