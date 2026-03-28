"""
claude_parser.py
BilledUp - Production Grade Natural Language Message Parser
-----------------------------------------------------------
Changes from previous version:
- Fixed 529 (overloaded) retry — now retries on 429 AND 529
- Increased retry delays to [2, 5, 10] for better recovery
- MAX_RETRIES increased to 4
- Rate limit increased to 100 calls/60s for 500 customers
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
 
log = logging.getLogger("billedup.parser")
 
# ── Constants ──
MAX_MESSAGE_LENGTH  = 1000
MAX_ITEMS_PER_BILL  = 50
MAX_PRICE           = 10_000_000
MAX_QTY             = 99999
MIN_PRICE           = 0.01
MAX_RETRIES         = 4                  # increased from 3
RETRY_DELAYS        = [2, 5, 10, 20]    # longer delays for overload recovery
RATE_LIMIT_CALLS    = 100               # increased from 50 for 500 customers
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
def validate_parsed_response(result: dict) -> tuple[dict, list]:
    issues = []
    if "items" not in result:
        result["items"] = []
        issues.append("No items field in response")
    if not isinstance(result.get("items"), list):
        result["items"] = []
        issues.append("Items field is not a list")
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
        valid_items.append({
            "name":  name,
            "qty":   round(qty, 3),
            "price": round(price, 2),
        })
    if len(valid_items) > MAX_ITEMS_PER_BILL:
        valid_items = valid_items[:MAX_ITEMS_PER_BILL]
        issues.append(f"Truncated to {MAX_ITEMS_PER_BILL} items")
    result["items"] = valid_items
    return result, issues
 
 
# ── Fallback rule-based parser ──
def _regex_parse_message(message: str) -> dict:
    """
    Rule-based fallback parser used when Claude API fails or returns
    an invalid/unparseable response.

    Supported patterns (case-insensitive):
      • "item price"          — e.g. "rice 50", "soap 25.5"
      • "item-price"          — e.g. "shirt-500", "pen-12"
      • "item x<qty> price"   — e.g. "rice x3 150", "pen x2 24"
      • "item <qty>x price"   — e.g. "rice 3x 150"
      • "<qty> item price"    — e.g. "3 rice 150"

    Returns the same JSON dict structure as Claude's output.
    """
    log.info("Using regex fallback parser")

    # ── Try to extract customer name from common prefixes ──
    customer_name = "Customer"
    # Stop the name at: comma/newline, end-of-string, OR the start of
    # an "word  number" sequence (which signals the first item).
    customer_pat = re.compile(
        r"(?:bill\s+(?:for|to)|for|to)\s+([A-Za-z][A-Za-z\s]{0,29}?)(?=[,\n]|\s+[A-Za-z]+\s+\d|$)",
        re.IGNORECASE,
    )
    cm = customer_pat.search(message)
    if cm:
        candidate = cm.group(1).strip()
        if len(candidate) >= 2:
            customer_name = candidate.title()
            # Strip the "bill for Name" prefix so name doesn't leak into first item
            message = message[:cm.start()] + message[cm.end():]
            message = message.strip()

    items: list[dict] = []
    notes_parts: list[str] = []

    # ── Item extraction patterns (ordered most-specific → least-specific) ──
    # Each pattern must capture: name, optional qty, price
    # Groups: (name, qty_or_None, price)  — order varies per pattern

    patterns = [
        # "item price x<qty>"  — e.g. "pen 10 x 5", "pen 10 x5", "pen 10 × 5"
        (
            re.compile(
                r"([A-Za-z][A-Za-z\s]{0,30}?)\s+(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)",
                re.IGNORECASE,
            ),
            "name_price_xqty",
        ),
        # "item x<qty> price"  or  "item <qty>x price"  — e.g. "rice x3 150", "pen 2x 24"
        (
            re.compile(
                r"([A-Za-z][A-Za-z\s]{0,30}?)\s+[xX](\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)",
                re.IGNORECASE,
            ),
            "name_xqty_price",
        ),
        (
            re.compile(
                r"([A-Za-z][A-Za-z\s]{0,30}?)\s+(\d+(?:\.\d+)?)[xX]\s+(\d+(?:\.\d+)?)",
                re.IGNORECASE,
            ),
            "name_qtyx_price",
        ),
        # "<qty> item price"  — e.g. "3 rice 150", "5 pen 10 3 notebook 40"
        # Anchored to start-of-string, comma/newline, OR after a previous
        # price (digit followed by whitespace) to support repeated patterns.
        (
            re.compile(
                r"(?:^|[,\n]|(?<=\d\s))\s*(\d+(?:\.\d+)?)\s+([A-Za-z][A-Za-z\s]{0,30}?)\s+(\d+(?:\.\d+)?)",
                re.IGNORECASE,
            ),
            "qty_name_price",
        ),
        # "item-price"  — e.g. "shirt-500"
        (
            re.compile(
                r"([A-Za-z][A-Za-z\s]{0,29}?)\s*-\s*(\d+(?:\.\d+)?)",
                re.IGNORECASE,
            ),
            "name_dash_price",
        ),
        # "item price"  — e.g. "soap 25"
        (
            re.compile(
                r"([A-Za-z][A-Za-z\s]{0,29}?)\s+(\d+(?:\.\d+)?)",
                re.IGNORECASE,
            ),
            "name_price",
        ),
        # "itemprice" (no space)  — e.g. "shirt99", "pant700"
        # Name must be ≥2 alpha chars to avoid "x5", "kg50", etc.
        (
            re.compile(
                r"([A-Za-z]{2,}[A-Za-z\s]*)(\d{2,6})",
                re.IGNORECASE,
            ),
            "name_nospace_price",
        ),
    ]

    # Track character spans already consumed so patterns don't double-match
    used_spans: list[tuple[int, int]] = []

    def _overlaps(span: tuple[int, int]) -> bool:
        for s, e in used_spans:
            if span[0] < e and span[1] > s:
                return True
        return False

    # Words that should never be treated as item names
    STOPWORDS = {
        "bill", "for", "to", "and", "the", "please", "kindly",
        "rs", "inr", "rupees", "total", "amount", "price", "qty",
        "quantity", "note", "notes", "hi", "hello", "thanks", "thank",
        "dear", "sir", "madam",
    }

    def _clean_name(raw: str) -> str:
        name = raw.strip(" \t-")
        name = re.sub(r"\s+", " ", name)
        # Drop leading/trailing stop-words
        tokens = name.split()
        while tokens and tokens[0].lower() in STOPWORDS:
            tokens = tokens[1:]
        while tokens and tokens[-1].lower() in STOPWORDS:
            tokens = tokens[:-1]
        return " ".join(tokens).strip()

    for pattern, ptype in patterns:
        for m in pattern.finditer(message):
            if _overlaps(m.span()):
                continue

            try:
                if ptype == "name_price_xqty":
                    name  = _clean_name(m.group(1))
                    price = float(m.group(2))
                    qty   = float(m.group(3))
                elif ptype == "name_xqty_price":
                    name  = _clean_name(m.group(1))
                    qty   = float(m.group(2))
                    price = float(m.group(3))
                elif ptype == "name_qtyx_price":
                    name  = _clean_name(m.group(1))
                    qty   = float(m.group(2))
                    price = float(m.group(3))
                elif ptype == "qty_name_price":
                    qty   = float(m.group(1))
                    name  = _clean_name(m.group(2))
                    price = float(m.group(3))
                elif ptype == "name_dash_price":
                    name  = _clean_name(m.group(1))
                    qty   = 1.0
                    price = float(m.group(2))
                elif ptype == "name_nospace_price":
                    name  = _clean_name(m.group(1))
                    qty   = 1.0
                    price = float(m.group(2))
                else:  # name_price
                    name  = _clean_name(m.group(1))
                    qty   = 1.0
                    price = float(m.group(2))
            except (ValueError, IndexError):
                continue

            # Basic sanity checks (mirrors validate_parsed_response limits)
            if not name or name.lower() in STOPWORDS:
                continue
            if 9_000_000_000 <= price <= 9_999_999_999:
                notes_parts.append(f"Skipped possible phone number: {price:.0f}")
                continue
            if price < MIN_PRICE or price > MAX_PRICE:
                continue
            if qty <= 0 or qty > MAX_QTY:
                qty = 1.0

            items.append({
                "name":  name.title(),
                "qty":   round(qty, 3),
                "price": round(price, 2),
            })
            used_spans.append(m.span())

    # De-duplicate items with identical (name, price) that may have been
    # captured by multiple overlapping patterns
    seen: set[tuple] = set()
    unique_items: list[dict] = []
    for it in items:
        key = (it["name"].lower(), it["price"])
        if key not in seen:
            seen.add(key)
            unique_items.append(it)

    notes_parts.insert(0, "Parsed by regex fallback (Claude API unavailable)")

    return {
        "customer_name": customer_name,
        "items":         unique_items[:MAX_ITEMS_PER_BILL],
        "confidence":    0.6 if unique_items else 0.0,
        "notes":         "; ".join(notes_parts),
        "error":         None if unique_items else "No items found by fallback parser",
        "warnings":      [],
        "parse_time_ms": 0,
    }


# ── Main parse function ──
def parse_message(message: str) -> dict:
    import anthropic
    start_time = time.time()
 
    clean_message, warnings = sanitize_message(message)
 
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
            # Retry on 529 (overloaded). 429 is caught by RateLimitError above.
            if e.status_code != 529:
                return _error_result(last_error, warnings=warnings,
                                     parse_time_ms=_elapsed_ms(start_time))
            if attempt == MAX_RETRIES - 1:
                return _error_result(last_error, warnings=warnings,
                                     parse_time_ms=_elapsed_ms(start_time))
 
        except Exception as e:
            last_error = f"Unexpected error: {e}"
            log.error(f"Attempt {attempt+1}: {last_error}")
            if attempt == MAX_RETRIES - 1:
                return _error_result(last_error, warnings=warnings,
                                     parse_time_ms=_elapsed_ms(start_time))
 
    if raw_response is None:
        log.warning("Claude API failed — activating regex fallback parser")
        fallback = _regex_parse_message(clean_message)
        fallback["warnings"] = warnings + fallback.get("warnings", [])
        fallback["warnings"].append(f"Claude API unavailable: {last_error or 'no response'}")
        fallback["parse_time_ms"] = _elapsed_ms(start_time)
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
 
    log.info(
        f"Parsed: customer='{result['customer_name']}' "
        f"items={len(result['items'])} "
        f"confidence={confidence:.0%} "
        f"time={result['parse_time_ms']}ms"
    )
    return result
 
 
def _error_result(error: str, warnings: list | None = None,
                  parse_time_ms: int = 0) -> dict:
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
 
 
# ── Tests ──
def run_tests():
    print("\n" + "=" * 55)
    print("BilledUp Parser — Unit Tests")
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
 
    test("sanitize empty",           lambda: aeq(sanitize_message("")[0], ""))
    test("sanitize normal",          lambda: atrue(sanitize_message("phone 299")[0] == "phone 299"))
    test("sanitize truncates long",  lambda: atrue(len(sanitize_message("x" * 2000)[0]) <= MAX_MESSAGE_LENGTH))
    test("sanitize control chars",   lambda: atrue("\x00" not in sanitize_message("test\x00msg")[0]))
    test("sanitize strips space",    lambda: aeq(sanitize_message("  hello  ")[0], "hello"))
 
    def _v(raw):
        result, _ = validate_parsed_response(raw)
        return result
 
    test("validate customer default", lambda: aeq(_v({})["customer_name"], "Customer"))
    test("validate keeps customer",   lambda: aeq(_v({"customer_name": "Suresh"})["customer_name"], "Suresh"))
    test("validate neg price",        lambda: aeq(len(_v({"items": [{"name": "phone", "qty": 1, "price": -100}]})["items"]), 0))
    test("validate zero price",       lambda: aeq(len(_v({"items": [{"name": "phone", "qty": 1, "price": 0}]})["items"]), 0))
    test("validate valid item",       lambda: aeq(len(_v({"items": [{"name": "phone", "qty": 1, "price": 299}]})["items"]), 1))
    test("validate empty name",       lambda: aeq(len(_v({"items": [{"name": "", "qty": 1, "price": 299}]})["items"]), 0))
    test("validate clamps confidence",lambda: atrue(0 <= _v({"confidence": 5.0})["confidence"] <= 1.0))
    test("validate missing items",    lambda: aeq(_v({"customer_name": "X"})["items"], []))
 
    def _rl_exhausted():
        rl = RateLimiter(2, 60)
        rl.is_allowed(); rl.is_allowed()
        return rl.is_allowed()
 
    test("rate limiter allows call",  lambda: atrue(RateLimiter(10, 60).is_allowed()))
    test("rate limiter blocks",       lambda: atrue(not _rl_exhausted()))
    test("error result structure",    lambda: atrue("error" in _error_result("test error")))

    # ── Fallback regex parser tests ──
    def _fb(msg):
        return _regex_parse_message(msg)

    # Output shape: all required keys must be present
    def _fb_shape():
        r = _fb("soap 25")
        for k in ("customer_name", "items", "confidence", "notes", "error", "warnings", "parse_time_ms"):
            if k not in r:
                raise AssertionError(f"Missing key '{k}'")
    test("fallback output shape",           _fb_shape)

    # "item price" — simple two-token
    test("fallback item price",             lambda: aeq(_fb("soap 25")["items"][0]["name"], "Soap"))
    test("fallback item price qty=1",       lambda: aeq(_fb("soap 25")["items"][0]["qty"], 1.0))
    test("fallback item price value",       lambda: aeq(_fb("soap 25")["items"][0]["price"], 25.0))

    # "item-price" — hyphen separator
    test("fallback item-price",             lambda: aeq(_fb("shirt-500")["items"][0]["name"], "Shirt"))
    test("fallback item-price value",       lambda: aeq(_fb("shirt-500")["items"][0]["price"], 500.0))

    # "item x<qty> price" — quantity prefix with x
    test("fallback item x2 price name",     lambda: aeq(_fb("pen x2 24")["items"][0]["name"], "Pen"))
    test("fallback item x2 price qty",      lambda: aeq(_fb("pen x2 24")["items"][0]["qty"], 2.0))
    test("fallback item x2 price value",    lambda: aeq(_fb("pen x2 24")["items"][0]["price"], 24.0))

    # "item <qty>x price" — quantity suffix with x
    test("fallback item 3x price qty",      lambda: aeq(_fb("rice 3x 150")["items"][0]["qty"], 3.0))

    # decimal price
    test("fallback decimal price",          lambda: aeq(_fb("soap 25.5")["items"][0]["price"], 25.5))

    # multiple items
    def _fb_multi():
        r = _fb("rice 50 soap 25 pen 10")
        if len(r["items"]) < 2:
            raise AssertionError(f"Expected ≥2 items, got {len(r['items'])}: {r['items']}")
    test("fallback multiple items",         _fb_multi)

    # confidence > 0 when items found, 0.0 when not
    test("fallback confidence >0 w items",  lambda: atrue(_fb("soap 25")["confidence"] > 0))
    test("fallback confidence 0 no items",  lambda: aeq(_fb("hello world")["confidence"], 0.0))

    # error field set when no items found
    test("fallback error on no items",      lambda: atrue(_fb("hello world")["error"] is not None))

    # error is None when items ARE found
    test("fallback no error when items",    lambda: aeq(_fb("soap 25")["error"], None))

    # phone-number price is skipped (10-digit price between 9B and 9.99B)
    test("fallback skips phone number",     lambda: aeq(len(_fb("item 9876543210")["items"]), 0))

    # note says it was regex-parsed
    test("fallback notes mentions regex",   lambda: atrue("regex fallback" in _fb("soap 25")["notes"].lower()))

    # customer extraction from "for <name>"
    test("fallback customer name",          lambda: aeq(_fb("bill for Ravi soap 25")["customer_name"], "Ravi"))

    print("=" * 55)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 55)
    return failed == 0
 
 
if __name__ == "__main__":
    import sys
    if not run_tests():
        print("\nFix failing tests before using parser.")
        sys.exit(1)
 