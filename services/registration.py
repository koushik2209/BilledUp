"""
services.registration — Registration State Machine & Onboarding
-----------------------------------------------------------------
Manages shopkeeper registration, trial activation, GSTIN validation,
and Indian state resolution.
"""

import re
import logging
from datetime import datetime, timedelta

from database import (
    db_session, Registration, ConversationLog, Shop,
    generate_api_key,
    init_database as init_db,
)
from bill_generator import PLACEHOLDER_GSTIN, GSTIN_REGEX

log = logging.getLogger("billedup.registration")


# ════════════════════════════════════════════════
# REGISTRATION CRUD
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
            "state": row.state,
            "state_name": row.state_name or "",
            "state_code": row.state_code or "",
            "trial_start": row.trial_start.isoformat() if row.trial_start else None,
            "trial_end": row.trial_end.isoformat() if row.trial_end else None,
            "active": row.active, "bills_count": row.bills_count,
        }


ALLOWED_REG_FIELDS = {
    "shop_name", "address", "gstin", "invoice_type", "state",
    "state_name", "state_code",
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


# ════════════════════════════════════════════════
# TRIAL MANAGEMENT
# ════════════════════════════════════════════════

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


def activate_trial(phone: str, shop_name: str, address: str, gstin: str = "",
                    state_name: str = "Telangana", state_code: str = "36"):
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
                state      = state_name,
                state_code = state_code,
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
        state_name   = state_name,
        state_code   = state_code,
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
# GSTIN VALIDATION
# ════════════════════════════════════════════════

def is_valid_gstin(gstin: str) -> bool:
    return bool(GSTIN_REGEX.match(gstin.upper().strip()))


def update_shop_default_bill_type(phone: str, bill_type: str) -> None:
    """Persist default_bill_type preference to the Shop table.

    Called when the owner says 'always bill of supply' / 'always tax invoice'.
    The value is read by load_shop_context and stored in ctx.default_bill_type
    so _is_bill_of_supply() can respect it without a GSTIN check.
    """
    shop_id = "S" + re.sub(r"\D", "", phone)[-8:]
    with db_session() as session:
        shop = session.query(Shop).filter_by(shop_id=shop_id).first()
        if shop:
            shop.default_bill_type = bill_type
    log.info(f"Default bill type updated for shop {shop_id} → {bill_type}")


def update_shop_gstin(phone: str, gstin: str) -> None:
    """Persist a new GSTIN to both Registration and Shop tables.

    Called when an existing (Bill of Supply) shop registers their GSTIN
    mid-session so the next load_shop_context() sees the real GSTIN
    instead of the placeholder.

    Note: uses two separate sessions — not atomic. If the second write
    (Shop table) fails, the first (Registration table) may have already
    committed.
    """
    shop_id = "S" + re.sub(r"\D", "", phone)[-8:]
    gstin = gstin.upper().strip()

    # Registration table: store real GSTIN and flip invoice_type
    upsert_registration(phone, gstin=gstin, invoice_type="TAX_INVOICE")

    # Shop table: replace placeholder with real GSTIN
    with db_session() as session:
        shop = session.query(Shop).filter_by(shop_id=shop_id).first()
        if shop:
            shop.gstin = gstin

    log.info(f"GSTIN updated for shop {shop_id} → {gstin}")


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
