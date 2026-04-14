"""
ai.sanitizer — Input Sanitization & Response Validation
---------------------------------------------------------
Cleans user messages and validates Claude's parsed output.
"""

import re
import logging

log = logging.getLogger("billedup.parser")

# ── Constants ──
MAX_MESSAGE_LENGTH  = 1000
MAX_ITEMS_PER_BILL  = 50
MAX_PRICE           = 10_000_000
MAX_QTY             = 99999
MIN_PRICE           = 0.01

# Regex pattern to detect weight/volume unit descriptors
# These get glued to the item name, not treated as quantity
_UNIT_PATTERN = re.compile(
    r'\b\d+(?:\.\d+)?\s*(?:gm|gms|g|kg|kgs|ml|l|ltr|ltrs|litre|litres|gram|grams)\b',
    re.IGNORECASE
)

# Indian mobile number: optional +91, first digit 6-9, 10 digits total,
# tolerates a single space or hyphen after the +91 and/or in the middle.
_PHONE_PATTERN = re.compile(
    r"(?:\+91[\-\s]?)?[6-9]\d{4}[\-\s]?\d{5}"
)


def extract_customer_phone(text: str) -> str | None:
    """Extract the first Indian mobile number from free-form text.

    Returns a normalized 10-digit string, or None if nothing matches.
    Strips +91, spaces, and dashes. Only digits come out.
    """
    if not text:
        return None
    m = _PHONE_PATTERN.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return None


def strip_phone_from_name(name: str) -> str:
    """Remove any phone-like digit runs that leaked into a customer name."""
    if not name:
        return name
    cleaned = _PHONE_PATTERN.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    return cleaned


# ── Input sanitization ──
def sanitize_message(message: str) -> tuple[str, list]:
    warnings = []
    if not message:
        return "", ["Empty message"]
    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[:MAX_MESSAGE_LENGTH]
        warnings.append(f"Message truncated to {MAX_MESSAGE_LENGTH} characters")
    message = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", message)
    message = re.sub(r"\s+", " ", message).strip()
    suspicious = [
        "system:", "assistant:", "ignore previous",
        "forget instructions", "new instructions"
    ]
    msg_lower = message.lower()
    for pattern in suspicious:
        if pattern in msg_lower:
            warnings.append(f"Suspicious pattern detected: '{pattern}'")
            message = re.sub(re.escape(pattern), "", message, flags=re.IGNORECASE)
    if len(message.strip()) < 3:
        warnings.append("Message too short to parse")
    return message.strip(), warnings


# ── Response validation ──
_VALID_PRICING       = ("exclusive", "inclusive")
_VALID_BILL_DISCOUNT = ("none", "percent", "flat", "override")
_VALID_ITEM_DISCOUNT = ("none", "percent", "flat")


def _coerce_float(raw, default=0.0) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val >= 0 else default


def _coerce_choice(raw, choices: tuple, default: str) -> str:
    if raw is None:
        return default
    val = str(raw).strip().lower()
    return val if val in choices else default


def validate_parsed_response(result: dict) -> tuple[dict, list]:
    issues = []
    if "items" not in result:
        result["items"] = []
        issues.append("No items field in response")
    if not isinstance(result.get("items"), list):
        result["items"] = []
        issues.append("Items field is not a list")
    # Bill-level discount & pricing fields
    result["pricing_type"] = _coerce_choice(
        result.get("pricing_type"), _VALID_PRICING, "exclusive"
    )
    result["bill_discount_type"] = _coerce_choice(
        result.get("bill_discount_type"), _VALID_BILL_DISCOUNT, "none"
    )
    result["bill_discount_value"] = _coerce_float(
        result.get("bill_discount_value"), 0.0
    )
    if result["bill_discount_type"] == "none":
        result["bill_discount_value"] = 0.0
    customer = str(result.get("customer_name", "")).strip()
    if not customer or customer.lower() in ("null", "none", "unknown", ""):
        customer = "Customer"
    customer = re.sub(r"[^\w\s\.\-]", "", customer).strip()
    result["customer_name"] = customer or "Customer"
    confidence = float(result.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))
    result["confidence"] = confidence
    valid_items = []
    for i, item in enumerate(result.get("items", [])):
        if not isinstance(item, dict):
            issues.append(f"Item {i+1} is not a dict — skipped")
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            issues.append(f"Item {i+1} has no name — skipped")
            continue
        name = re.sub(r"[^\w\s\-\.]", "", name).strip()
        if not name:
            issues.append(f"Item {i+1} name invalid after cleaning — skipped")
            continue
        try:
            qty = float(item.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1.0
            issues.append(f"Item '{name}' qty invalid — defaulting to 1")
        qty = max(0.001, min(float(MAX_QTY), qty))
        # Safety net: if qty looks like a weight amount for weight-sold items
        # and Claude misread the instruction, fix it here.
        _weight_items = {"gold", "silver", "platinum", "oil", "milk", "rice",
                         "wheat", "sugar", "dal", "ghee", "butter", "flour"}
        item_base = name.lower().split()[0] if name else ""
        if item_base in _weight_items and qty > 1 and qty >= 100:
            # Nobody buys 100+ units of gold/oil/milk — this is a weight descriptor
            unit_guess = f"{int(qty)}gm"
            name = f"{name} {unit_guess}"
            qty = 1.0
        try:
            price = float(item.get("price", 0))
        except (TypeError, ValueError):
            issues.append(f"Item '{name}' price invalid — skipped")
            continue
        if price < MIN_PRICE:
            issues.append(f"Item '{name}' price is {price} — skipped")
            continue
        if price > MAX_PRICE:
            issues.append(f"Item '{name}' price Rs.{price} exceeds limit — skipped")
            continue
        if 9000000000 <= price <= 9999999999:
            issues.append(f"Item '{name}' price looks like phone number — skipped")
            continue
        item_disc_type = _coerce_choice(
            item.get("item_discount_type"), _VALID_ITEM_DISCOUNT, "none"
        )
        item_disc_val = _coerce_float(item.get("item_discount_value"), 0.0)
        if item_disc_type == "none":
            item_disc_val = 0.0
        valid_items.append({
            "name":  name,
            "qty":   round(qty, 3),
            "price": round(price, 2),
            "item_discount_type":  item_disc_type,
            "item_discount_value": round(item_disc_val, 2),
        })
    if len(valid_items) > MAX_ITEMS_PER_BILL:
        valid_items = valid_items[:MAX_ITEMS_PER_BILL]
        issues.append(f"Truncated to {MAX_ITEMS_PER_BILL} items")
    result["items"] = valid_items
    return result, issues


# ── Unit-quantity post-processing ──
def _fix_unit_quantities(items: list, original_text: str) -> list:
    """
    Post-process parsed items to detect cases where a weight/unit
    descriptor was split across name and qty.

    If an item has qty != 1 and the original text contains
    "{qty}{unit}" adjacent to the item name, it means the unit
    was a descriptor not a count — fix it.

    Example: name="gold", qty=500, price=100000
    Check if "500gm" or "500g" etc appears in original text near "gold"
    → fix to name="gold 500gm", qty=1, price=100000
    """
    fixed = []
    for item in items:
        name  = item["name"]
        qty   = item["qty"]
        price = item["price"]

        if qty != 1:
            # Search original text for "{qty}{unit}" near this item
            qty_str = str(int(qty)) if qty == int(qty) else str(qty)
            unit_re = re.compile(
                rf'\b{re.escape(qty_str)}'
                rf'\s*(?:gm|gms|g|kg|kgs|ml|l|ltr|ltrs|litre|litres|gram|grams)\b',
                re.IGNORECASE
            )
            match = unit_re.search(original_text)
            if match:
                # This qty was actually a unit — glue to name
                unit_str = match.group(0).strip()
                item = {**item, "name": f"{name} {unit_str}", "qty": 1.0}

        fixed.append(item)
    return fixed
