"""
services.pending — PendingBill Storage & Retrieval
-----------------------------------------------------
Manages bill previews awaiting user confirmation.
Stores serialized PendingBill in the DB with expiry.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from database import db_session, PendingBillRecord

log = logging.getLogger("billedup.pending")

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
    is_inclusive: bool = False
    customer_phone: str = ""


def _serialize_pending(bill: PendingBill) -> str:
    """Serialize PendingBill to JSON string for DB storage."""
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
        "is_inclusive": bill.is_inclusive,
        "customer_phone": bill.customer_phone,
    }
    return json.dumps(data)


def _deserialize_pending(json_str: str) -> PendingBill:
    """Deserialize JSON string back to PendingBill."""
    data = json.loads(json_str)
    data["created_at"] = datetime.fromisoformat(data["created_at"])
    # Backwards compat: old pending bills in DB won't have these fields
    data.setdefault("is_bill_of_supply", False)
    data.setdefault("is_inclusive", False)
    data.setdefault("customer_phone", "")
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
