"""
db.models — SQLAlchemy ORM Models
-----------------------------------
All table definitions. No business logic.
"""

from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Float, Text,
    Boolean, DateTime, Index, LargeBinary,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


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
    default_pricing = Column(String(10), default="exclusive")
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
    subtotal_before_discount = Column(Float, default=0.0)
    bill_discount_type  = Column(String(10), default="none")
    bill_discount_value = Column(Float, default=0.0)
    taxable_amount      = Column(Float, default=0.0)
    pricing_type        = Column(String(10), default="exclusive")
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


class ProcessedMessage(Base):
    """Dedup table: stores WhatsApp message IDs to prevent duplicate processing
    when Meta retries webhook delivery."""
    __tablename__ = "processed_messages"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String(100), unique=True, nullable=False, index=True)
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
