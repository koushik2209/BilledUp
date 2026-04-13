"""
core.entities.bill_result — Computed bill totals
"""

from dataclasses import dataclass


@dataclass
class BillResult:
    items:       list
    subtotal:    float
    total_cgst:  float
    total_sgst:  float
    total_igst:  float
    total_gst:   float
    grand_total: float
    in_words:    str
    is_igst:     bool = False
    pricing_type:                  str   = "exclusive"   # "exclusive" | "inclusive"
    subtotal_before_bill_discount: float = 0.0
    bill_discount_type:            str   = "none"        # "none" | "percent" | "flat" | "override"
    bill_discount_value:           float = 0.0
    discount_total:                float = 0.0           # actual ₹ amount deducted
    taxable_amount:                float = 0.0           # after all discounts
    needs_confirmation:            bool  = False
