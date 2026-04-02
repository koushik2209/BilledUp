"""
database.py
BilledUp - SQLAlchemy Database Layer
-------------------------------------
Supports both PostgreSQL (production) and SQLite (testing/local).
All models, session management, and CRUD operations in one place.
"""

import os
import json
import secrets
import logging
import threading
from datetime import datetime
from contextlib import contextmanager

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Text,
    Boolean, DateTime, Index, LargeBinary, func,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from config import DATABASE_URL

log = logging.getLogger("billedup.db")

# ── Engine & Session Factory ──
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=False,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

Base = declarative_base()


# ════════════════════════════════════════════════
# MODELS
# ════════════════════════════════════════════════

class Shop(Base):
    __tablename__ = "shops"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    shop_id    = Column(String(50), unique=True, nullable=False, index=True)
    name       = Column(String(200), nullable=False)
    address    = Column(Text, nullable=False)
    gstin      = Column(String(20), nullable=False)
    phone      = Column(String(20), nullable=False)
    upi        = Column(String(100), default="")
    state      = Column(String(50), default="")
    state_code = Column(String(5), default="")
    api_key    = Column(String(64), unique=True, nullable=True, index=True)
    active     = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Bill(Base):
    __tablename__ = "bills"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    invoice_number  = Column(String(50), unique=True, nullable=False)
    shop_id         = Column(String(50), nullable=False, index=True)
    customer_name   = Column(String(200), nullable=False)
    customer_phone  = Column(String(20), default="")
    items_json      = Column(Text, nullable=False)
    subtotal        = Column(Float, nullable=False)
    total_cgst      = Column(Float, default=0.0)
    total_sgst      = Column(Float, default=0.0)
    total_igst      = Column(Float, default=0.0)
    total_gst       = Column(Float, nullable=False)
    grand_total     = Column(Float, nullable=False)
    is_igst         = Column(Boolean, default=False)
    is_return       = Column(Boolean, default=False)
    pdf_path        = Column(Text, nullable=False)
    pdf_data        = Column(LargeBinary, nullable=True)
    raw_message     = Column(Text, default="")
    confidence      = Column(Float, default=1.0)
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)


class SessionRecord(Base):
    __tablename__ = "sessions"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    shop_id     = Column(String(50), nullable=False)
    started_at  = Column(DateTime, default=datetime.utcnow)
    ended_at    = Column(DateTime, nullable=True)
    bills_count = Column(Integer, default=0)
    total_value = Column(Float, default=0.0)
    notes       = Column(Text, default="")


class InvoiceSequence(Base):
    __tablename__ = "invoice_sequences"

    key      = Column(String(100), primary_key=True)
    sequence = Column(Integer, default=0)


class Registration(Base):
    __tablename__ = "registrations"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    phone        = Column(String(30), unique=True, nullable=False, index=True)
    shop_name    = Column(String(200), default="")
    address      = Column(Text, default="")
    gstin        = Column(String(20), default="")
    invoice_type = Column(String(20), default="TAX_INVOICE")
    state        = Column(String(20), default="NEW")
    state_name   = Column(String(50), default="")
    state_code   = Column(String(5), default="")
    trial_start  = Column(DateTime, nullable=True)
    trial_end    = Column(DateTime, nullable=True)
    active       = Column(Boolean, default=False)
    bills_count  = Column(Integer, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ConversationLog(Base):
    __tablename__ = "conversation_log"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    phone      = Column(String(30), nullable=False, index=True)
    direction  = Column(String(5), nullable=False)
    message    = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class PendingBillRecord(Base):
    __tablename__ = "pending_bills"

    phone      = Column(String(30), primary_key=True)
    data_json  = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


class ReportPDF(Base):
    __tablename__ = "report_pdfs"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    filename   = Column(String(200), unique=True, nullable=False, index=True)
    shop_id    = Column(String(50), nullable=False, index=True)
    pdf_data   = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ShopItemMaster(Base):
    __tablename__ = "shop_item_master"
    __table_args__ = (
        Index("ix_shop_item_master_shop_item", "shop_id", "item_name", unique=True),
    )

    id         = Column(Integer, primary_key=True, autoincrement=True)
    shop_id    = Column(String(50), nullable=False, index=True)
    item_name  = Column(String(200), nullable=False)
    hsn        = Column(String(20), nullable=False)
    gst_rate   = Column(Integer, nullable=False)
    confirmed  = Column(Boolean, default=False)
    use_count  = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    log.info(f"Database initialised: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else DATABASE_URL}")


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


# ════════════════════════════════════════════════
# API KEY MANAGEMENT
# ════════════════════════════════════════════════

def generate_api_key() -> str:
    """Generate a unique 48-char API key prefixed with 'bu_'."""
    return "bu_" + secrets.token_hex(24)


def assign_api_key(shop_id: str) -> str:
    """Generate and assign a new API key to a shop. Returns the key."""
    key = generate_api_key()
    with db_session() as session:
        shop = session.query(Shop).filter_by(shop_id=shop_id.upper()).first()
        if not shop:
            raise ValueError(f"Shop '{shop_id}' not found")
        shop.api_key = key
    log.info(f"API key assigned to shop {shop_id}")
    return key


def validate_api_key(api_key: str) -> Shop | None:
    """Validate an API key. Returns the Shop if valid, None otherwise."""
    if not api_key or not api_key.startswith("bu_"):
        return None
    with db_session() as session:
        shop = session.query(Shop).filter_by(api_key=api_key, active=True).first()
        if shop:
            # Detach from session so it can be used outside
            session.expunge(shop)
        return shop


# ════════════════════════════════════════════════
# SHOP ITEM MASTER
# ════════════════════════════════════════════════

def get_item_master(shop_id: str, item_name: str) -> dict | None:
    """Get a shop's saved item by name. Returns dict or None."""
    name = item_name.lower().strip()
    with db_session() as session:
        row = session.query(ShopItemMaster).filter_by(
            shop_id=shop_id, item_name=name,
        ).first()
        if not row:
            return None
        return {
            "item_name": row.item_name,
            "hsn": row.hsn,
            "gst_rate": row.gst_rate,
            "confirmed": row.confirmed,
            "use_count": row.use_count,
        }


def save_item_master(shop_id: str, item_name: str, hsn: str, gst_rate: int,
                     confirmed: bool = False):
    """Upsert an item in the shop's item master. Increments use_count."""
    name = item_name.lower().strip()
    with db_session() as session:
        row = session.query(ShopItemMaster).filter_by(
            shop_id=shop_id, item_name=name,
        ).first()
        if row:
            row.hsn = hsn
            row.gst_rate = gst_rate
            if confirmed:
                row.confirmed = True
            row.use_count += 1
        else:
            session.add(ShopItemMaster(
                shop_id=shop_id, item_name=name,
                hsn=hsn, gst_rate=gst_rate,
                confirmed=confirmed, use_count=1,
            ))


def get_top_items(shop_id: str, limit: int = 20) -> list[dict]:
    """Get top items by use_count for a shop."""
    with db_session() as session:
        rows = session.query(ShopItemMaster).filter_by(
            shop_id=shop_id,
        ).order_by(ShopItemMaster.use_count.desc()).limit(limit).all()
        return [
            {
                "item_name": r.item_name,
                "hsn": r.hsn,
                "gst_rate": r.gst_rate,
                "confirmed": r.confirmed,
                "use_count": r.use_count,
            }
            for r in rows
        ]


def update_item_gst(shop_id: str, item_name: str, gst_rate: int) -> bool:
    """Update GST rate for an existing item and mark confirmed. Returns True if found."""
    name = item_name.lower().strip()
    with db_session() as session:
        row = session.query(ShopItemMaster).filter_by(
            shop_id=shop_id, item_name=name,
        ).first()
        if not row:
            return False
        row.gst_rate = gst_rate
        row.confirmed = True
        return True
