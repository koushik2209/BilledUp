"""
return_detector.py
BilledUp — Return / Credit Note Detection
------------------------------------------
Rule-based + fuzzy detection. No external API calls.
"""

import re
import logging

log = logging.getLogger("billedup.returns")

# ── Phrases that signal a return intent ──
# IMPORTANT: Only full phrases here — never bare words like "back" or "exchange"
# which cause false positives ("back cover", "exchange offer").
_RETURN_PHRASES = [
    "return", "returned", "refund", "credit note",
    "cancel order", "cancelled order",
    "give back", "take back", "sent back", "send back",
    "came back", "got back",                     # "customer came back to return"
    "exchange and return", "exchange this",
    "want to exchange", "wants to exchange",
]

# Pre-compiled regex for fast phrase matching (word-boundary on both ends)
_RETURN_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _RETURN_PHRASES) + r")\b",
    re.IGNORECASE,
)

# ── Product phrases that look like return keywords but aren't ──
# If the return keyword is part of one of these phrases, it's NOT a return.
_FALSE_POSITIVE_PHRASES = [
    "return gift", "gift pack", "back cover", "back case", "back panel",
    "exchange offer", "money back", "buy back", "cash back", "cashback",
    "back pain", "phone case",
]
_FALSE_POSITIVE_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _FALSE_POSITIVE_PHRASES) + r")\b",
    re.IGNORECASE,
)


def _keyword_match(message: str) -> bool:
    """Check if message contains return-related keywords (regex word-boundary).

    Returns False if the matched keyword is part of a known product phrase
    (e.g., "return gift", "back cover").
    """
    match = _RETURN_PATTERN.search(message)
    if not match:
        return False
    # Check if a false-positive product phrase overlaps the keyword match
    if _FALSE_POSITIVE_PATTERN.search(message):
        log.debug(f"Return keyword '{match.group()}' overridden by product phrase in '{message[:60]}'")
        return False
    return True


def _fuzzy_match(message: str) -> bool:
    """Fuzzy match common misspellings of return keywords."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return False

    msg_lower = message.lower()
    targets = ["return", "returned", "refund", "credit note"]
    # Check each word and bigram in the message
    words = msg_lower.split()
    bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)]
    tokens = words + bigrams

    for token in tokens:
        for target in targets:
            if fuzz.partial_ratio(token, target) > 80:
                return True
    return False


def _has_negative_amounts(items: list) -> bool:
    """Check if any parsed item has a negative price."""
    for item in items:
        price = item.get("price", 0)
        if isinstance(price, (int, float)) and price < 0:
            return True
    return False


def _majority_negative(items: list) -> bool:
    """Check if majority of items have negative prices."""
    if not items:
        return False
    neg_count = sum(1 for i in items if i.get("price", 0) < 0)
    return neg_count > len(items) / 2


def detect_return_intent(message: str, parsed_items: list) -> bool:
    """
    Detect if user wants to return items / generate a credit note.

    Logic (any one is enough):
    1. Message contains return keywords (exact regex)
    2. Message fuzzy-matches return keywords (for typos)
    3. Any item has a negative price

    For mixed positive/negative items:
    - Only treat as return if majority are negative

    Whitelist: product phrases like "return gift", "back cover" override
    any keyword/fuzzy match to prevent false positives.

    Returns True if this should be a credit note.
    """
    # Whitelist check: if message contains known product phrases, skip text-based detection
    has_product_phrase = bool(_FALSE_POSITIVE_PATTERN.search(message))

    # Keyword match — strongest signal
    if not has_product_phrase and _keyword_match(message):
        log.info(f"Return detected: keyword match in '{message[:60]}'")
        return True

    # Fuzzy match — catches typos like "retun", "refnd"
    if not has_product_phrase and _fuzzy_match(message):
        log.info(f"Return detected: fuzzy match in '{message[:60]}'")
        return True

    # Negative prices — explicit signal
    if _has_negative_amounts(parsed_items):
        if _majority_negative(parsed_items):
            log.info(f"Return detected: majority negative prices")
            return True
        # Mixed: some negative, some positive — not clearly a return
        log.info(f"Mixed prices detected but not majority negative — treating as normal bill")
        return False

    return False


def negate_items(items: list) -> list:
    """
    Ensure all item prices are negative for a credit note.
    Returns a new list (does not mutate input).
    """
    result = []
    for item in items:
        new_item = dict(item)
        price = abs(new_item.get("price", 0))
        new_item["price"] = -price
        result.append(new_item)
    return result
