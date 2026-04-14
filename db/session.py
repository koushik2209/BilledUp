"""
db.session — Engine, Session Factory, Init & Schema Validation
---------------------------------------------------------------
"""

import os
import logging
import threading
from datetime import datetime
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL
from db.models import Base, InvoiceSequence

log = logging.getLogger("billedup.db")

# ── Engine & Session Factory ──
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=False,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ════════════════════════════════════════════════
# SESSION HELPER
# ════════════════════════════════════════════════

@contextmanager
def db_session():
    """Context manager for safe database transactions."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        log.error(f"DB transaction failed: {e}")
        raise
    finally:
        session.close()


# ════════════════════════════════════════════════
# INIT
# ════════════════════════════════════════════════

def init_database():
    """Create all tables."""
    Base.metadata.create_all(engine)
    log.info(f"[DB] Database initialised: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else DATABASE_URL}")


# ════════════════════════════════════════════════
# SCHEMA VALIDATION
# ════════════════════════════════════════════════

_REQUIRED_SCHEMA = {
    "processed_messages": ["message_id"],
    "report_pdfs":        ["filename", "shop_id", "pdf_data"],
    "shop_item_master":   ["shop_id", "item_name", "hsn", "gst_rate", "confirmed", "use_count"],
    "pending_bills":      ["phone", "data_json", "expires_at"],
    "bills":              ["pdf_data", "is_return", "is_igst",
                           "bill_discount_type", "bill_discount_value",
                           "subtotal_before_discount", "taxable_amount",
                           "pricing_type"],
    "registrations":      ["invoice_type", "state_name", "state_code"],
    "conversation_log":   ["phone", "direction", "message"],
    "shops":              ["api_key", "state", "state_code", "upi", "default_pricing"],
}


def validate_schema() -> list[str]:
    """Check that all required tables and columns exist in the live database.

    Returns a list of human-readable problem strings.
    Empty list = schema is fine.
    """
    from sqlalchemy import inspect as sa_inspect

    problems: list[str] = []
    try:
        inspector = sa_inspect(engine)
        existing_tables = set(inspector.get_table_names())

        for table, required_cols in _REQUIRED_SCHEMA.items():
            if table not in existing_tables:
                problems.append(f"missing table: {table}")
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table)}
            for col in required_cols:
                if col not in existing_cols:
                    problems.append(f"missing column: {table}.{col}")
    except Exception as e:
        problems.append(f"inspection error: {e}")

    if problems:
        for p in problems:
            log.warning(f"[DB] Schema issue: {p}")
    else:
        log.info("[DB] Schema validation passed")

    return problems


def reset_database():
    """Drop ALL tables and recreate from current models.

    For SQLite: also deletes the DB file for a clean slate.
    ONLY safe in dev — caller must gate on DEV_MODE.
    """
    log.warning("[DB] Resetting database — dropping all tables")
    Base.metadata.drop_all(engine)
    engine.dispose()

    if DATABASE_URL.startswith("sqlite"):
        db_path = DATABASE_URL.replace("sqlite:///", "")
        if db_path and os.path.exists(db_path):
            try:
                os.remove(db_path)
                log.info(f"[DB] Deleted SQLite file: {db_path}")
            except OSError as e:
                log.warning(f"[DB] Could not delete SQLite file: {e}")

    Base.metadata.create_all(engine)
    log.info("[DB] Database recreated from models")


def ensure_schema(dev_mode: bool = False):
    """Startup schema check. Call after init_database().

    - If schema is valid → no-op.
    - If dev_mode=True  → auto-reset and recreate.
    - If dev_mode=False → log warnings only (production safe).
    """
    problems = validate_schema()
    if not problems:
        return

    log.warning(f"[DB] {len(problems)} schema issue(s) detected")

    if dev_mode:
        log.warning("[DB] DEV_MODE=True — auto-resetting database")
        reset_database()
        post = validate_schema()
        if post:
            log.error(f"[DB] Schema STILL invalid after reset: {post}")
        else:
            log.info("[DB] Schema valid after reset")
    else:
        log.warning(
            "[DB] DEV_MODE is off — NOT auto-resetting. "
            "Fix manually or set DEV_MODE=True to auto-reset on next startup."
        )


# ════════════════════════════════════════════════
# INVOICE SEQUENCE (thread-safe)
# ════════════════════════════════════════════════

_invoice_lock = threading.Lock()


def generate_next_sequence(shop_key: str, year: str) -> int:
    """Atomically increment and return the next invoice sequence number."""
    key = f"{shop_key}_{year}"

    with _invoice_lock:
        with db_session() as session:
            row = session.query(InvoiceSequence).filter_by(key=key).with_for_update().first()
            if row:
                row.sequence += 1
                sequence = row.sequence
            else:
                sequence = 1
                session.add(InvoiceSequence(key=key, sequence=sequence))
            session.flush()
            return sequence
