"""
core.entities.bill_item — Line item in a GST bill
"""

from dataclasses import dataclass


@dataclass
class BillItem:
    name:     str
    qty:      float
    price:    float
    hsn:      str   = ""
    gst_rate: int   = 18
    amount:   float = 0.0
    cgst:     float = 0.0
    sgst:     float = 0.0
    igst:     float = 0.0
    total:    float = 0.0
    item_discount_type:  str   = "none"   # "none" | "percent" | "flat"
    item_discount_value: float = 0.0
    raw_amount:          float = 0.0      # qty * price, pre-discount

    def validate(self):
        if not self.name or not self.name.strip():
            raise ValueError("Item name cannot be empty")
        if self.qty <= 0:
            raise ValueError(f"Quantity must be positive for '{self.name}'")
        if self.price <= 0:
            raise ValueError(f"Price must be positive for '{self.name}'")
        if self.price > 10_000_000:
            raise ValueError(f"Price exceeds Rs.1 crore for '{self.name}'")
