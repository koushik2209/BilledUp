"""
core.daily_summary — Daily WhatsApp Summary Formatter
-------------------------------------------------------
Pure function: dict → WhatsApp message string.
No database access, no network calls.
"""
import math


def _safe(data, *keys, default=0):
    """Navigate nested dict safely; return default for missing/None."""
    val = data
    for key in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(key)
    return val if val is not None else default


def _fmt(amount) -> str:
    """
    Indian number formatting — never shows decimals.

    < 1,000         → ₹850
    1,000 – 99,999  → ₹34,200  (Indian comma grouping)
    ≥ 1,00,000      → ₹4.2L    (traditional round to 1 decimal)
    """
    amount = max(0, round(float(amount or 0)))
    if amount < 1_000:
        return f"₹{int(amount)}"
    if amount < 1_00_000:
        s = str(int(amount))
        last3 = s[-3:]
        rest  = s[:-3]
        parts = []
        while rest:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        parts.append(last3)
        return "₹" + ",".join(parts)
    # Traditional rounding to 1 decimal (avoids Python banker's rounding on .5)
    tenths = math.floor(amount / 10_000 + 0.5) / 10
    return f"₹{tenths:.1f}L"


def _month_section(
    month: dict,
    has_gstin: bool,
    include_sale: bool = True,
    include_gst: bool = True,
    skip_returns: bool = False,
) -> list:
    """Build month section as a list of lines (including leading blank)."""
    name           = str(month.get("name") or "Month")
    total_bills    = int(_safe(month, "total_bills"))
    grand_total    = float(_safe(month, "grand_total"))
    sale_amount    = float(_safe(month, "sale_amount"))
    total_gst      = float(_safe(month, "total_gst"))
    returns_count  = int(_safe(month, "returns_count"))
    returns_amount = float(_safe(month, "returns_amount"))

    lines = ["", f"📅 *{name} So Far*", ""]
    if has_gstin:
        lines.append(f"🧾 Bills: {total_bills}")
        if include_sale:
            lines.append(f"💰 Sale Amount: {_fmt(sale_amount)}")
        if include_gst:
            lines.append(f"🏛 GST Collected: {_fmt(total_gst)}")
        lines.append(f"✅ Total: {_fmt(grand_total)}")
    else:
        lines += [
            f"🧾 Bills: {total_bills}",
            f"💰 Total: {_fmt(grand_total)}",
        ]
    if returns_count > 0 and not skip_returns:
        lines += ["", f"↩️ Returns: {returns_count} bill(s) — {_fmt(returns_amount)}"]
    return lines


def format_daily_summary(data: dict) -> str:
    """
    Format a daily billing summary for WhatsApp delivery.

    Input shape matches get_daily_summary_data() output.
    Rules:
    - has_gstin=True  → show sale_amount + GST + grand_total
    - has_gstin=False → show grand_total only (no GST)
    - total_bills=0   → motivational zero-bills message
    - returns_count>0 → show returns + net total lines
    - Max 18 lines; trim month_returns → month_gst → month_sale if over
    """
    if not isinstance(data, dict):
        return "Could not generate summary."

    has_gstin = bool(_safe(data, "has_gstin", default=False))
    date_str  = str(_safe(data, "date", default=""))
    today     = data.get("today") or {}
    month     = data.get("month")

    total_bills = int(_safe(today, "total_bills"))

    # Zero-bills path
    if total_bills == 0:
        lines = [
            f"📊 *Today's Summary — {date_str}*",
            "",
            "No bills today — happens sometimes.",
            "Get ready for a stronger tomorrow.",
        ]
        if month:
            lines += _month_section(month, has_gstin)
        return "\n".join(lines)

    grand_total    = float(_safe(today, "grand_total"))
    sale_amount    = float(_safe(today, "sale_amount"))
    total_gst      = float(_safe(today, "total_gst"))
    returns_count  = int(_safe(today, "returns_count"))
    returns_amount = float(_safe(today, "returns_amount"))

    lines = [f"📊 *Today's Summary — {date_str}*", ""]
    if has_gstin:
        lines += [
            f"🧾 Bills: {total_bills}",
            f"💰 Sale Amount: {_fmt(sale_amount)}",
            f"🏛 GST Collected: {_fmt(total_gst)}",
            f"✅ Grand Total: {_fmt(grand_total)}",
        ]
    else:
        lines += [
            f"🧾 Bills: {total_bills}",
            f"💰 Total Billed: {_fmt(grand_total)}",
        ]

    if returns_count > 0:
        net = max(0.0, grand_total - returns_amount)
        lines += [
            "",
            f"↩️ Returns: {returns_count} bill(s) — {_fmt(returns_amount)}",
            "",
            f"🏁 Net Total: {_fmt(net)}",
        ]

    if month:
        # Try trimming until ≤ 18 lines (trim order per spec)
        skip_returns = False
        skip_gst     = False
        skip_sale    = False
        for _ in range(3):
            month_lines = _month_section(
                month, has_gstin,
                include_sale=not skip_sale,
                include_gst=not skip_gst,
                skip_returns=skip_returns,
            )
            if len(lines + month_lines) <= 18:
                break
            if not skip_returns:
                skip_returns = True
            elif not skip_gst:
                skip_gst = True
            else:
                skip_sale = True
        lines += month_lines

    return "\n".join(lines[:18])
