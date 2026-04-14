"""
core.billing — GST Bill Calculation
-------------------------------------
Pure business logic: no PDF, no DB, no API calls.
"""

import logging

from core.entities import BillItem, BillResult, VALID_GST_SLABS

log = logging.getLogger("billedup.generator")


# ════════════════════════════════════════════════
# NUMBER TO WORDS (Indian numbering)
# ════════════════════════════════════════════════

def number_to_words(amount: float) -> str:
    ones   = ["","One","Two","Three","Four","Five","Six","Seven","Eight","Nine","Ten",
               "Eleven","Twelve","Thirteen","Fourteen","Fifteen","Sixteen","Seventeen",
               "Eighteen","Nineteen"]
    tens_w = ["","","Twenty","Thirty","Forty","Fifty","Sixty","Seventy","Eighty","Ninety"]

    def h(n):
        if n == 0:         return ""
        elif n < 20:       return ones[n] + " "
        elif n < 100:      return tens_w[n // 10] + " " + h(n % 10)
        elif n < 1000:     return ones[n // 100] + " Hundred " + h(n % 100)
        elif n < 100000:   return h(n // 1000) + "Thousand " + h(n % 1000)
        elif n < 10000000: return h(n // 100000) + "Lakh " + h(n % 100000)
        else:              return h(n // 10000000) + "Crore " + h(n % 10000000)

    try:
        amount  = round(float(amount), 2)
        if amount < 0:
            return "Minus " + number_to_words(abs(amount))
        rupees  = int(amount)
        paise   = round((amount - rupees) * 100)
        result  = h(rupees).strip() or "Zero"
        result += " Rupees"
        if paise > 0:
            result += f" and {h(paise).strip()} Paise"
        return result + " Only"
    except Exception as e:
        log.warning(f"number_to_words failed: {e}")
        return "Amount in words unavailable"


# ════════════════════════════════════════════════
# INTRA/INTER STATE
# ════════════════════════════════════════════════

def is_intra_state(shop_state_code: str, customer_state_code: str) -> bool:
    """
    Determine if transaction is intra-state (CGST+SGST) or inter-state (IGST).
    If customer state code is empty/missing, assumes intra-state (same as shop).
    """
    if not customer_state_code or not customer_state_code.strip():
        return True
    return shop_state_code.strip() == customer_state_code.strip()


# ════════════════════════════════════════════════
# BILL CALCULATION
# ════════════════════════════════════════════════

def calculate_bill(
    items: list,
    gst_client=None,
    shop_state_code: str = "",
    customer_state_code: str = "",
    bill_of_supply: bool = False,
    is_inclusive: bool = False,
    bill_discount_type: str = "none",
    bill_discount_value: float = 0.0,
) -> BillResult:
    """Calculate bill totals.

    bill_of_supply=True → all GST is zero (shop has no GSTIN).
    Items still get HSN codes for record-keeping but gst_rate is forced to 0%.
    is_inclusive=True  → each item price is the GST-inclusive unit price;
    the taxable base is backed out as price / (1 + rate/100).
    bill_discount_type/value → "none" | "percent" | "flat" | "override".
    Item-level discounts live on each BillItem (item_discount_type/value).
    """
    if not items:
        raise ValueError("Cannot generate bill — no items provided")

    intra = is_intra_state(shop_state_code, customer_state_code)
    if bill_of_supply:
        log.info("Bill of Supply — no GST applied")
    else:
        log.info(f"Tax type: {'CGST+SGST (intra-state)' if intra else 'IGST (inter-state)'}")

    from gst_rates import get_gst_rate_smart, adjust_gst_for_price

    # ─────────────────────────────────────────────
    # Pass 1: resolve rates, apply item-level discounts, build `lines`
    # ─────────────────────────────────────────────
    lines = []
    pre_bill_subtotal = 0.0

    for item in items:
        item.validate()
        name  = item.name.strip()
        qty   = round(float(item.qty), 3)
        price = round(float(item.price), 2)

        # Use pre-resolved rates if available (set during preview),
        # otherwise look up fresh — keeps preview and final bill in sync.
        if item.hsn:
            hsn      = item.hsn
            gst_rate = item.gst_rate
        else:
            try:
                rate_info = get_gst_rate_smart(name, gst_client)
            except Exception as e:
                log.warning(f"GST lookup failed for '{name}': {e} — using default 18%")
                rate_info = {"hsn": "9999", "gst": 18}

            # Apply price-based slab (clothing/footwear)
            rate_info = adjust_gst_for_price(name, price, rate_info)
            hsn      = rate_info.get("hsn", "9999")
            gst_rate = rate_info.get("gst", 18)

        # Bill of Supply → force zero GST (keep HSN for records)
        if bill_of_supply:
            gst_rate = 0
        elif gst_rate not in VALID_GST_SLABS:
            log.warning(f"Invalid slab {gst_rate}% for '{name}' — correcting to 18%")
            gst_rate = 18

        raw_amount = round(qty * price, 2)

        # ── Item-level discount ──
        i_disc_type = getattr(item, "item_discount_type", "none") or "none"
        i_disc_val  = float(getattr(item, "item_discount_value", 0.0) or 0.0)
        if i_disc_type == "percent":
            pct = max(0.0, min(100.0, i_disc_val))
            item_discount = round(raw_amount * pct / 100, 2)
        elif i_disc_type == "flat":
            item_discount = round(min(max(0.0, i_disc_val), raw_amount), 2)
        else:
            item_discount = 0.0
        line_after_item_disc = round(raw_amount - item_discount, 2)
        pre_bill_subtotal += line_after_item_disc

        lines.append({
            "name": name, "qty": qty, "price": price, "hsn": hsn,
            "gst_rate": gst_rate, "raw_amount": raw_amount,
            "i_disc_type": i_disc_type, "i_disc_val": i_disc_val,
            "line_after_item_disc": line_after_item_disc,
            "scaled_line": line_after_item_disc,  # overwritten in Pass 2
        })

    pre_bill_subtotal = round(pre_bill_subtotal, 2)

    # ─────────────────────────────────────────────
    # Bill-level discount: compute scale factor, apply to each line
    # Scale-ratio approach preserves per-item GST rates.
    # ─────────────────────────────────────────────
    scale: float = 1.0
    natural_grand: float = 0.0          # populated for override mode
    override_target: float = 0.0        # clamped target for override mode

    if bill_discount_type == "flat" and bill_discount_value and bill_discount_value > 0:
        deduction = min(round(float(bill_discount_value), 2), pre_bill_subtotal)
        scale = 0.0 if pre_bill_subtotal == 0 else (pre_bill_subtotal - deduction) / pre_bill_subtotal
    elif bill_discount_type == "percent" and bill_discount_value and bill_discount_value > 0:
        pct = max(0.0, min(100.0, float(bill_discount_value)))
        scale = 1.0 - pct / 100.0
    elif bill_discount_type == "override":
        # Compute the natural grand total from item-discounted lines so we
        # know the ratio that brings every line to the shopkeeper's target.
        for L in lines:
            base = L["line_after_item_disc"]
            r = L["gst_rate"]
            if bill_of_supply or r == 0 or is_inclusive:
                natural_grand += base
            else:
                natural_grand += round(base * (1 + r / 100), 2)
        natural_grand = round(natural_grand, 2)
        target = round(float(bill_discount_value or 0.0), 2)
        # Clamp: target above natural is bogus (ignore); below zero is bogus.
        override_target = max(0.0, min(target, natural_grand))
        if natural_grand <= 0 or override_target == natural_grand:
            scale = 1.0                 # no-op
        elif override_target == 0:
            scale = 0.0
        else:
            scale = override_target / natural_grand
    # else: scale stays 1.0 (no-op)

    if scale != 1.0:
        for L in lines:
            L["scaled_line"] = round(L["line_after_item_disc"] * scale, 2)

        # Rounding absorption (flat only): force scaled sum to equal
        # pre_bill_subtotal − deduction exactly, so the discount row shows
        # the exact amount the shopkeeper said.
        if bill_discount_type == "flat":
            deduction = min(round(float(bill_discount_value), 2), pre_bill_subtotal)
            expected_sum = round(pre_bill_subtotal - deduction, 2)
            actual_sum   = round(sum(L["scaled_line"] for L in lines), 2)
            delta = round(expected_sum - actual_sum, 2)
            if lines and abs(delta) <= 0.10 and delta != 0.0:
                lines[-1]["scaled_line"] = round(lines[-1]["scaled_line"] + delta, 2)

    # ─────────────────────────────────────────────
    # Pass 2: compute per-item GST on scaled lines, assemble BillItems
    # ─────────────────────────────────────────────
    processed: list = []
    subtotal  = 0.0

    for L in lines:
        base_lump = L["scaled_line"]
        gst_rate  = L["gst_rate"]

        if is_inclusive and not bill_of_supply and gst_rate > 0:
            # scaled_line is GST-inclusive → back out the base.
            amount  = round(base_lump / (1 + gst_rate / 100), 2)
            gst_amt = round(base_lump - amount, 2)
        else:
            amount  = base_lump
            gst_amt = round(amount * gst_rate / 100, 2)

        if bill_of_supply:
            cgst = sgst = igst = 0.0
        elif intra:
            cgst = round(gst_amt / 2, 2)
            sgst = round(gst_amt - cgst, 2)
            igst = 0.0
        else:
            cgst = 0.0
            sgst = 0.0
            igst = gst_amt

        total     = round(amount + gst_amt, 2)
        subtotal += amount

        processed.append(BillItem(
            name=L["name"].title(), qty=L["qty"], price=L["price"],
            hsn=L["hsn"], gst_rate=gst_rate, amount=amount,
            cgst=cgst, sgst=sgst, igst=igst, total=total,
            raw_amount=L["raw_amount"],
            item_discount_type=L["i_disc_type"],
            item_discount_value=L["i_disc_val"],
        ))

    subtotal    = round(subtotal, 2)
    total_cgst  = round(sum(i.cgst for i in processed), 2)
    total_sgst  = round(sum(i.sgst for i in processed), 2)
    total_igst  = round(sum(i.igst for i in processed), 2)
    total_gst   = round(total_cgst + total_sgst + total_igst, 2)
    grand_total = round(subtotal + total_gst, 2)

    # ─────────────────────────────────────────────
    # Override exactness: nudge the last processed item so grand_total
    # equals the shopkeeper's target to the paisa. Per-line rounding at
    # different GST rates can drift by ±₹0.01–₹0.05; anything larger than
    # ₹0.50 is left alone so a real scale-ratio bug surfaces.
    # ─────────────────────────────────────────────
    if bill_discount_type == "override" and processed and scale != 1.0:
        delta = round(override_target - grand_total, 2)
        if abs(delta) <= 0.50 and delta != 0.0:
            last = processed[-1]
            r    = last.gst_rate
            new_total = round(last.total + delta, 2)
            if bill_of_supply or r == 0:
                new_amount = new_total
                new_gst    = 0.0
            else:
                new_amount = round(new_total / (1 + r / 100), 2)
                new_gst    = round(new_total - new_amount, 2)
            if bill_of_supply:
                new_cgst = new_sgst = new_igst = 0.0
            elif intra:
                new_cgst = round(new_gst / 2, 2)
                new_sgst = round(new_gst - new_cgst, 2)
                new_igst = 0.0
            else:
                new_cgst = 0.0
                new_sgst = 0.0
                new_igst = new_gst
            # Update aggregates relative to the last item's old contribution.
            subtotal   = round(subtotal   - last.amount + new_amount, 2)
            total_cgst = round(total_cgst - last.cgst   + new_cgst,   2)
            total_sgst = round(total_sgst - last.sgst   + new_sgst,   2)
            total_igst = round(total_igst - last.igst   + new_igst,   2)
            total_gst  = round(total_cgst + total_sgst + total_igst,  2)
            # Mutate the last BillItem in place.
            last.amount = new_amount
            last.cgst   = new_cgst
            last.sgst   = new_sgst
            last.igst   = new_igst
            last.total  = new_total
            grand_total = round(subtotal + total_gst, 2)
            # Keep scaled_line in sync so discount-bookkeeping stays correct.
            if is_inclusive:
                lines[-1]["scaled_line"] = new_total
            else:
                lines[-1]["scaled_line"] = new_amount

    # taxable_amount = post-bill-discount base-or-lump
    #   • exclusive: sum of (pre-GST) bases = subtotal
    #   • inclusive: sum of (GST-inclusive) scaled_lines
    scaled_sum     = round(sum(L["scaled_line"] for L in lines), 2)
    taxable_amount = scaled_sum if is_inclusive else subtotal
    discount_total = round(pre_bill_subtotal - scaled_sum, 2)

    log.info(
        f"Bill - {len(processed)} items | "
        f"subtotal=Rs.{subtotal} | "
        f"gst=Rs.{total_gst} | "
        f"total=Rs.{grand_total}"
    )
    pricing_type = "inclusive" if is_inclusive else "exclusive"

    return BillResult(
        items=processed, subtotal=subtotal,
        total_cgst=total_cgst, total_sgst=total_sgst,
        total_igst=total_igst, total_gst=total_gst,
        grand_total=grand_total,
        in_words=number_to_words(grand_total),
        is_igst=not intra,
        pricing_type=pricing_type,
        subtotal_before_bill_discount=pre_bill_subtotal,
        bill_discount_type=bill_discount_type,
        bill_discount_value=float(bill_discount_value or 0.0),
        discount_total=discount_total,
        taxable_amount=taxable_amount,
    )
