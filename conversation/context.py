"""
conversation.context — Shop Context Loader
-------------------------------------------
Loads all runtime context needed to build the LLM system prompt:
shop profile, conversation history, pending bill, last bill,
item memory, trial status, language preference, shop type.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func

from db.session import db_session
from db.models import Shop, Bill, Registration, ConversationLog, ShopItemMaster
from services.pending import get_pending_bill

log = logging.getLogger("billedup.conversation.context")


# ════════════════════════════════════════════════
# DATACLASS
# ════════════════════════════════════════════════

@dataclass
class ShopContext:
    phone: str
    shop_name: str
    owner_name: str
    shop_type: str              # mobile / clothing / grocery / footwear / general
    state: str
    state_code: str
    gstin: str
    default_pricing: str        # inclusive / exclusive
    default_bill_type: str      # "tax_invoice" | "bill_of_supply" | "" (falls back to GSTIN check)
    language: str               # en / hi / te
    conversation_history: str   # last 8 msgs as "Shopkeeper: …\nBilledUp: …"
    pending_bill: Optional[dict]
    pending_bill_age_mins: int
    last_bill: Optional[dict]
    top_items: list             # [{name, gst_rate, count, hsn}]
    frequent_customers: list    # [{name, count}]
    bills_today: int
    total_bills: int
    is_new_user: bool           # total_bills < 5
    is_power_user: bool         # total_bills > 50
    trial_active: bool
    trial_days_left: int


# ════════════════════════════════════════════════
# MAIN LOADER
# ════════════════════════════════════════════════

def load_shop_context(phone: str) -> "ShopContext":
    """Load full runtime context for a shopkeeper phone number.

    Fetches pending bill in a separate session before opening the main
    session to avoid nested transaction issues.
    """
    # Pending bill uses its own db_session — resolve it first
    try:
        pending = get_pending_bill(phone)
    except Exception as exc:
        log.warning(f"Could not load pending bill for {phone}: {exc}")
        pending = None

    try:
        with db_session() as session:
            shop_id = _get_shop_id(phone)

            reg = session.query(Registration).filter_by(phone=phone).first()
            if not reg or not reg.active:
                return _unregistered_context(phone)

            shop = session.query(Shop).filter_by(shop_id=shop_id).first()

            shop_name       = (reg.shop_name or (shop.name if shop else None) or "Shop").strip()
            gstin           = (reg.gstin or (shop.gstin if shop else None) or "").strip()
            state           = (reg.state_name or (shop.state if shop else None) or "").strip()
            state_code      = (reg.state_code or (shop.state_code if shop else None) or "").strip()
            default_pricing   = ((shop.default_pricing   if shop else None) or "exclusive").strip()
            default_bill_type = ((shop.default_bill_type if shop else None) or "").strip()

            history            = _load_history(session, phone)
            last_bill          = _load_last_bill(session, shop_id)
            top_items          = _load_top_items(session, shop_id)
            frequent_customers = _load_frequent_customers(session, shop_id)

            today_start = datetime.utcnow().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            bills_today = session.query(Bill).filter(
                Bill.shop_id    == shop_id,
                Bill.is_return  == False,  # noqa: E712
                Bill.created_at >= today_start,
            ).count()
            total_bills = session.query(Bill).filter(
                Bill.shop_id   == shop_id,
                Bill.is_return == False,   # noqa: E712
            ).count()

            pending_dict = _serialize_pending(pending)
            pending_age  = 0
            if pending and pending.created_at:
                try:
                    delta       = datetime.utcnow() - _naive_utc(pending.created_at)
                    pending_age = max(0, int(delta.total_seconds() / 60))
                except Exception:
                    pending_age = 0

            trial_active, trial_days = _get_trial_status({
                "trial_end": reg.trial_end.isoformat() if reg.trial_end else None,
                "active":    reg.active,
            })

            language   = _detect_preferred_language(history)
            shop_type  = _detect_shop_type(top_items)
            owner_name = _extract_owner_name(shop_name)

            return ShopContext(
                phone                = phone,
                shop_name            = shop_name,
                owner_name           = owner_name,
                shop_type            = shop_type,
                state                = state,
                state_code           = state_code,
                gstin                = gstin,
                default_pricing      = default_pricing,
                default_bill_type    = default_bill_type,
                language             = language,
                conversation_history = history,
                pending_bill         = pending_dict,
                pending_bill_age_mins= pending_age,
                last_bill            = last_bill,
                top_items            = top_items,
                frequent_customers   = frequent_customers,
                bills_today          = bills_today,
                total_bills          = total_bills,
                is_new_user          = total_bills < 5,
                is_power_user        = total_bills > 50,
                trial_active         = trial_active,
                trial_days_left      = trial_days,
            )

    except Exception as exc:
        log.error(f"load_shop_context failed for {phone}: {exc}", exc_info=True)
        return _unregistered_context(phone)


# ════════════════════════════════════════════════
# INTERNAL HELPERS
# ════════════════════════════════════════════════

def _get_shop_id(phone: str) -> str:
    """Derive shop_id from phone — must match services/registration.py logic."""
    return "S" + re.sub(r"\D", "", phone)[-8:]


def _naive_utc(dt: datetime) -> datetime:
    """Return a timezone-naive UTC datetime regardless of input tzinfo."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ── History ──

def _load_history(session, phone: str, limit: int = 8) -> str:
    """Return last `limit` conversation messages as a human-readable string.

    Format per line: "Shopkeeper: <msg>" or "BilledUp: <msg>".
    Each message is truncated to 200 characters.
    """
    try:
        rows = (
            session.query(ConversationLog)
            .filter_by(phone=phone)
            .order_by(ConversationLog.created_at.desc())
            .limit(limit)
            .all()
        )
        lines = []
        for row in reversed(rows):
            speaker = "Shopkeeper" if row.direction == "IN" else "BilledUp"
            text    = (row.message or "").strip()[:200]
            lines.append(f"{speaker}: {text}")
        return "\n".join(lines)
    except Exception as exc:
        log.warning(f"_load_history failed for {phone}: {exc}")
        return ""


# ── Last bill ──

def _load_last_bill(session, shop_id: str) -> Optional[dict]:
    """Return the most recent non-return bill as a plain dict, or None."""
    try:
        bill = (
            session.query(Bill)
            .filter(
                Bill.shop_id   == shop_id,
                Bill.is_return == False,  # noqa: E712
            )
            .order_by(Bill.created_at.desc())
            .first()
        )
        if not bill:
            return None

        items: list = []
        try:
            raw = json.loads(bill.items_json or "[]")
            if isinstance(raw, list):
                items = raw
        except (json.JSONDecodeError, TypeError):
            pass

        return {
            "invoice_number": bill.invoice_number or "",
            "date":           (
                bill.created_at.strftime("%d %b %Y")
                if bill.created_at else ""
            ),
            "customer_name":  bill.customer_name or "Customer",
            "customer_phone": bill.customer_phone or "",
            "items":          items,
            "grand_total":    float(bill.grand_total or 0.0),
            "pricing_type":   bill.pricing_type or "exclusive",
        }
    except Exception as exc:
        log.warning(f"_load_last_bill failed for shop {shop_id}: {exc}")
        return None


# ── Item master ──

def _load_top_items(session, shop_id: str, limit: int = 5) -> list:
    """Return top `limit` items by use_count from ShopItemMaster."""
    try:
        rows = (
            session.query(ShopItemMaster)
            .filter_by(shop_id=shop_id)
            .order_by(ShopItemMaster.use_count.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "name":     row.item_name or "",
                "gst_rate": int(row.gst_rate or 18),
                "count":    int(row.use_count or 0),
                "hsn":      row.hsn or "",
            }
            for row in rows
        ]
    except Exception as exc:
        log.warning(f"_load_top_items failed for shop {shop_id}: {exc}")
        return []


# ── Frequent customers ──

def _load_frequent_customers(session, shop_id: str, limit: int = 3) -> list:
    """Return top `limit` customers by bill count (excluding generic names)."""
    try:
        rows = (
            session.query(
                Bill.customer_name,
                func.count(Bill.id).label("bill_count"),
            )
            .filter(
                Bill.shop_id      == shop_id,
                Bill.is_return    == False,        # noqa: E712
                Bill.customer_name.isnot(None),
                Bill.customer_name != "",
                Bill.customer_name != "Customer",
            )
            .group_by(Bill.customer_name)
            .order_by(func.count(Bill.id).desc())
            .limit(limit)
            .all()
        )
        return [{"name": r.customer_name, "count": r.bill_count} for r in rows]
    except Exception as exc:
        log.warning(f"_load_frequent_customers failed for shop {shop_id}: {exc}")
        return []


# ── Trial status ──

def _get_trial_status(reg: dict) -> tuple[bool, int]:
    """Return (is_active, days_left) from a registration dict."""
    try:
        if not reg.get("active"):
            return False, 0
        trial_end_raw = reg.get("trial_end")
        if not trial_end_raw:
            return False, 0
        if isinstance(trial_end_raw, datetime):
            trial_end = _naive_utc(trial_end_raw)
        else:
            parsed = datetime.fromisoformat(str(trial_end_raw))
            trial_end = _naive_utc(parsed)
        now = datetime.utcnow()
        if now >= trial_end:
            return False, 0
        return True, max(0, (trial_end - now).days)
    except Exception as exc:
        log.warning(f"_get_trial_status failed: {exc}")
        return False, 0


# ── Pending bill serializer ──

def _serialize_pending(pending) -> Optional[dict]:
    """Safely convert a PendingBill dataclass to a plain dict for the prompt."""
    if pending is None:
        return None
    try:
        items: list = []
        for item in getattr(pending, "items", None) or []:
            if isinstance(item, dict):
                items.append({
                    "name":  item.get("name", ""),
                    "qty":   item.get("qty", 1),
                    "price": float(item.get("price", 0.0)),
                })
            else:
                items.append({
                    "name":  getattr(item, "name", ""),
                    "qty":   getattr(item, "qty", 1),
                    "price": float(getattr(item, "price", 0.0)),
                })

        return {
            "items":          items,
            "item_count":     len(items),
            "customer_name":  getattr(pending, "customer_name", "Customer") or "Customer",
            "customer_phone": getattr(pending, "customer_phone", "") or "",
            "discount_type":  getattr(pending, "bill_discount_type", "none") or "none",
            "discount_value": float(getattr(pending, "bill_discount_value", 0.0) or 0.0),
            "pricing_type":   getattr(pending, "pricing_type", "exclusive") or "exclusive",
            "bill_type": (
                "bill_of_supply"
                if getattr(pending, "is_bill_of_supply", False)
                else "tax_invoice"
            ),
        }
    except Exception as exc:
        log.warning(f"_serialize_pending failed: {exc}")
        return None


# ── Language detection ──

_TELUGU_RE = re.compile(
    r"\b(andi|kosam|cheyyandi|rendu|moodu|avunu|ledu|kaadu|"
    r"kavali|cheyyi|cheppandi|ikkade|okati|naalugu|aidu|"
    r"aaru|yedu|enimidi|tommidi|padhi|pettu|pampinchu|"
    r"cheyandi|chestanu|chestundi|ivvandi|teesuko|"
    r"cheyagalaru|cheppagalaru|isthanu|untundi)\b",
    re.IGNORECASE,
)
_HINDI_RE = re.compile(
    r"\b(bhai|karo|chahiye|nahi|haan|aur|ek|do|teen|char|"
    r"paanch|chhe|saat|aath|nau|das|kitna|kitne|bolo|batao|"
    r"dena|lena|ruko|theek|sahi|galat|kya|yaar|iska|uska|"
    r"wala|wali|mera|tera|apna|accha|thoda|bahut)\b",
    re.IGNORECASE,
)
_HINDI_PHRASES_RE = re.compile(
    r"(ke liye|ka kya|kya hai|kya tha|kya karna|kar do|"
    r"kar dena|de do|de dena|bata do)",
    re.IGNORECASE,
)


def _detect_preferred_language(history: str) -> str:
    """Detect shopkeeper's preferred language from conversation history.

    Scans only the shopkeeper's own lines (direction=IN).
    Returns "te" (Telugu), "hi" (Hindi), or "en" (English).
    """
    if not history:
        return "en"
    try:
        shopkeeper_text = " ".join(
            line[len("Shopkeeper:"):].strip()
            for line in history.splitlines()
            if line.startswith("Shopkeeper:")
        )
        if not shopkeeper_text:
            return "en"

        te_hits = len(_TELUGU_RE.findall(shopkeeper_text))
        hi_hits = (
            len(_HINDI_RE.findall(shopkeeper_text))
            + len(_HINDI_PHRASES_RE.findall(shopkeeper_text))
        )

        if te_hits > hi_hits and te_hits >= 1:
            return "te"
        if hi_hits > te_hits and hi_hits >= 1:
            return "hi"
        if te_hits == hi_hits and te_hits >= 2:
            return "te"  # Telugu-first tiebreak for Indian South market
        return "en"
    except Exception:
        return "en"


# ── Shop type detection ──

_SHOP_KEYWORDS: dict[str, list[str]] = {
    "mobile": [
        "charger", "cable", "cover", "case", "screen guard", "earphone",
        "headphone", "powerbank", "power bank", "tempered", "adapter",
        "mobile", "phone", "battery", "usb", "type c", "lightning",
        "back cover", "flip cover", "otg", "data cable",
    ],
    "clothing": [
        "shirt", "pant", "kurti", "saree", "sari", "lehenga", "dupatta",
        "jeans", "dress", "blouse", "salwar", "kurta", "dhoti", "lungi",
        "skirt", "top", "frock", "gown", "suit", "coat", "jacket",
        "fabric", "cloth", "denim", "cotton", "silk", "wool", "linen",
    ],
    "grocery": [
        "rice", "dal", "oil", "sugar", "salt", "flour", "maida", "atta",
        "milk", "bread", "biscuit", "tea", "coffee", "masala", "spice",
        "ghee", "butter", "cheese", "egg", "soap", "shampoo", "detergent",
        "pulses", "wheat", "basmati",
    ],
    "footwear": [
        "shoe", "sandal", "slipper", "chappal", "boot", "heel",
        "sneaker", "loafer", "flat", "wedge", "footwear", "hawai",
    ],
}


def _detect_shop_type(top_items: list) -> str:
    """Infer shop category from item names in the item master."""
    if not top_items:
        return "general"
    try:
        all_names = " ".join(
            (item.get("name") or "").lower() for item in top_items
        )
        scores: dict[str, int] = {cat: 0 for cat in _SHOP_KEYWORDS}
        for category, keywords in _SHOP_KEYWORDS.items():
            for kw in keywords:
                if kw in all_names:
                    scores[category] += 1
        best_cat = max(scores, key=lambda c: scores[c])
        return best_cat if scores[best_cat] > 0 else "general"
    except Exception:
        return "general"


# ── Owner name extraction ──

_SUFFIX_RE = re.compile(
    r"\s+(mobile|accessories|accessory|garments|garment|stores?|"
    r"shops?|traders?|enterprises?|mart|market|gallery|boutique|"
    r"collections?|cent(?:er|re)|point|house|hub|world|zone|"
    r"palace|electronics|electric(?:al)?|hardware|general|"
    r"wholesale|retail|agency|agencies|brothers|sisters|"
    r"sons|and\s+sons|co\.?|pvt\.?\s*ltd\.?|llp|industries)\b.*$",
    re.IGNORECASE,
)


def _extract_owner_name(shop_name: str) -> str:
    """Extract the owner's first name from a shop name.

    Examples:
        "Ravi Mobile Accessories" → "Ravi"
        "Sri Balaji Garments"     → "Sri"   (honourific kept as-is)
        "Trendy Collections"      → "Trendy"
    """
    if not shop_name or not shop_name.strip():
        return "there"
    try:
        cleaned = _SUFFIX_RE.sub("", shop_name.strip()).strip()
        parts = cleaned.split()
        if not parts:
            parts = shop_name.strip().split()
        return parts[0].title() if parts else "there"
    except Exception:
        words = shop_name.strip().split()
        return words[0].title() if words else "there"


# ── Fallback for unregistered / errored users ──

def _unregistered_context(phone: str) -> ShopContext:
    """Return a minimal ShopContext for users who are not yet registered."""
    return ShopContext(
        phone                = phone,
        shop_name            = "",
        owner_name           = "there",
        shop_type            = "general",
        state                = "",
        state_code           = "",
        gstin                = "",
        default_pricing      = "exclusive",
        default_bill_type    = "",
        language             = "en",
        conversation_history = "",
        pending_bill         = None,
        pending_bill_age_mins= 0,
        last_bill            = None,
        top_items            = [],
        frequent_customers   = [],
        bills_today          = 0,
        total_bills          = 0,
        is_new_user          = True,
        is_power_user        = False,
        trial_active         = False,
        trial_days_left      = 0,
    )
