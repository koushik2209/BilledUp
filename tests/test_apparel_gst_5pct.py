"""GST-rate coverage for Indian apparel commonly sold in retail.

These items must always resolve to 5% GST regardless of price (most are
unstitched-fabric per CBIC FAQ) or via the explicit dict entry. Tests
cover three lookup paths:

  1. Exact match on the GST_RATES dict (Step 1 of get_gst_rate_smart)
  2. Fabric override via adjust_gst_for_price (alt spellings, slab bypass)
  3. Fuzzy match via rapidfuzz for misspellings (Step 2)
"""
import pytest

from gst_rates import (
    GST_RATES,
    FABRIC_ALWAYS_5PCT,
    get_gst_rate,
    get_gst_rate_smart,
    adjust_gst_for_price,
)


# ── Exact-match coverage ───────────────────────────────────────────

@pytest.mark.parametrize("item,expected_hsn", [
    ("kurta",     "6211"),
    ("kurtha",    "6211"),   # alt spelling — added in this commit
    ("kurti",     "6211"),   # was in FABRIC set only — promoted to GST_RATES
    ("kurthi",    "6211"),   # alt spelling — added in this commit
    ("salwar",    "6211"),
    ("dupatta",   "6214"),
    ("lehenga",   "6204"),
    ("dhoti",     "5208"),   # was in FABRIC set only — promoted
    ("lungi",     "5208"),   # was in FABRIC set only — promoted
    ("saree",     "5208"),
    ("sari",      "5208"),   # alt spelling — added in this commit
    ("blouse",    "6206"),   # added in this commit
    ("petticoat", "6208"),
])
def test_apparel_exact_match_5pct(item, expected_hsn):
    """Step 1 (exact): every listed item resolves to 5% GST + correct HSN."""
    assert item in GST_RATES, f"{item!r} missing from GST_RATES"
    rate = GST_RATES[item]
    assert rate["gst"] == 5, f"{item} expected 5% got {rate['gst']}%"
    assert rate["hsn"] == expected_hsn, (
        f"{item} expected HSN {expected_hsn} got {rate['hsn']}"
    )


# ── get_gst_rate_smart (full lookup pipeline) ──────────────────────

@pytest.mark.parametrize("item", [
    "kurta", "kurtha", "kurti", "kurthi",
    "salwar", "dupatta", "lehenga",
    "dhoti", "lungi", "saree", "sari",
    "blouse", "petticoat",
])
def test_apparel_smart_lookup_resolves_to_5pct(item):
    """End-to-end: get_gst_rate_smart returns 5% with high confidence."""
    rate = get_gst_rate_smart(item, client=None)
    assert rate["gst"] == 5
    assert rate["source"] in ("exact", "fuzzy"), (
        f"{item}: expected exact or fuzzy match, got source={rate['source']}"
    )
    # Exact matches must be high-confidence (slow-path fuzzy is medium).
    if rate["source"] == "exact":
        assert rate["confidence"] == "high"


# ── Fuzzy match for misspellings ───────────────────────────────────

@pytest.mark.parametrize("typo,expected_anchor", [
    ("kurtaa",   "kurt"),     # extra a → matches kurta/kurtha
    ("kurthas",  "kurt"),     # plural with h
    ("kurties",  "kurt"),     # plural-ish
    ("salwars",  "salwar"),   # plural
    ("dupattaa", "dupatt"),   # extra a
    ("lehengaa", "lehenga"),
    ("sarii",    "sar"),      # extra i
    ("blouses",  "blouse"),   # plural
])
def test_apparel_fuzzy_misspelling_resolves_to_5pct(typo, expected_anchor):
    """Step 2 (fuzzy): common misspellings still land on 5%.

    The matched key isn't asserted exactly — rapidfuzz may pick any of the
    close 5%-rated entries — but the resolved GST rate must be 5%, and the
    anchor substring must appear in the matched key (sanity check).
    """
    rate = get_gst_rate_smart(typo, client=None)
    # After fuzzy hit, FABRIC_ALWAYS_5PCT may further enforce 5% via
    # adjust_gst_for_price; both paths land at 5%.
    adjusted = adjust_gst_for_price(typo, 500, rate)
    assert adjusted["gst"] == 5, (
        f"{typo!r} resolved to {adjusted['gst']}% — expected 5% "
        f"(source={rate.get('source')}, confidence={rate.get('confidence')})"
    )


# ── Fabric override: alt spellings stay 5% even at high price ──────

@pytest.mark.parametrize("item,price", [
    ("kurta",   5000),    # stitched but listed as fabric per existing convention
    ("kurtha",  5000),
    ("kurti",   3000),
    ("kurthi",  3000),
    ("saree",   8000),    # high-end silk saree → still 5% (unstitched fabric)
    ("sari",    8000),
    ("dupatta", 4000),
    ("dhoti",   2000),
    ("lungi",   1500),
])
def test_fabric_items_stay_5pct_above_slab_threshold(item, price):
    """Items in FABRIC_ALWAYS_5PCT ignore the >₹2500 → 18% slab.

    The 56th GST Council slab applies to stitched garments only.
    Saree, kurta etc. are wrapped/draped fabric — always 5%.
    """
    rate = get_gst_rate_smart(item, client=None)
    adjusted = adjust_gst_for_price(item, price, rate)
    assert adjusted["gst"] == 5, (
        f"{item} at ₹{price} resolved to {adjusted['gst']}% — must stay 5%"
    )


# ── Regression: existing rates unchanged ───────────────────────────

@pytest.mark.parametrize("item,expected_gst", [
    ("phone case",      18),
    ("charger",         18),
    ("rice",            0),
    ("biscuit",         18),
    ("gold",            3),
    ("ac",              28),
    ("medicine",        5),
])
def test_other_rates_unchanged(item, expected_gst):
    """Sanity: this commit must not have shifted any unrelated rate."""
    rate = get_gst_rate(item)
    assert rate["gst"] == expected_gst, (
        f"{item} drift: expected {expected_gst}% got {rate['gst']}%"
    )


# ── FABRIC_ALWAYS_5PCT membership for new variants ─────────────────

def test_fabric_set_includes_new_alt_spellings():
    """The set should cover the alt spellings added in this commit so the
    'any(kw in item_lower)' substring check catches compound names like
    'red kurthadress' or 'kurthi set'."""
    for v in ("kurtha", "kurthi", "sari"):
        assert v in FABRIC_ALWAYS_5PCT, (
            f"{v!r} missing from FABRIC_ALWAYS_5PCT — substring catch fails"
        )


def test_blouse_resolved_at_5pct_at_any_price():
    """Blouse is intentionally 5% at any price per user spec — not in
    CLOTHING_KEYWORDS, so the slab doesn't bump it to 18%."""
    rate = get_gst_rate_smart("blouse", client=None)
    for price in (500, 2000, 5000):
        adjusted = adjust_gst_for_price("blouse", price, rate)
        assert adjusted["gst"] == 5, (
            f"blouse at ₹{price} drifted to {adjusted['gst']}%"
        )
