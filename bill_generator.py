"""
bill_generator.py
BilledUp - Production Grade GST Bill Generator
 
Changes from previous version:
- Invoice number now stored in SQLite (not JSON file) — survives redeploys
- TAX INVOICE vs BILL OF SUPPLY based on GSTIN
- GSTIN placeholder shows "Not Registered" on bill
- Support phone from config (not hardcoded)
- Thread-safe invoice sequence using DB transactions
"""
import os, re, logging
from datetime import datetime
from dataclasses import dataclass
from xml.sax.saxutils import escape as xml_escape
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle,
    Paragraph, Spacer, HRFlowable
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
 
log = logging.getLogger("billedup.generator")
 
BRAND_BLUE  = colors.HexColor("#1a73e8")
BRAND_DARK  = colors.HexColor("#1a1a2e")
LIGHT_GRAY  = colors.HexColor("#f8f9fa")
MID_GRAY    = colors.HexColor("#dee2e6")
TEXT_GRAY   = colors.HexColor("#6c757d")
WHITE       = colors.white
BLACK       = colors.black
VALID_GST_SLABS  = {0, 3, 5, 12, 18, 28}
GSTIN_REGEX      = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
PLACEHOLDER_GSTIN = "GSTIN00000000000"
 
PAGE_W = 182 * mm   # A4 usable width after 14mm margins
 
 
# ════════════════════════════════════════════════
# DATA CLASSES
# ════════════════════════════════════════════════
 
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
 
    def validate(self):
        if not self.name or not self.name.strip():
            raise ValueError("Item name cannot be empty")
        if self.qty <= 0:
            raise ValueError(f"Quantity must be positive for '{self.name}'")
        if self.price <= 0:
            raise ValueError(f"Price must be positive for '{self.name}'")
        if self.price > 10_000_000:
            raise ValueError(f"Price exceeds Rs.1 crore for '{self.name}'")
 
 
@dataclass
class ShopProfile:
    shop_id:    str
    name:       str
    address:    str
    gstin:      str
    phone:      str
    state:      str = "Telangana"
    state_code: str = "36"
    upi:        str = ""
 
    @property
    def has_gstin(self) -> bool:
        """True if shop has a real, valid-format GSTIN (not placeholder)."""
        if not self.gstin or self.gstin == PLACEHOLDER_GSTIN:
            return False
        return bool(GSTIN_REGEX.match(self.gstin.upper().strip()))
 
    @property
    def display_gstin(self) -> str:
        """GSTIN to show on bill."""
        return self.gstin.upper() if self.has_gstin else "Not Registered"
 
    @property
    def invoice_type(self) -> str:
        """TAX INVOICE if GSTIN registered, else BILL OF SUPPLY."""
        return "TAX INVOICE" if self.has_gstin else "BILL OF SUPPLY"
 
    def validate(self):
        if not self.name.strip():
            raise ValueError("Shop name cannot be empty")
        if not self.address.strip():
            raise ValueError("Shop address cannot be empty")
        if self.has_gstin:
            if not GSTIN_REGEX.match(self.gstin.upper().strip()):
                raise ValueError(
                    f"Invalid GSTIN format: '{self.gstin}'. "
                    f"Expected: 22AAAAA0000A1Z5"
                )
        if len(re.sub(r"\D", "", self.phone)) < 10:
            raise ValueError(f"Invalid phone number: '{self.phone}'")
 
 
@dataclass
class CustomerInfo:
    name:       str
    phone:      str = ""
    address:    str = ""
    gstin:      str = ""
    state:      str = ""
    state_code: str = ""
 
    def validate(self):
        if not self.name.strip():
            raise ValueError("Customer name cannot be empty")
        if self.gstin and not GSTIN_REGEX.match(self.gstin.upper().strip()):
            raise ValueError(f"Invalid customer GSTIN: '{self.gstin}'")
 
 
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
 
 
# ════════════════════════════════════════════════
# INVOICE NUMBER — DB backed (survives redeploys)
# ════════════════════════════════════════════════

def generate_invoice_number(shop_id: str, is_return: bool = False) -> str:
    """
    Generate next sequential invoice number.
    Stored in DB via SQLAlchemy — survives server restarts and redeploys.
    Thread-safe via mutex + row-level lock.

    is_return=True → prefix "CN" (Credit Note) instead of BILL_PREFIX.
    Credit notes use a separate sequence key to avoid gaps in invoice numbering.
    """
    if not shop_id.strip():
        raise ValueError("shop_id cannot be empty")

    from database import generate_next_sequence

    shop_key = shop_id.upper().strip()
    year     = datetime.now().strftime("%Y")

    if is_return:
        prefix = "CN"
        seq_key = f"CN_{shop_key}"
    else:
        from config import BILL_PREFIX
        prefix = BILL_PREFIX
        seq_key = shop_key

    sequence = generate_next_sequence(seq_key, year)
    invoice_no = f"{prefix}-{year}-{shop_key}-{sequence:05d}"
    log.info(f"Generated {'credit note' if is_return else 'invoice'}: {invoice_no}")
    return invoice_no


 
# ════════════════════════════════════════════════
# NUMBER TO WORDS
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
# BILL CALCULATION
# ════════════════════════════════════════════════
 
def is_intra_state(shop_state_code: str, customer_state_code: str) -> bool:
    """
    Determine if transaction is intra-state (CGST+SGST) or inter-state (IGST).
    If customer state code is empty/missing, assumes intra-state (same as shop).
    """
    if not customer_state_code or not customer_state_code.strip():
        return True
    return shop_state_code.strip() == customer_state_code.strip()


def calculate_bill(
    items: list,
    gst_client=None,
    shop_state_code: str = "",
    customer_state_code: str = "",
    bill_of_supply: bool = False,
) -> BillResult:
    """Calculate bill totals.

    bill_of_supply=True → all GST is zero (shop has no GSTIN).
    Items still get HSN codes for record-keeping but gst_rate is forced to 0%.
    """
    if not items:
        raise ValueError("Cannot generate bill — no items provided")

    intra = is_intra_state(shop_state_code, customer_state_code)
    if bill_of_supply:
        log.info("Bill of Supply — no GST applied")
    else:
        log.info(f"Tax type: {'CGST+SGST (intra-state)' if intra else 'IGST (inter-state)'}")

    from gst_rates import get_gst_rate_smart, adjust_gst_for_price
    processed = []
    subtotal  = 0.0

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

        amount  = round(qty * price, 2)
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

        total    = round(amount + gst_amt, 2)
        subtotal += amount

        processed.append(BillItem(
            name=name.title(), qty=qty, price=price,
            hsn=hsn, gst_rate=gst_rate, amount=amount,
            cgst=cgst, sgst=sgst, igst=igst, total=total,
        ))

    subtotal    = round(subtotal, 2)
    total_cgst  = round(sum(i.cgst for i in processed), 2)
    total_sgst  = round(sum(i.sgst for i in processed), 2)
    total_igst  = round(sum(i.igst for i in processed), 2)
    total_gst   = round(total_cgst + total_sgst + total_igst, 2)
    grand_total = round(subtotal + total_gst, 2)

    log.info(
        f"Bill - {len(processed)} items | "
        f"subtotal=Rs.{subtotal} | "
        f"gst=Rs.{total_gst} | "
        f"total=Rs.{grand_total}"
    )
    return BillResult(
        items=processed, subtotal=subtotal,
        total_cgst=total_cgst, total_sgst=total_sgst,
        total_igst=total_igst, total_gst=total_gst,
        grand_total=grand_total,
        in_words=number_to_words(grand_total),
        is_igst=not intra,
    )
 
 
# ════════════════════════════════════════════════
# PDF STYLES
# ════════════════════════════════════════════════
 
def _styles() -> dict:
    return {
        # Header
        "shop_name":     ParagraphStyle("sn",  fontSize=18, textColor=BRAND_DARK, fontName="Helvetica-Bold", alignment=TA_LEFT),
        "doc_type":      ParagraphStyle("dt",  fontSize=11, textColor=BRAND_BLUE, fontName="Helvetica-Bold", alignment=TA_RIGHT),
        "doc_sub":       ParagraphStyle("ds",  fontSize=8,  textColor=TEXT_GRAY,  fontName="Helvetica",      alignment=TA_RIGHT),
        # Sections
        "section_label": ParagraphStyle("sl",  fontSize=7,  textColor=BRAND_BLUE, fontName="Helvetica-Bold", spaceBefore=0, spaceAfter=1),
        "label":         ParagraphStyle("lb",  fontSize=7,  textColor=TEXT_GRAY,  fontName="Helvetica-Bold"),
        "small":         ParagraphStyle("sm",  fontSize=8,  textColor=BLACK,      fontName="Helvetica"),
        "small_bold":    ParagraphStyle("sb",  fontSize=8,  textColor=BLACK,      fontName="Helvetica-Bold"),
        "small_right":   ParagraphStyle("sr",  fontSize=8,  textColor=BLACK,      fontName="Helvetica",      alignment=TA_RIGHT),
        "meta_label":    ParagraphStyle("ml",  fontSize=8,  textColor=TEXT_GRAY,  fontName="Helvetica"),
        "meta_value":    ParagraphStyle("mv",  fontSize=8,  textColor=BLACK,      fontName="Helvetica-Bold"),
        "gstin":         ParagraphStyle("gs",  fontSize=8,  textColor=BRAND_BLUE, fontName="Helvetica-Bold"),
        # Table
        "th":            ParagraphStyle("th",  fontSize=8,  textColor=WHITE,      fontName="Helvetica-Bold"),
        "td":            ParagraphStyle("td",  fontSize=8,  textColor=BLACK,      fontName="Helvetica"),
        "td_bold":       ParagraphStyle("tdb", fontSize=8,  textColor=BLACK,      fontName="Helvetica-Bold"),
        # Totals
        "total_label":   ParagraphStyle("trl", fontSize=9,  textColor=TEXT_GRAY,  fontName="Helvetica"),
        "total_value":   ParagraphStyle("trv", fontSize=9,  textColor=BLACK,      fontName="Helvetica",      alignment=TA_RIGHT),
        "grand_label":   ParagraphStyle("gl",  fontSize=11, textColor=WHITE,      fontName="Helvetica-Bold", alignment=TA_RIGHT),
        "grand_value":   ParagraphStyle("gv",  fontSize=12, textColor=WHITE,      fontName="Helvetica-Bold", alignment=TA_RIGHT),
        "words":         ParagraphStyle("wd",  fontSize=7,  textColor=TEXT_GRAY,  fontName="Helvetica-Oblique"),
        # Footer
        "footer":        ParagraphStyle("ft",  fontSize=7,  textColor=TEXT_GRAY,  fontName="Helvetica",      alignment=TA_CENTER),
        "powered":       ParagraphStyle("pw",  fontSize=8,  textColor=BRAND_BLUE, fontName="Helvetica-Bold", alignment=TA_CENTER),
        "terms":         ParagraphStyle("tm",  fontSize=6,  textColor=TEXT_GRAY,  fontName="Helvetica"),
    }
 
 
# ════════════════════════════════════════════════
# PDF GENERATION
# ════════════════════════════════════════════════
 
def generate_pdf_bill(
    shop:           ShopProfile,
    customer:       CustomerInfo,
    items:          list,
    invoice_number: str,
    gst_client=None,
    is_return:      bool = False,
) -> tuple[bytes, BillResult]:
    """
    Generate a GST bill PDF in memory.
    Returns (pdf_bytes, bill_result).

    Bill type:
    - is_return=True       → CREDIT NOTE
    - Shop WITH GSTIN      → TAX INVOICE
    - Shop WITHOUT GSTIN   → BILL OF SUPPLY
    """
    log.info(f"Generating {'credit note' if is_return else 'bill'} {invoice_number} for {shop.name}")
    shop.validate()
    customer.validate()
    if not items:
        raise ValueError("Items list is empty")
    if not invoice_number.strip():
        raise ValueError("Invoice number cannot be empty")

    bill = calculate_bill(
        items, gst_client, shop.state_code, customer.state_code,
        bill_of_supply=not shop.has_gstin,
    )

    # For credit notes, negate all monetary values in the result
    if is_return:
        bill = BillResult(
            items=[BillItem(
                name=i.name, qty=i.qty, price=-i.price, hsn=i.hsn,
                gst_rate=i.gst_rate, cgst=-i.cgst, sgst=-i.sgst,
                igst=-i.igst, total=-i.total,
            ) for i in bill.items],
            subtotal=-bill.subtotal,
            total_cgst=-bill.total_cgst, total_sgst=-bill.total_sgst,
            total_igst=-bill.total_igst, total_gst=-bill.total_gst,
            grand_total=-bill.grand_total, is_igst=bill.is_igst,
            in_words=bill.in_words,
        )

    from io import BytesIO
    from config import PLATFORM_NAME, PLATFORM_TAGLINE, PLATFORM_SUPPORT

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=14*mm, leftMargin=14*mm,
        topMargin=12*mm, bottomMargin=12*mm,
        compress=1,
    )
 
    s     = _styles()
    story = []
    today = datetime.now().strftime("%d %B %Y")
    HW    = PAGE_W / 2

    # ── HEADER: Shop name (left) + Doc type (right) ──
    if is_return:
        doc_type = "CREDIT NOTE"
        doc_sub  = "Return / Refund"
    else:
        doc_type = shop.invoice_type
        doc_sub  = "GST Registered" if shop.has_gstin else "Unregistered"

    ht = Table([[
        [
            Paragraph(xml_escape(shop.name), s["shop_name"]),
        ],
        [
            Paragraph(doc_type, s["doc_type"]),
            Paragraph(doc_sub,  s["doc_sub"]),
        ],
    ]], colWidths=[HW, HW])
    ht.setStyle(TableStyle([
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LINEBELOW",     (0,0), (-1,-1), 1.5, BRAND_BLUE),
    ]))
    story.append(ht)
    story.append(Spacer(1, 2*mm))

    # ── SHOP + INVOICE DETAILS (two-column row) ──
    shop_info = [
        Paragraph(xml_escape(shop.address), s["small"]),
        Paragraph(f"Phone: {xml_escape(shop.phone)}", s["small"]),
    ]
    if shop.has_gstin:
        shop_info.append(Paragraph(f"GSTIN: {xml_escape(shop.display_gstin)}", s["gstin"]))
    shop_info.append(
        Paragraph(f"State: {xml_escape(shop.state)}  |  Code: {xml_escape(shop.state_code)}", s["small"]),
    )

    meta_data = [
        [Paragraph("Invoice No:",  s["meta_label"]), Paragraph(invoice_number, s["meta_value"])],
        [Paragraph("Date:",        s["meta_label"]), Paragraph(today,          s["meta_value"])],
    ]
    if shop.upi:
        meta_data.append(
            [Paragraph("UPI:", s["meta_label"]), Paragraph(shop.upi, s["meta_value"])],
        )
    meta_tbl = Table(meta_data, colWidths=[22*mm, HW - 24*mm])
    meta_tbl.setStyle(TableStyle([
        ("TOPPADDING",    (0,0), (-1,-1), 1),
        ("BOTTOMPADDING", (0,0), (-1,-1), 1),
        ("LEFTPADDING",   (0,0), (-1,-1), 0),
        ("RIGHTPADDING",  (0,0), (-1,-1), 0),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
    ]))

    info_row = Table([[shop_info, meta_tbl]], colWidths=[HW, HW])
    info_row.setStyle(TableStyle([
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("LEFTPADDING",   (0,0), (-1,-1), 0),
        ("RIGHTPADDING",  (0,0), (-1,-1), 0),
    ]))
    story.append(info_row)
    story.append(Spacer(1, 3*mm))

    # ── CUSTOMER ──
    cust_lines = [
        Paragraph("BILL TO", s["section_label"]),
        Paragraph(f"<b>{xml_escape(customer.name)}</b>", s["small_bold"]),
    ]
    if customer.address:
        cust_lines.append(Paragraph(xml_escape(customer.address), s["small"]))
    if customer.phone:
        cust_lines.append(Paragraph(f"Phone: {xml_escape(customer.phone)}", s["small"]))
    if customer.gstin:
        cust_lines.append(Paragraph(f"GSTIN: {xml_escape(customer.gstin.upper())}", s["gstin"]))
    if customer.state:
        state_str = f"State: {xml_escape(customer.state)}"
        if customer.state_code:
            state_str += f"  |  Code: {xml_escape(customer.state_code)}"
        cust_lines.append(Paragraph(state_str, s["small"]))

    ct = Table([[cust_lines]], colWidths=[PAGE_W])
    ct.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), LIGHT_GRAY),
        ("BOX",           (0,0), (-1,-1), 0.5, MID_GRAY),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("RIGHTPADDING",  (0,0), (-1,-1), 8),
    ]))
    story.append(ct)
    story.append(Spacer(1, 4*mm))

    # ── ITEMS TABLE ──
    # TAX INVOICE intra:  S.No | Description | HSN | Qty | Price | Amount | GST% | CGST | SGST | Total
    # TAX INVOICE inter:  S.No | Description | HSN | Qty | Price | Amount | GST% | IGST | Total
    # BILL OF SUPPLY:     S.No | Description | Qty | Price | Amount
    if shop.has_gstin and not bill.is_igst:
        cw = [8*mm, 48*mm, 14*mm, 10*mm, 20*mm, 20*mm, 10*mm, 16*mm, 16*mm, 20*mm]
        hdr = ["S.No", "Description", "HSN", "Qty", "Price", "Amount", "GST%", "CGST", "SGST", "Total"]
    elif shop.has_gstin and bill.is_igst:
        cw = [8*mm, 52*mm, 14*mm, 10*mm, 22*mm, 22*mm, 10*mm, 22*mm, 22*mm]
        hdr = ["S.No", "Description", "HSN", "Qty", "Price", "Amount", "GST%", "IGST", "Total"]
    else:
        cw = [10*mm, 82*mm, 20*mm, 30*mm, 40*mm]
        hdr = ["S.No", "Description", "Qty", "Price", "Amount"]

    rows = [[Paragraph(h, s["th"]) for h in hdr]]

    for idx, item in enumerate(bill.items, 1):
        qty_str = str(int(item.qty)) if item.qty == int(item.qty) else str(item.qty)
        if shop.has_gstin and not bill.is_igst:
            rows.append([
                Paragraph(str(idx),                       s["td"]),
                Paragraph(xml_escape(item.name),          s["td"]),
                Paragraph(xml_escape(str(item.hsn)),      s["td"]),
                Paragraph(qty_str,                        s["td"]),
                Paragraph(f"Rs.{item.price:.2f}",         s["td"]),
                Paragraph(f"Rs.{item.amount:.2f}",        s["td"]),
                Paragraph(f"{item.gst_rate}%",            s["td"]),
                Paragraph(f"Rs.{item.cgst:.2f}",          s["td"]),
                Paragraph(f"Rs.{item.sgst:.2f}",          s["td"]),
                Paragraph(f"Rs.{item.total:.2f}",         s["td_bold"]),
            ])
        elif shop.has_gstin and bill.is_igst:
            rows.append([
                Paragraph(str(idx),                       s["td"]),
                Paragraph(xml_escape(item.name),          s["td"]),
                Paragraph(xml_escape(str(item.hsn)),      s["td"]),
                Paragraph(qty_str,                        s["td"]),
                Paragraph(f"Rs.{item.price:.2f}",         s["td"]),
                Paragraph(f"Rs.{item.amount:.2f}",        s["td"]),
                Paragraph(f"{item.gst_rate}%",            s["td"]),
                Paragraph(f"Rs.{item.igst:.2f}",          s["td"]),
                Paragraph(f"Rs.{item.total:.2f}",         s["td_bold"]),
            ])
        else:
            rows.append([
                Paragraph(str(idx),                       s["td"]),
                Paragraph(xml_escape(item.name),          s["td"]),
                Paragraph(qty_str,                        s["td"]),
                Paragraph(f"Rs.{item.price:.2f}",         s["td"]),
                Paragraph(f"Rs.{item.amount:.2f}",        s["td_bold"]),
            ])

    it = Table(rows, colWidths=cw, repeatRows=1)
    it.setStyle(TableStyle([
        # Header row
        ("BACKGROUND",     (0,0), (-1,0),  BRAND_DARK),
        ("TEXTCOLOR",      (0,0), (-1,0),  WHITE),
        # Body
        ("FONTSIZE",       (0,0), (-1,-1), 8),
        ("TOPPADDING",     (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",  (0,0), (-1,-1), 5),
        ("LEFTPADDING",    (0,0), (-1,-1), 4),
        ("RIGHTPADDING",   (0,0), (-1,-1), 4),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [WHITE, LIGHT_GRAY]),
        # Grid
        ("LINEBELOW",      (0,0), (-1,0),  1, BRAND_BLUE),
        ("LINEBELOW",      (0,1), (-1,-1), 0.25, MID_GRAY),
        ("BOX",            (0,0), (-1,-1), 0.5, MID_GRAY),
        # Alignment
        ("ALIGN",          (0,0), (0,-1),  "CENTER"),  # S.No
        ("ALIGN",          (2,0), (-1,-1), "RIGHT"),   # numeric columns
        ("VALIGN",         (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(it)
    story.append(Spacer(1, 3*mm))

    # ── TOTALS ──
    TL = 112*mm   # spacer
    TM = 40*mm    # label
    TR = 30*mm    # value

    if shop.has_gstin and not bill.is_igst:
        totals_data = [
            ["", Paragraph("Subtotal",       s["total_label"]), Paragraph(f"Rs.{bill.subtotal:.2f}",   s["total_value"])],
            ["", Paragraph("CGST collected", s["total_label"]), Paragraph(f"Rs.{bill.total_cgst:.2f}", s["total_value"])],
            ["", Paragraph("SGST collected", s["total_label"]), Paragraph(f"Rs.{bill.total_sgst:.2f}", s["total_value"])],
            ["", Paragraph("Total GST",      s["total_label"]), Paragraph(f"Rs.{bill.total_gst:.2f}",  s["total_value"])],
        ]
    elif shop.has_gstin and bill.is_igst:
        totals_data = [
            ["", Paragraph("Subtotal",       s["total_label"]), Paragraph(f"Rs.{bill.subtotal:.2f}",   s["total_value"])],
            ["", Paragraph("IGST collected", s["total_label"]), Paragraph(f"Rs.{bill.total_igst:.2f}", s["total_value"])],
            ["", Paragraph("Total GST",      s["total_label"]), Paragraph(f"Rs.{bill.total_gst:.2f}",  s["total_value"])],
        ]
    else:
        totals_data = []

    if totals_data:
        tt = Table(totals_data, colWidths=[TL, TM, TR])
        tt.setStyle(TableStyle([
            ("TOPPADDING",    (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("RIGHTPADDING",  (-1,0),(-1,-1), 4),
            ("LINEBELOW",     (1,-1),(-1,-1), 0.5, MID_GRAY),
        ]))
        story.append(tt)

    # ── GRAND TOTAL ──
    gt = Table([[
        Paragraph(f"GRAND TOTAL", s["grand_label"]),
        Paragraph(f"Rs.{bill.grand_total:.2f}", s["grand_value"]),
    ]], colWidths=[TL + TM, TR])
    gt.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), BRAND_BLUE),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("LEFTPADDING",   (0,0), (-1,-1), 10),
        ("RIGHTPADDING",  (-1,0),(-1,-1), 6),
    ]))
    story.append(gt)
    story.append(Spacer(1, 2*mm))

    # ── AMOUNT IN WORDS ──
    story.append(Paragraph(f"<b>Amount in words:</b>  <i>{bill.in_words}</i>", s["words"]))
    story.append(Spacer(1, 8*mm))

    # ── FOOTER: Terms + Powered by ──
    story.append(HRFlowable(width="100%", thickness=0.5, color=MID_GRAY, spaceAfter=3*mm))

    terms_text = (
        "1. Goods once sold will not be taken back or exchanged.  "
        "2. All disputes subject to local jurisdiction.  "
        "3. E&amp;OE — Errors and omissions excepted."
    )
    story.append(Paragraph("Terms &amp; Conditions:", s["label"]))
    story.append(Spacer(1, 1*mm))
    story.append(Paragraph(terms_text, s["terms"]))
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(f"Powered by {PLATFORM_NAME}", s["powered"]))
    story.append(Paragraph(
        "Computer generated invoice. No physical signature required.",
        s["footer"],
    ))
 
    try:
        doc.build(story)
    except Exception as e:
        log.error(f"PDF build failed: {e}")
        raise RuntimeError(f"PDF generation failed: {e}")

    pdf_bytes = buffer.getvalue()
    size_kb = len(pdf_bytes) / 1024
    log.info(f"Bill generated: {invoice_number} ({size_kb:.1f} KB)")
    if size_kb > 500:
        log.warning(f"Bill is {size_kb:.0f}KB — may be slow on WhatsApp")

    return pdf_bytes, bill
 
 
# ════════════════════════════════════════════════
# UNIT TESTS
# ════════════════════════════════════════════════
 
def run_tests():
    print("\n" + "="*50)
    print("Running BilledUp unit tests...")
    print("="*50)
    passed = 0; failed = 0
 
    def test(name, fn):
        nonlocal passed, failed
        try:
            fn(); print(f"  PASS  {name}"); passed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {e}"); failed += 1
 
    def aeq(a, b):
        if a != b: raise AssertionError(f"Expected '{b}' got '{a}'")
    def atrue(v):
        if not v: raise AssertionError("Expected True")
    def araises(exc, fn):
        try:
            fn(); raise AssertionError(f"{exc.__name__} not raised")
        except exc: pass
 
    test("words: 100",    lambda: aeq(number_to_words(100),    "One Hundred Rupees Only"))
    test("words: 0",      lambda: aeq(number_to_words(0),      "Zero Rupees Only"))
    test("words: 100000", lambda: aeq(number_to_words(100000), "One Lakh Rupees Only"))
    test("words: paise",  lambda: aeq(number_to_words(10.50),  "Ten Rupees and Fifty Paise Only"))
    test("words: large",  lambda: aeq(number_to_words(1000000),"Ten Lakh Rupees Only"))
 
    test("BillItem valid",      lambda: BillItem("phone", 1, 299).validate())
    test("BillItem empty name", lambda: araises(ValueError, lambda: BillItem("", 1, 299).validate()))
    test("BillItem neg price",  lambda: araises(ValueError, lambda: BillItem("phone", 1, -1).validate()))
    test("BillItem zero qty",   lambda: araises(ValueError, lambda: BillItem("phone", 0, 299).validate()))
 
    test("ShopProfile valid GSTIN",
         lambda: ShopProfile("S1","Test Shop","Hyd","36AABCU9603R1ZX","+91 9876543210").validate())
    test("ShopProfile placeholder GSTIN OK",
         lambda: ShopProfile("S1","Test","Hyd",PLACEHOLDER_GSTIN,"+91 9876543210").validate())
    test("ShopProfile no GSTIN OK",
         lambda: ShopProfile("S1","Test","Hyd","","+91 9876543210").validate())
    test("ShopProfile bad GSTIN",
         lambda: araises(ValueError, lambda: ShopProfile("S1","Test","Hyd","INVALID","+91 9876543210").validate()))
    test("ShopProfile empty name",
         lambda: araises(ValueError, lambda: ShopProfile("S1","","Hyd","36AABCU9603R1ZX","+91 9876543210").validate()))
 
    test("ShopProfile has_gstin True",
         lambda: atrue(ShopProfile("S1","T","H","36AABCU9603R1ZX","+91 9876543210").has_gstin))
    test("ShopProfile has_gstin False placeholder",
         lambda: atrue(not ShopProfile("S1","T","H",PLACEHOLDER_GSTIN,"+91 9876543210").has_gstin))
    test("ShopProfile invoice_type TAX",
         lambda: aeq(ShopProfile("S1","T","H","36AABCU9603R1ZX","+91 9876543210").invoice_type, "TAX INVOICE"))
    test("ShopProfile invoice_type BILL OF SUPPLY",
         lambda: aeq(ShopProfile("S1","T","H",PLACEHOLDER_GSTIN,"+91 9876543210").invoice_type, "BILL OF SUPPLY"))
 
    test("CustomerInfo valid",
         lambda: CustomerInfo("Suresh", "+91 9000000000", "Hyd").validate())
    test("CustomerInfo empty",
         lambda: araises(ValueError, lambda: CustomerInfo("").validate()))
    test("CustomerInfo with state",
         lambda: CustomerInfo("Ravi", state="Karnataka", state_code="29").validate())

    # ── IGST logic tests ──
    test("is_intra_state same code",
         lambda: atrue(is_intra_state("36", "36")))
    test("is_intra_state diff code",
         lambda: atrue(not is_intra_state("36", "29")))
    test("is_intra_state empty customer",
         lambda: atrue(is_intra_state("36", "")))
    test("is_intra_state whitespace customer",
         lambda: atrue(is_intra_state("36", "  ")))

    def _test_intra_bill():
        items = [BillItem("phone case", 1, 100)]
        br = calculate_bill(items, gst_client=None, shop_state_code="36", customer_state_code="36")
        atrue(not br.is_igst)
        atrue(br.total_cgst > 0)
        atrue(br.total_sgst > 0)
        aeq(br.total_igst, 0.0)
        aeq(br.total_gst, round(br.total_cgst + br.total_sgst, 2))
    test("calculate_bill intra-state", _test_intra_bill)

    def _test_inter_bill():
        items = [BillItem("phone case", 1, 100)]
        br = calculate_bill(items, gst_client=None, shop_state_code="36", customer_state_code="29")
        atrue(br.is_igst)
        aeq(br.total_cgst, 0.0)
        aeq(br.total_sgst, 0.0)
        atrue(br.total_igst > 0)
        aeq(br.total_gst, br.total_igst)
    test("calculate_bill inter-state", _test_inter_bill)

    def _test_igst_total_matches():
        items = [BillItem("phone case", 1, 100)]
        intra = calculate_bill(items, gst_client=None, shop_state_code="36", customer_state_code="36")
        inter = calculate_bill(items, gst_client=None, shop_state_code="36", customer_state_code="29")
        aeq(intra.grand_total, inter.grand_total)
        aeq(intra.total_gst, inter.total_gst)
    test("IGST total equals CGST+SGST total", _test_igst_total_matches)

    test("Invoice format",
         lambda: atrue(generate_invoice_number("DEMO").startswith("INV-")))
    test("Invoice sequential", lambda: _test_sequential())
 
    print("="*50)
    print(f"Results: {passed} passed, {failed} failed")
    print("="*50)
    return failed == 0
 
def _test_sequential():
    n1 = generate_invoice_number("SEQTEST")
    n2 = generate_invoice_number("SEQTEST")
    s1 = int(n1.split("-")[-1])
    s2 = int(n2.split("-")[-1])
    if s2 != s1 + 1:
        raise AssertionError(f"Not sequential: got {s1} then {s2}")
 
 
if __name__ == "__main__":
    from config import get_anthropic_client
    if not run_tests():
        print("\nFix failing tests before generating bills.")
        exit(1)
 
    print("\nGenerating sample bill...\n")
    client = get_anthropic_client()
 
    shop_with_gstin = ShopProfile(
        shop_id="RAVI", name="Ravi Mobile Accessories",
        address="Shop No. 14, Koti Market, Hyderabad - 500095",
        gstin="36AABCU9603R1ZX", phone="+91 98765 43210",
        state="Telangana", state_code="36", upi="ravi@ybl",
    )
    shop_no_gstin = ShopProfile(
        shop_id="SARI", name="Sri Sai Sarees",
        address="Begum Bazaar, Hyderabad - 500012",
        gstin=PLACEHOLDER_GSTIN, phone="+91 97047 69588",
    )
    customer_intra = CustomerInfo(
        name="Suresh Kumar", phone="+91 90000 11111",
        address="Dilsukhnagar, Hyderabad",
        state="Telangana", state_code="36",
    )
    customer_inter = CustomerInfo(
        name="Amit Sharma", phone="+91 90000 22222",
        address="Jayanagar, Bangalore",
        state="Karnataka", state_code="29",
    )
    items = [
        BillItem("phone case",     qty=1, price=299),
        BillItem("charger 20w",    qty=1, price=499),
        BillItem("earphones",      qty=2, price=199),
    ]
 
    # Generate TAX INVOICE — intra-state (CGST + SGST)
    inv1 = generate_invoice_number(shop_with_gstin.shop_id)
    pdf1, bill1 = generate_pdf_bill(
        shop=shop_with_gstin, customer=customer_intra,
        items=items, invoice_number=inv1, gst_client=client,
    )
    print(f"TAX INVOICE (intra): {inv1} ({len(pdf1)/1024:.1f} KB)  |  Rs.{bill1.grand_total:.2f}")

    # Generate TAX INVOICE — inter-state (IGST)
    inv3 = generate_invoice_number(shop_with_gstin.shop_id)
    pdf3, bill3 = generate_pdf_bill(
        shop=shop_with_gstin, customer=customer_inter,
        items=items, invoice_number=inv3, gst_client=client,
    )
    print(f"TAX INVOICE (inter): {inv3} ({len(pdf3)/1024:.1f} KB)  |  Rs.{bill3.grand_total:.2f}  IGST=Rs.{bill3.total_igst:.2f}")

    # Generate BILL OF SUPPLY
    items2 = [BillItem("saree", qty=1, price=1500), BillItem("dress", qty=1, price=800)]
    inv2   = generate_invoice_number(shop_no_gstin.shop_id)
    pdf2, bill2 = generate_pdf_bill(
        shop=shop_no_gstin, customer=CustomerInfo("Hansika"),
        items=items2, invoice_number=inv2, gst_client=client,
    )
    print(f"BILL OF SUPPLY: {inv2} ({len(pdf2)/1024:.1f} KB)  |  Rs.{bill2.grand_total:.2f}")