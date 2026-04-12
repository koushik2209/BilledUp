"""
ai.parser — Claude API Message Parser
-----------------------------------------
Sends shopkeeper messages to Claude for item extraction.
Falls back to regex parser on failure.
"""

import re
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from collections import deque
from anthropic.types import TextBlock

from config import get_anthropic_client
from ai.sanitizer import (
    sanitize_message, validate_parsed_response,
    extract_customer_phone, strip_phone_from_name,
)
from ai.regex_parser import _regex_parse_message

log = logging.getLogger("billedup.parser")

# ── Constants ──
MAX_RETRIES         = 4
RETRY_DELAYS        = [2, 5, 10, 20]
RATE_LIMIT_CALLS    = 100
RATE_LIMIT_WINDOW   = 60


# ── Rate limiter ──
class RateLimiter:
    """Thread-safe sliding window rate limiter."""
    def __init__(self, max_calls: int, window_seconds: int):
        self.max_calls      = max_calls
        self.window_seconds = window_seconds
        self.calls          = deque()
        self.lock           = threading.Lock()

    def is_allowed(self) -> bool:
        with self.lock:
            now    = datetime.now()
            cutoff = now - timedelta(seconds=self.window_seconds)
            while self.calls and self.calls[0] < cutoff:
                self.calls.popleft()
            if len(self.calls) >= self.max_calls:
                return False
            self.calls.append(now)
            return True

    def wait_time(self) -> float:
        with self.lock:
            if not self.calls:
                return 0
            oldest = self.calls[0]
            reset  = oldest + timedelta(seconds=self.window_seconds)
            wait   = (reset - datetime.now()).total_seconds()
            return max(0, wait)

_rate_limiter = RateLimiter(RATE_LIMIT_CALLS, RATE_LIMIT_WINDOW)


# ── System prompt ──
SYSTEM_PROMPT = """You are a GST billing assistant for Indian retail shops.
Extract bill information from a shopkeeper's WhatsApp message.
Messages may be in English, Telugu, or Hindi — or a mix.

Extract:
1. customer_name: Who the bill is for (default "Customer" if not mentioned)
2. items: List of products with name, quantity, price

Rules:
- Always translate item names to simple English
- If quantity not mentioned, assume 1
- Weight/unit descriptors must be kept as part of the item name, NOT treated as quantity. qty must remain 1 for these. Units to recognise: gm, g, kg, kgs, ml, l, ltr, litre, litres, gram, grams. Examples:
  "gold 500gm 100000"  → name="gold 500gm",  qty=1, price=100000
  "oil 1kg 120"        → name="oil 1kg",      qty=1, price=120
  "milk 500ml 25"      → name="milk 500ml",   qty=1, price=25
  "rice 2kg 80"        → name="rice 2kg",     qty=1, price=80
  "silver 10gm 6000"   → name="silver 10gm",  qty=1, price=6000
- Normal quantity words (x2, 2x, "2 shirts") are still treated as qty. Only numeric+unit combos are glued to the item name.
- Prices given are BEFORE GST — never add GST yourself
- Ignore greetings, please, thank you etc.
- If a price seems like a phone number (10 digits), ignore it
- Prices are in Indian Rupees only
- Hyphens between item and price are valid separators e.g. "shirt-500" means shirt at Rs.500

Reply ONLY in this exact JSON. No explanation, no markdown:
{
  "customer_name": "string",
  "items": [
    {"name": "string", "qty": number, "price": number}
  ],
  "confidence": 0.95,
  "notes": "any ambiguity or assumption made",
  "error": null
}

confidence: 0.0 to 1.0 — how confident you are in the extraction.
Set error (string) if you cannot extract anything meaningful.
Set notes if you made any assumptions."""


# ── Main parse function ──
def parse_message(message: str) -> dict:
    import anthropic
    start_time = time.time()

    clean_message, warnings = sanitize_message(message)
    parsed_phone = extract_customer_phone(clean_message) if clean_message else None

    if not clean_message:
        return _error_result("Empty or invalid message", warnings=warnings,
                             parse_time_ms=_elapsed_ms(start_time))
    if len(clean_message) < 3:
        return _error_result("Message too short to parse", warnings=warnings,
                             parse_time_ms=_elapsed_ms(start_time))

    log.info(f"Parsing: '{clean_message[:80]}{'...' if len(clean_message)>80 else ''}'")

    if not _rate_limiter.is_allowed():
        wait = _rate_limiter.wait_time()
        log.warning(f"Rate limit hit — retry in {wait:.1f}s")
        return _error_result(
            f"Too many requests — please wait {wait:.0f} seconds",
            warnings=warnings, parse_time_ms=_elapsed_ms(start_time)
        )

    raw_response = None
    last_error   = None
    client       = get_anthropic_client()

    for attempt in range(MAX_RETRIES):
        try:
            if attempt > 0:
                delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                log.info(f"Retry {attempt}/{MAX_RETRIES} after {delay}s")
                time.sleep(delay)

            response = client.messages.create(
                model      = "claude-sonnet-4-20250514",
                max_tokens = 600,
                system     = SYSTEM_PROMPT,
                messages   = [{"role": "user", "content": clean_message}]
            )
            if not response.content:
                return _error_result("Empty response from Claude",
                                     warnings=warnings,
                                     parse_time_ms=_elapsed_ms(start_time))
            block = response.content[0]
            if not isinstance(block, TextBlock):
                return _error_result("Unexpected response format from Claude",
                                     warnings=warnings,
                                     parse_time_ms=_elapsed_ms(start_time))
            raw_response = block.text.strip()
            break

        except anthropic.RateLimitError as e:
            last_error = f"Claude rate limit: {e}"
            log.warning(f"Attempt {attempt+1}: {last_error}")

        except anthropic.APITimeoutError as e:
            last_error = f"Claude timeout: {e}"
            log.warning(f"Attempt {attempt+1}: {last_error}")

        except anthropic.APIConnectionError as e:
            last_error = f"Connection error: {e}"
            log.warning(f"Attempt {attempt+1}: {last_error}")

        except anthropic.APIStatusError as e:
            last_error = f"API error {e.status_code}: {e.message}"
            log.error(f"Attempt {attempt+1}: {last_error}")
            if e.status_code != 529:
                return _error_result(last_error, warnings=warnings,
                                     parse_time_ms=_elapsed_ms(start_time))

        except Exception as e:
            last_error = f"Unexpected error: {e}"
            log.error(f"Attempt {attempt+1}: {last_error}")
            if not isinstance(e, (OSError, ConnectionError)):
                return _error_result(last_error, warnings=warnings,
                                     parse_time_ms=_elapsed_ms(start_time))

    if raw_response is None:
        log.warning("Claude API failed — activating regex fallback parser")
        fallback = _regex_parse_message(clean_message)
        fallback["warnings"] = warnings + fallback.get("warnings", [])
        fallback["warnings"].append(f"Claude API unavailable: {last_error or 'no response'}")
        fallback["parse_time_ms"] = _elapsed_ms(start_time)
        _apply_phone(fallback, parsed_phone)
        return fallback

    try:
        clean_raw = raw_response.replace("```json", "").replace("```", "").strip()
        result    = json.loads(clean_raw)
    except json.JSONDecodeError as e:
        log.error(f"JSON decode failed: {e} | raw: {raw_response[:200]}")
        log.warning("Claude returned invalid JSON — activating regex fallback parser")
        fallback = _regex_parse_message(clean_message)
        fallback["warnings"] = warnings + fallback.get("warnings", [])
        fallback["warnings"].append(f"Claude returned invalid JSON: {e}")
        fallback["parse_time_ms"] = _elapsed_ms(start_time)
        _apply_phone(fallback, parsed_phone)
        return fallback

    result, issues = validate_parsed_response(result)
    if issues:
        log.warning(f"Validation issues: {issues}")
        warnings.extend(issues)

    confidence = result.get("confidence", 0.5)
    if confidence < 0.3:
        log.warning(f"Low confidence: {confidence:.2f} — may be inaccurate")
        warnings.append(f"Low confidence ({confidence:.0%}) — please verify items")

    if not result["items"] and not result.get("error"):
        result["error"] = "No items found in message — please include item names and prices"

    result["warnings"]      = warnings
    result["parse_time_ms"] = _elapsed_ms(start_time)
    _apply_phone(result, parsed_phone)

    log.info(
        f"Parsed: customer='{result['customer_name']}' "
        f"items={len(result['items'])} "
        f"confidence={confidence:.0%} "
        f"time={result['parse_time_ms']}ms"
    )
    return result


def _apply_phone(result: dict, phone: str | None) -> None:
    """Attach parsed customer_phone and strip any phone digits from the name."""
    result["customer_phone"] = phone
    if phone and result.get("customer_name"):
        cleaned = strip_phone_from_name(result["customer_name"])
        if cleaned:
            result["customer_name"] = cleaned


def _error_result(error: str, warnings: list | None = None,
                  parse_time_ms: int = 0) -> dict:
    log.error(f"Parse failed: {error}")
    return {
        "customer_name":  "Customer",
        "customer_phone": None,
        "items":          [],
        "confidence":     0.0,
        "notes":          "",
        "error":          error,
        "warnings":       warnings or [],
        "parse_time_ms":  parse_time_ms,
    }

def _elapsed_ms(start: float) -> int:
    return int((time.time() - start) * 1000)

def format_result(result: dict) -> str:
    lines = []
    if result.get("error"):
        lines.append(f"  ERROR    : {result['error']}")
    else:
        lines.append(f"  Customer : {result['customer_name']}")
        lines.append(f"  Items    : {len(result['items'])} found")
        lines.append(f"  Confidence: {result.get('confidence', 0):.0%}")
        if result.get("notes"):
            lines.append(f"  Notes    : {result['notes']}")
        lines.append("  " + "-" * 45)
        for i, item in enumerate(result["items"], 1):
            lines.append(
                f"  {i}. {item['name']:22} "
                f"qty={item['qty']}  "
                f"Rs.{item['price']:.2f}"
            )
    if result.get("warnings"):
        lines.append("  Warnings :")
        for w in result["warnings"]:
            lines.append(f"    - {w}")
    lines.append(f"  Time     : {result.get('parse_time_ms', 0)}ms")
    return "\n".join(lines)
