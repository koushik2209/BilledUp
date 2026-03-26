import os
import json
import logging
import anthropic

CACHE_FILE = "gst_cache.json"

GST_RATES = {

    # ── MOBILE & ELECTRONICS ──
    "mobile": {"hsn": "8517", "gst": 18},
    "phone": {"hsn": "8517", "gst": 18},
    "smartphone": {"hsn": "8517", "gst": 18},
    "feature phone": {"hsn": "8517", "gst": 12},
    "charger": {"hsn": "8504", "gst": 18},
    "adapter": {"hsn": "8504", "gst": 18},
    "cable": {"hsn": "8544", "gst": 18},
    "usb cable": {"hsn": "8544", "gst": 18},
    "data cable": {"hsn": "8544", "gst": 18},
    "earphones": {"hsn": "8518", "gst": 18},
    "earbuds": {"hsn": "8518", "gst": 18},
    "headphones": {"hsn": "8518", "gst": 18},
    "headset": {"hsn": "8518", "gst": 18},
    "cover": {"hsn": "3926", "gst": 18},
    "case": {"hsn": "3926", "gst": 18},
    "back cover": {"hsn": "3926", "gst": 18},
    "phone case": {"hsn": "3926", "gst": 18},
    "screen guard": {"hsn": "3926", "gst": 18},
    "tempered glass": {"hsn": "7007", "gst": 18},
    "screen protector": {"hsn": "3926", "gst": 18},
    "powerbank": {"hsn": "8507", "gst": 18},
    "power bank": {"hsn": "8507", "gst": 18},
    "speaker": {"hsn": "8518", "gst": 18},
    "bluetooth speaker": {"hsn": "8518", "gst": 18},
    "tablet": {"hsn": "8471", "gst": 18},
    "laptop": {"hsn": "8471", "gst": 18},
    "computer": {"hsn": "8471", "gst": 18},
    "desktop": {"hsn": "8471", "gst": 18},
    "monitor": {"hsn": "8528", "gst": 18},
    "keyboard": {"hsn": "8471", "gst": 18},
    "mouse": {"hsn": "8471", "gst": 18},
    "printer": {"hsn": "8443", "gst": 18},
    "scanner": {"hsn": "8471", "gst": 18},
    "pen drive": {"hsn": "8523", "gst": 18},
    "memory card": {"hsn": "8523", "gst": 18},
    "hard disk": {"hsn": "8471", "gst": 18},
    "ssd": {"hsn": "8471", "gst": 18},
    "router": {"hsn": "8517", "gst": 18},
    "wifi": {"hsn": "8517", "gst": 18},
    "smartwatch": {"hsn": "9102", "gst": 18},
    "watch": {"hsn": "9102", "gst": 18},
    "camera": {"hsn": "8525", "gst": 18},
    "tripod": {"hsn": "9620", "gst": 18},
    "selfie stick": {"hsn": "9620", "gst": 18},
    "television": {"hsn": "8528", "gst": 18},
    "tv": {"hsn": "8528", "gst": 18},
    "led tv": {"hsn": "8528", "gst": 18},
    "remote": {"hsn": "8543", "gst": 18},
    "set top box": {"hsn": "8528", "gst": 18},

    # ── HOME APPLIANCES ──
    "refrigerator": {"hsn": "8418", "gst": 18},
    "fridge": {"hsn": "8418", "gst": 18},
    "washing machine": {"hsn": "8450", "gst": 18},
    "air conditioner": {"hsn": "8415", "gst": 28},
    "ac": {"hsn": "8415", "gst": 28},
    "cooler": {"hsn": "8479", "gst": 18},
    "fan": {"hsn": "8414", "gst": 18},
    "ceiling fan": {"hsn": "8414", "gst": 18},
    "mixer": {"hsn": "8509", "gst": 18},
    "grinder": {"hsn": "8509", "gst": 18},
    "juicer": {"hsn": "8509", "gst": 18},
    "microwave": {"hsn": "8516", "gst": 18},
    "oven": {"hsn": "8516", "gst": 18},
    "iron": {"hsn": "8516", "gst": 12},
    "geyser": {"hsn": "8516", "gst": 18},
    "water heater": {"hsn": "8516", "gst": 18},
    "water purifier": {"hsn": "8421", "gst": 18},
    "vacuum cleaner": {"hsn": "8508", "gst": 18},
    "induction": {"hsn": "8516", "gst": 18},
    "gas stove": {"hsn": "7321", "gst": 18},
    "pressure cooker": {"hsn": "7323", "gst": 12},

    # ── MEDICINES & MEDICAL ──
    "medicine": {"hsn": "3004", "gst": 5},
    "tablet medicine": {"hsn": "3004", "gst": 5},
    "capsule": {"hsn": "3004", "gst": 5},
    "syrup": {"hsn": "3004", "gst": 5},
    "injection": {"hsn": "3004", "gst": 5},
    "ointment": {"hsn": "3004", "gst": 5},
    "cream": {"hsn": "3304", "gst": 18},
    "surgical": {"hsn": "9018", "gst": 12},
    "bandage": {"hsn": "3005", "gst": 12},
    "gloves": {"hsn": "3926", "gst": 12},
    "mask": {"hsn": "6307", "gst": 5},
    "sanitizer": {"hsn": "3808", "gst": 18},
    "thermometer": {"hsn": "9025", "gst": 12},
    "bp machine": {"hsn": "9018", "gst": 12},
    "glucometer": {"hsn": "9027", "gst": 12},

    # ── CLOTHING & TEXTILES ──
    "shirt": {"hsn": "6205", "gst": 12},
    "tshirt": {"hsn": "6109", "gst": 12},
    "t-shirt": {"hsn": "6109", "gst": 12},
    "trouser": {"hsn": "6203", "gst": 12},
    "pants": {"hsn": "6203", "gst": 12},
    "jeans": {"hsn": "6203", "gst": 12},
    "saree": {"hsn": "5208", "gst": 5},
    "salwar": {"hsn": "6211", "gst": 5},
    "kurta": {"hsn": "6211", "gst": 5},
    "dress": {"hsn": "6204", "gst": 12},
    "jacket": {"hsn": "6201", "gst": 12},
    "sweater": {"hsn": "6110", "gst": 12},
    "socks": {"hsn": "6115", "gst": 12},
    "underwear": {"hsn": "6107", "gst": 12},
    "bra": {"hsn": "6212", "gst": 12},
    "legging": {"hsn": "6104", "gst": 12},
    "dupatta": {"hsn": "6214", "gst": 5},
    "fabric": {"hsn": "5208", "gst": 5},
    "cloth": {"hsn": "5208", "gst": 5},
    "bedsheet": {"hsn": "6302", "gst": 12},
    "pillow": {"hsn": "9404", "gst": 12},
    "blanket": {"hsn": "6301", "gst": 12},
    "towel": {"hsn": "6302", "gst": 12},
    "curtain": {"hsn": "6303", "gst": 12},

    # ── FOOTWEAR ──
    "chappal": {"hsn": "6402", "gst": 5},
    "sandal": {"hsn": "6402", "gst": 5},
    "shoes": {"hsn": "6403", "gst": 18},
    "sneakers": {"hsn": "6403", "gst": 18},
    "boots": {"hsn": "6403", "gst": 18},
    "slippers": {"hsn": "6402", "gst": 5},

    # ── FOOD & GROCERY ──
    "rice": {"hsn": "1006", "gst": 0},
    "wheat": {"hsn": "1001", "gst": 0},
    "flour": {"hsn": "1101", "gst": 0},
    "dal": {"hsn": "0713", "gst": 0},
    "sugar": {"hsn": "1701", "gst": 0},
    "salt": {"hsn": "2501", "gst": 0},
    "oil": {"hsn": "1511", "gst": 5},
    "ghee": {"hsn": "0405", "gst": 12},
    "butter": {"hsn": "0405", "gst": 12},
    "milk": {"hsn": "0401", "gst": 0},
    "curd": {"hsn": "0403", "gst": 0},
    "egg": {"hsn": "0407", "gst": 0},
    "chicken": {"hsn": "0207", "gst": 0},
    "fish": {"hsn": "0302", "gst": 0},
    "biscuit": {"hsn": "1905", "gst": 18},
    "chocolate": {"hsn": "1806", "gst": 18},
    "namkeen": {"hsn": "2106", "gst": 12},
    "chips": {"hsn": "2008", "gst": 18},
    "noodles": {"hsn": "1902", "gst": 18},
    "tea": {"hsn": "0902", "gst": 5},
    "coffee": {"hsn": "0901", "gst": 5},
    "cold drink": {"hsn": "2202", "gst": 28},
    "soft drink": {"hsn": "2202", "gst": 28},
    "water bottle": {"hsn": "2201", "gst": 18},
    "juice": {"hsn": "2009", "gst": 12},
    "honey": {"hsn": "0409", "gst": 0},
    "spices": {"hsn": "0910", "gst": 5},
    "masala": {"hsn": "0910", "gst": 5},

    # ── STATIONERY & BOOKS ──
    "pen": {"hsn": "9608", "gst": 18},
    "pencil": {"hsn": "9609", "gst": 12},
    "notebook": {"hsn": "4820", "gst": 12},
    "book": {"hsn": "4901", "gst": 0},
    "textbook": {"hsn": "4901", "gst": 0},
    "register": {"hsn": "4820", "gst": 12},
    "stapler": {"hsn": "8305", "gst": 18},
    "scissors": {"hsn": "8213", "gst": 18},
    "eraser": {"hsn": "4016", "gst": 18},
    "sharpener": {"hsn": "8214", "gst": 18},
    "marker": {"hsn": "9608", "gst": 18},
    "highlighter": {"hsn": "9608", "gst": 18},
    "paper": {"hsn": "4802", "gst": 12},
    "envelope": {"hsn": "4817", "gst": 18},
    "calculator": {"hsn": "8470", "gst": 18},

    # ── HARDWARE & TOOLS ──
    "hammer": {"hsn": "8205", "gst": 18},
    "screwdriver": {"hsn": "8205", "gst": 18},
    "drill": {"hsn": "8467", "gst": 18},
    "saw": {"hsn": "8202", "gst": 18},
    "paint": {"hsn": "3208", "gst": 18},
    "brush": {"hsn": "9603", "gst": 18},
    "pipe": {"hsn": "3917", "gst": 18},
    "wire": {"hsn": "8544", "gst": 18},
    "switch": {"hsn": "8536", "gst": 18},
    "bulb": {"hsn": "8539", "gst": 12},
    "led bulb": {"hsn": "8539", "gst": 12},
    "tube light": {"hsn": "8539", "gst": 12},
    "battery": {"hsn": "8506", "gst": 18},
    "lock": {"hsn": "8301", "gst": 18},
    "hinge": {"hsn": "8302", "gst": 18},
    "nail": {"hsn": "7317", "gst": 18},
    "screw": {"hsn": "7318", "gst": 18},
    "cement": {"hsn": "2523", "gst": 28},
    "sand": {"hsn": "2505", "gst": 5},
    "brick": {"hsn": "6901", "gst": 5},

    # ── PERSONAL CARE ──
    "shampoo": {"hsn": "3305", "gst": 18},
    "soap": {"hsn": "3401", "gst": 18},
    "toothpaste": {"hsn": "3306", "gst": 18},
    "toothbrush": {"hsn": "9603", "gst": 18},
    "facewash": {"hsn": "3304", "gst": 18},
    "moisturizer": {"hsn": "3304", "gst": 18},
    "sunscreen": {"hsn": "3304", "gst": 18},
    "lipstick": {"hsn": "3304", "gst": 18},
    "foundation": {"hsn": "3304", "gst": 18},
    "kajal": {"hsn": "3304", "gst": 18},
    "perfume": {"hsn": "3303", "gst": 28},
    "deo": {"hsn": "3307", "gst": 18},
    "deodorant": {"hsn": "3307", "gst": 18},
    "razor": {"hsn": "8212", "gst": 18},
    "shaving cream": {"hsn": "3307", "gst": 18},
    "hair oil": {"hsn": "3305", "gst": 18},
    "hair colour": {"hsn": "3305", "gst": 18},
    "nail polish": {"hsn": "3304", "gst": 18},
    "diaper": {"hsn": "9619", "gst": 18},
    "cotton": {"hsn": "5201", "gst": 0},

    # ── FURNITURE & HOME ──
    "chair": {"hsn": "9401", "gst": 18},
    "table": {"hsn": "9403", "gst": 18},
    "bed": {"hsn": "9403", "gst": 18},
    "sofa": {"hsn": "9401", "gst": 18},
    "almirah": {"hsn": "9403", "gst": 18},
    "cupboard": {"hsn": "9403", "gst": 18},
    "mattress": {"hsn": "9404", "gst": 18},
    "mirror": {"hsn": "7009", "gst": 18},
    "frame": {"hsn": "4414", "gst": 12},
    "bucket": {"hsn": "3924", "gst": 18},
    "mug": {"hsn": "3924", "gst": 18},
    "plate": {"hsn": "7323", "gst": 12},
    "glass": {"hsn": "7013", "gst": 18},
    "bowl": {"hsn": "7323", "gst": 12},
    "spoon": {"hsn": "8215", "gst": 12},
    "knife": {"hsn": "8211", "gst": 18},
    "pan": {"hsn": "7323", "gst": 18},
    "flask": {"hsn": "9617", "gst": 18},
    "broom": {"hsn": "9603", "gst": 5},
    "mop": {"hsn": "9603", "gst": 5},

    # ── AUTOMOBILES & SPARES ──
    "tyre": {"hsn": "4011", "gst": 28},
    "tube": {"hsn": "4013", "gst": 18},
    "engine oil": {"hsn": "2710", "gst": 18},
    "brake": {"hsn": "8708", "gst": 28},
    "helmet": {"hsn": "6506", "gst": 18},
    "battery car": {"hsn": "8507", "gst": 18},
    "headlight": {"hsn": "8512", "gst": 18},
    "wiper": {"hsn": "8512", "gst": 18},

    # ── SPORTS & TOYS ──
    "bat": {"hsn": "9506", "gst": 12},
    "ball": {"hsn": "9506", "gst": 12},
    "cycle": {"hsn": "8712", "gst": 12},
    "toy": {"hsn": "9503", "gst": 12},
    "doll": {"hsn": "9502", "gst": 12},
    "puzzle": {"hsn": "9503", "gst": 12},
    "gym equipment": {"hsn": "9506", "gst": 18},
    "yoga mat": {"hsn": "9506", "gst": 18},

    # ── AGRICULTURAL ──
    "seed": {"hsn": "1209", "gst": 0},
    "fertilizer": {"hsn": "3102", "gst": 0},
    "pesticide": {"hsn": "3808", "gst": 18},
    "tractor": {"hsn": "8701", "gst": 12},
    "pump": {"hsn": "8413", "gst": 12},

    # ── DEFAULT ──
    "default": {"hsn": "9999", "gst": 18}
}


_log = logging.getLogger("billeasy.gst")


def load_cache():
    """Load previously Claude-found items from cache file."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            _log.warning(f"GST cache corrupted, starting fresh: {e}")
    return {}


def save_cache(cache):
    """Save newly found items to cache file — atomic write."""
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    os.replace(tmp, CACHE_FILE)


def get_gst_rate(item_name):
    """
    Basic lookup — checks hardcoded list only.
    Use get_gst_rate_smart() for full Claude fallback.
    """
    item_lower = item_name.lower().strip()

    if item_lower in GST_RATES:
        return GST_RATES[item_lower]

    for key in GST_RATES:
        if key in item_lower:
            return GST_RATES[key]

    return GST_RATES["default"]


def get_gst_rate_smart(item_name, client=None):
    """
    Smart GST lookup — 4 step fallback system:
    1. Hardcoded list  — instant, zero cost
    2. Cache           — instant, zero cost
    3. Claude API      — accurate, tiny cost
    4. Default 18%     — last resort only
    """
    item_lower = item_name.lower().strip()

    # Step 1 — hardcoded list
    if item_lower in GST_RATES:
        return GST_RATES[item_lower]

    for key in GST_RATES:
        if key in item_lower:
            return GST_RATES[key]

    # Step 2 — cache
    cache = load_cache()
    if item_lower in cache:
        return cache[item_lower]

    # Step 3 — Claude API
    if client:
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=100,
                messages=[{
                    "role": "user",
                    "content": (
                        f"What is the HSN code and GST rate for '{item_name}' "
                        f"in India as of 2024? Reply in this exact JSON format "
                        f"only, no explanation, no markdown:\n"
                        f'{{\"hsn\": \"XXXX\", \"gst\": 18}}'
                    )
                }]
            )

            raw = response.content[0].text.strip()

            # Clean up any accidental markdown
            raw = raw.replace("```json", "").replace("```", "").strip()

            result = json.loads(raw)

            if "hsn" in result and "gst" in result:
                # Validate gst is one of India's 5 valid slabs
                valid_slabs = [0, 5, 12, 18, 28]
                if result["gst"] not in valid_slabs:
                    result["gst"] = 18  # safe default

                # Save to cache
                cache[item_lower] = result
                save_cache(cache)
                return result

        except Exception as e:
            _log.warning(f"Claude lookup failed for '{item_name}': {e}")

    # Step 4 — last resort default
    _log.warning(
        f"Unknown item '{item_name}' — "
        f"using default 18%. Add manually to GST_RATES if needed."
    )
    return GST_RATES["default"]


def get_all_categories():
    """Returns all product categories in hardcoded list."""
    return [k for k in GST_RATES.keys() if k != "default"]


if __name__ == "__main__":
    import anthropic
    from config import ANTHROPIC_API_KEY

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Test 1 — hardcoded list
    print("=" * 50)
    print("TEST 1 — Hardcoded GST Rate Lookup")
    print("=" * 50)
    test_items = [
        "phone case", "charger", "rice", "medicine",
        "shirt", "cement", "led bulb", "spoon",
        "toothpaste", "cold drink", "saree", "chappal"
    ]
    for item in test_items:
        rate = get_gst_rate(item)
        print(f"{item:22} → HSN: {rate['hsn']:6}  GST: {rate['gst']}%")

    print(f"\nTotal categories covered: {len(get_all_categories())}")

    # Test 2 — Claude fallback
    print("\n" + "=" * 50)
    print("TEST 2 — Claude Fallback for Unknown Items")
    print("=" * 50)
    unknown_items = ["drone", "artificial flowers", "aquarium", "chess board"]
    for item in unknown_items:
        rate = get_gst_rate_smart(item, client)
        print(f"{item:22} → HSN: {rate['hsn']:6}  GST: {rate['gst']}%")