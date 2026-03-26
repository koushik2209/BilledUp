"""
claude_parser.py
BillEasy - Production Grade Natural Language Message Parser
-----------------------------------------------------------
Features:
- Retry logic with exponential backoff
- Rate limiting protection
- Input sanitization
- Confidence scoring
- Detailed structured logging
- Supports English, Telugu, Hindi
- Handles malformed, partial, ambiguous messages
"""
 
import re
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from collections import deque
import anthropic
from anthropic.types import TextBlock

from config import get_anthropic_client
 
log = logging.getLogger("billeasy.parser")
 
# ── Constants ──
MAX_MESSAGE_LENGTH  = 1000    # characters
MAX_ITEMS_PER_BILL  = 50      # sanity limit
MAX_PRICE           = 10_000_000  # Rs.1 crore
MAX_QTY             = 99999
MIN_PRICE           = 0.01
MAX_RETRIES         = 3
RETRY_DELAYS        = [1, 2, 4]  # seconds — exponential backoff
RATE_LIMIT_CALLS    = 50         # max calls
RATE_LIMIT_WINDOW   = 60         # per 60 seconds
 
# ── Rate limiter ──
class RateLimiter:
    """
    Thread-safe sliding window rate limiter.
    Prevents API abuse and protects against cost spikes.
    """
    def __init__(self, max_calls: int, window_seconds: int):
        self.max_calls      = max_calls
        self.window_seconds = window_seconds
        self.calls          = deque()
        self.lock           = threading.Lock()
 
    def is_allowed(self) -> bool:
        with self.lock:
            now    = datetime.now()
            cutoff = now - timedelta(seconds=self.window_seconds)
            # Remove old calls outside window
            while self.calls and self.calls[0] < cutoff:
                self.calls.popleft()
            if len(self.calls) >= self.max_calls:
                return False
            self.calls.append(now)
            return True
 
    def wait_time(self) -> float:
        """Returns seconds until next call is allowed."""
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
- Prices given are BEFORE GST — never add GST yourself
- Ignore greetings, please, thank you etc.
- If a price seems like a phone number (10 digits), ignore it
- Prices are in Indian Rupees only
 
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
 
# ── Input sanitization ──
def sanitize_message(message: str) -> tuple[str, list]:
    """
    Clean and validate raw input message.
    Returns (cleaned_message, list_of_warnings).
    """
    warnings = []
 
    if not message:
        return "", ["Empty message"]
 
    # Truncate if too long
    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[:MAX_MESSAGE_LENGTH]
        warnings.append(
            f"Message truncated to {MAX_MESSAGE_LENGTH} characters"
        )
 
    # Remove null bytes and control characters (except newlines/tabs)
    message = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", message)
 
    # Normalize whitespace
    message = re.sub(r"\s+", " ", message).strip()
 
    # Warn if message looks like it might be a template injection attempt
    suspicious = [
        "system:", "assistant:", "ignore previous",
        "forget instructions", "new instructions"
    ]
    msg_lower = message.lower()
    for pattern in suspicious:
        if pattern in msg_lower:
            warnings.append(f"Suspicious pattern detected: '{pattern}'")
            # Remove the suspicious part
            message = re.sub(re.escape(pattern), "", message, flags=re.IGNORECASE)
 
    if len(message.strip()) < 3:
        warnings.append("Message too short to parse")
 
    return message.strip(), warnings
 
 
# ── Response validation ──
def validate_parsed_response(result: dict) -> tuple[dict, list]:
    """
    Validate and clean Claude's parsed response.
    Returns (cleaned_result, list_of_issues).
    """
    issues = []
 
    # Ensure required fields exist
    if "items" not in result:
        result["items"] = []
        issues.append("No items field in response")
 
    if not isinstance(result.get("items"), list):
        result["items"] = []
        issues.append("Items field is not a list")
 
    # Validate customer name
    customer = str(result.get("customer_name", "")).strip()
    if not customer or customer.lower() in ("null", "none", "unknown", ""):
        customer = "Customer"
    # Remove special characters from name
    customer = re.sub(r"[^\w\s\.\-]", "", customer).strip()
    result["customer_name"] = customer or "Customer"
 
    # Validate confidence
    confidence = float(result.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))
    result["confidence"] = confidence
 
    # Validate each item
    valid_items = []
    for i, item in enumerate(result.get("items", [])):
        if not isinstance(item, dict):
            issues.append(f"Item {i+1} is not a dict — skipped")
            continue
 
        # Name
        name = str(item.get("name", "")).strip()
        if not name:
            issues.append(f"Item {i+1} has no name — skipped")
            continue
        # Clean name — only alphanumeric and spaces
        name = re.sub(r"[^\w\s\-\.]", "", name).strip()
        if not name:
            issues.append(f"Item {i+1} name invalid after cleaning — skipped")
            continue
 
        # Quantity
        try:
            qty = float(item.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1.0
            issues.append(f"Item '{name}' qty invalid — defaulting to 1")
        qty = max(0.001, min(float(MAX_QTY), qty))
 
        # Price
        try:
            price = float(item.get("price", 0))
        except (TypeError, ValueError):
            issues.append(f"Item '{name}' price invalid — skipped")
            continue
        if price <= 0:
            issues.append(f"Item '{name}' price is {price} — skipped")
            continue
        if price > MAX_PRICE:
            issues.append(f"Item '{name}' price Rs.{price} exceeds limit — skipped")
            continue
 
        # Sanity check — price looks like a phone number
        if price >= 9000000000 and price <= 9999999999:
            issues.append(f"Item '{name}' price looks like phone number — skipped")
            continue
 
        valid_items.append({
            "name":  name,
            "qty":   round(qty, 3),
            "price": round(price, 2),
        })
 
    # Enforce max items
    if len(valid_items) > MAX_ITEMS_PER_BILL:
        valid_items = valid_items[:MAX_ITEMS_PER_BILL]
        issues.append(f"Truncated to {MAX_ITEMS_PER_BILL} items")
 
    result["items"] = valid_items
    return result, issues
 
 
# ── Main parse function ──
def parse_message(message: str) -> dict:
    """
    Parse a natural language WhatsApp message into structured bill data.
 
    Supports:
    - English: "phone case 299 charger 199 customer Suresh"
    - Telugu:  "oka phone case 299 ki rendu charger 199 ki Ravi ki bill"
    - Hindi:   "ek charger 499 aur do earphone 199 - Suresh ka bill"
    - Mixed:   Any combination of the above
 
    Returns:
        {
            "customer_name": str,
            "items": [{"name": str, "qty": float, "price": float}],
            "confidence": float,  # 0.0 to 1.0
            "notes": str,
            "error": str or None,
            "warnings": list,
            "parse_time_ms": int,
        }
    """
    start_time = time.time()
 
    # ── Step 1: Sanitize input ──
    clean_message, warnings = sanitize_message(message)
 
    if not clean_message:
        return _error_result(
            "Empty or invalid message",
            warnings=warnings,
            parse_time_ms=_elapsed_ms(start_time)
        )
 
    if len(clean_message) < 3:
        return _error_result(
            "Message too short to parse",
            warnings=warnings,
            parse_time_ms=_elapsed_ms(start_time)
        )
 
    log.info(f"Parsing: '{clean_message[:80]}{'...' if len(clean_message)>80 else ''}'")
 
    # ── Step 2: Rate limit check ──
    if not _rate_limiter.is_allowed():
        wait = _rate_limiter.wait_time()
        log.warning(f"Rate limit hit — retry in {wait:.1f}s")
        return _error_result(
            f"Too many requests — please wait {wait:.0f} seconds",
            warnings=warnings,
            parse_time_ms=_elapsed_ms(start_time)
        )
 
    # ── Step 3: Call Claude with retry ──
    raw_response = None
    last_error   = None
 
    for attempt in range(MAX_RETRIES):
        try:
            if attempt > 0:
                delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                log.info(f"Retry {attempt}/{MAX_RETRIES} after {delay}s")
                time.sleep(delay)
 
            response = get_anthropic_client().messages.create(
                model      = "claude-sonnet-4-20250514",
                max_tokens = 600,
                system     = SYSTEM_PROMPT,
                messages   = [{"role": "user", "content": clean_message}]
            )
            block = response.content[0]
            if not isinstance(block, TextBlock):
                return _error_result(
                    "Unexpected response format from Claude",
                    warnings=warnings,
                    parse_time_ms=_elapsed_ms(start_time),
                )
            raw_response = block.text.strip()
            break  # success
 
        except anthropic.RateLimitError as e:
            last_error = f"Claude rate limit: {e}"
            log.warning(f"Attempt {attempt+1}: {last_error}")
            if attempt == MAX_RETRIES - 1:
                return _error_result(last_error, warnings=warnings,
                                     parse_time_ms=_elapsed_ms(start_time))
 
        except anthropic.APITimeoutError as e:
            last_error = f"Claude timeout: {e}"
            log.warning(f"Attempt {attempt+1}: {last_error}")
            if attempt == MAX_RETRIES - 1:
                return _error_result(last_error, warnings=warnings,
                                     parse_time_ms=_elapsed_ms(start_time))
 
        except anthropic.APIConnectionError as e:
            last_error = f"Connection error: {e}"
            log.warning(f"Attempt {attempt+1}: {last_error}")
            if attempt == MAX_RETRIES - 1:
                return _error_result(last_error, warnings=warnings,
                                     parse_time_ms=_elapsed_ms(start_time))
 
        except anthropic.APIStatusError as e:
            last_error = f"API error {e.status_code}: {e.message}"
            log.error(f"Attempt {attempt+1}: {last_error}")
            # Don't retry on 4xx errors except 429
            if e.status_code != 429:
                return _error_result(last_error, warnings=warnings,
                                     parse_time_ms=_elapsed_ms(start_time))
 
        except Exception as e:
            last_error = f"Unexpected error: {e}"
            log.error(f"Attempt {attempt+1}: {last_error}")
            if attempt == MAX_RETRIES - 1:
                return _error_result(last_error, warnings=warnings,
                                     parse_time_ms=_elapsed_ms(start_time))
 
    if raw_response is None:
        return _error_result(
            last_error or "No response from Claude",
            warnings=warnings,
            parse_time_ms=_elapsed_ms(start_time)
        )
 
    # ── Step 4: Parse JSON ──
    try:
        clean_raw = raw_response.replace("```json", "").replace("```", "").strip()
        result    = json.loads(clean_raw)
    except json.JSONDecodeError as e:
        log.error(f"JSON decode failed: {e} | raw: {raw_response[:200]}")
        return _error_result(
            f"Could not understand response format",
            warnings=warnings,
            parse_time_ms=_elapsed_ms(start_time)
        )
 
    # ── Step 5: Validate response ──
    result, issues = validate_parsed_response(result)
 
    if issues:
        log.warning(f"Validation issues: {issues}")
        warnings.extend(issues)
 
    # ── Step 6: Check confidence ──
    confidence = result.get("confidence", 0.5)
    if confidence < 0.3:
        log.warning(f"Low confidence: {confidence:.2f} — may be inaccurate")
        warnings.append(f"Low confidence ({confidence:.0%}) — please verify items")
 
    # ── Step 7: Check we got items ──
    if not result["items"] and not result.get("error"):
        result["error"] = "No items found in message — please include item names and prices"
 
    # ── Final result ──
    result["warnings"]      = warnings
    result["parse_time_ms"] = _elapsed_ms(start_time)
 
    log.info(
        f"Parsed: customer='{result['customer_name']}' "
        f"items={len(result['items'])} "
        f"confidence={confidence:.0%} "
        f"time={result['parse_time_ms']}ms"
    )
    return result
 
 
def _error_result(error: str, warnings: list | None = None, parse_time_ms: int = 0) -> dict:
    """Standard error response."""
    log.error(f"Parse failed: {error}")
    return {
        "customer_name": "Customer",
        "items":         [],
        "confidence":    0.0,
        "notes":         "",
        "error":         error,
        "warnings":      warnings or [],
        "parse_time_ms": parse_time_ms,
    }
 
def _elapsed_ms(start: float) -> int:
    """Elapsed milliseconds since start."""
    return int((time.time() - start) * 1000)
 
 
def format_result(result: dict) -> str:
    """Format parsed result for terminal display."""
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
 
 
# ── Tests ──
def run_tests():
    print("\n" + "=" * 55)
    print("BillEasy Parser — Unit Tests")
    print("=" * 55)
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
        if a != b: raise AssertionError(f"Expected '{b}' got '{a}'")
 
    # Sanitize tests
    test("sanitize empty",
         lambda: aeq(sanitize_message("")[0], ""))
    test("sanitize normal",
         lambda: atrue(sanitize_message("phone 299")[0] == "phone 299"))
    test("sanitize truncates long",
         lambda: atrue(len(sanitize_message("x" * 2000)[0]) <= MAX_MESSAGE_LENGTH))
    test("sanitize removes control chars",
         lambda: atrue("\x00" not in sanitize_message("test\x00msg")[0]))
    test("sanitize strips whitespace",
         lambda: aeq(sanitize_message("  hello  ")[0], "hello"))
 
    # Validate response tests
    def _v(raw):
        result, _ = validate_parsed_response(raw)
        return result
 
    test("validate adds customer default",
         lambda: aeq(_v({})["customer_name"], "Customer"))
    test("validate keeps valid customer",
         lambda: aeq(_v({"customer_name": "Suresh"})["customer_name"], "Suresh"))
    test("validate removes negative price",
         lambda: aeq(len(_v({"items": [{"name": "phone", "qty": 1, "price": -100}]})["items"]), 0))
    test("validate removes zero price",
         lambda: aeq(len(_v({"items": [{"name": "phone", "qty": 1, "price": 0}]})["items"]), 0))
    test("validate keeps valid item",
         lambda: aeq(len(_v({"items": [{"name": "phone", "qty": 1, "price": 299}]})["items"]), 1))
    test("validate removes empty name",
         lambda: aeq(len(_v({"items": [{"name": "", "qty": 1, "price": 299}]})["items"]), 0))
    test("validate clamps confidence",
         lambda: atrue(0 <= _v({"confidence": 5.0})["confidence"] <= 1.0))
    test("validate handles missing items field",
         lambda: aeq(_v({"customer_name": "X"})["items"], []))
 
    # Rate limiter test
    test("rate limiter allows call",
         lambda: atrue(RateLimiter(10, 60).is_allowed()))
    test("rate limiter blocks after limit",
         lambda: atrue(not _rate_limit_exhausted()))
 
    # Error result test
    test("error result structure",
         lambda: atrue("error" in _error_result("test error")))
 
    print("=" * 55)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 55)
    return failed == 0
 
def _rate_limit_exhausted():
    rl = RateLimiter(2, 60)
    rl.is_allowed()
    rl.is_allowed()
    return rl.is_allowed()  # Should be False
 
 
# ── Demo ──
if __name__ == "__main__":
    import sys
 
    if not run_tests():
        print("\nFix failing tests before using parser.")
        sys.exit(1)
 
    print("\n" + "=" * 55)
    print("BillEasy Parser — Live Test")
    print("=" * 55)
 
    test_messages = [
        # English
        "phone case 299 charger 499 earphones 199 customer Suresh",
        # Telugu
        "oka phone case 299 ki rendu charger 199 ki Ravi ki bill cheyyi",
        # Hindi
        "bhai ek charger 499 aur do earphone 199 - Suresh ka bill",
        # Mixed with qty
        "tempered glass 149 power bank 899 2 customer Ramesh",
        # No customer name
        "screen guard 99 usb cable 149",
        # Edge case — empty
        "",
        # Edge case — only greeting
        "hello bhai",
    ]
 
    for msg in test_messages:
        print(f"\nInput : '{msg}'")
        print("-" * 55)
        result = parse_message(msg)
        print(format_result(result))
 
    print("\n" + "=" * 55)
    print("All tests complete.")
    print("=" * 55)