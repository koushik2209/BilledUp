"""
bill_generator.py
BillEasy - Production Grade GST Bill Generator
"""
import os, re, json, logging, threading
from datetime import datetime
from dataclasses import dataclass
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("billeasy.generator")

BRAND_BLUE  = colors.HexColor("#1a73e8")
BRAND_DARK  = colors.HexColor("#1a1a2e")
LIGHT_GRAY  = colors.HexColor("#f8f9fa")
MID_GRAY    = colors.HexColor("#dee2e6")
TEXT_GRAY   = colors.HexColor("#6c757d")
WHITE       = colors.white
BLACK       = colors.black
VALID_GST_SLABS = {0, 5, 12, 18, 28}
GSTIN_REGEX = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
PLACEHOLDER_GSTIN = "GSTIN00000000000"

# A4 usable width = 210mm - 14mm left - 14mm right = 182mm
PAGE_W = 182 * mm

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

    def validate(self):
        if not self.name.strip():
            raise ValueError("Shop name cannot be empty")
        if not self.address.strip():
            raise ValueError("Shop address cannot be empty")
        if self.gstin and self.gstin != PLACEHOLDER_GSTIN:
            if not GSTIN_REGEX.match(self.gstin.upper().strip()):
                raise ValueError(f"Invalid GSTIN format: '{self.gstin}'. Expected: 22AAAAA0000A1Z5")
        if len(re.sub(r"\D", "", self.phone)) < 10:
            raise ValueError(f"Invalid phone number: '{self.phone}'")

@dataclass
class CustomerInfo:
    name:    str
    phone:   str = ""
    address: str = ""
    gstin:   str = ""

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
    total_gst:   float
    grand_total: float
    in_words:    str

INVOICE_REGISTRY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "invoice_registry.json"
)

_invoice_lock = threading.Lock()

def _load_registry() -> dict:
    if os.path.exists(INVOICE_REGISTRY_FILE):
        try:
            with open(INVOICE_REGISTRY_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.error(f"Registry read error: {e} - starting fresh")
    return {}

def _save_registry(registry: dict):
    tmp = INVOICE_REGISTRY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(registry, f, indent=2)
    os.replace(tmp, INVOICE_REGISTRY_FILE)

def generate_invoice_number(shop_id: str) -> str:
    if not shop_id.strip():
        raise ValueError("shop_id cannot be empty")
    with _invoice_lock:
        shop_key = shop_id.upper().strip()
        year     = datetime.now().strftime("%Y")
        registry = _load_registry()
        key      = f"{shop_key}_{year}"
        sequence = registry.get(key, 0) + 1
        registry[key] = sequence
        _save_registry(registry)
    invoice_no = f"INV-{year}-{shop_key}-{sequence:05d}"
    log.info(f"Generated invoice: {invoice_no}")
    return invoice_no

def number_to_words(amount: float) -> str:
    ones   = ["","One","Two","Three","Four","Five","Six","Seven","Eight","Nine","Ten",
               "Eleven","Twelve","Thirteen","Fourteen","Fifteen","Sixteen","Seventeen","Eighteen","Nineteen"]
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

def calculate_bill(items: list, gst_client=None) -> BillResult:
    if not items:
        raise ValueError("Cannot generate bill - no items provided")
    from gst_rates import get_gst_rate_smart
    processed = []
    subtotal  = 0.0

    for item in items:
        item.validate()
        name  = item.name.strip()
        qty   = round(float(item.qty), 3)
        price = round(float(item.price), 2)

        try:
            rate_info = get_gst_rate_smart(name, gst_client)
        except Exception as e:
            log.warning(f"GST lookup failed for '{name}': {e} - using default 18%")
            rate_info = {"hsn": "9999", "gst": 18}

        hsn      = rate_info.get("hsn", "9999")
        gst_rate = rate_info.get("gst", 18)

        if gst_rate not in VALID_GST_SLABS:
            log.warning(f"Invalid slab {gst_rate}% for '{name}' - correcting to 18%")
            gst_rate = 18

        amount  = round(qty * price, 2)
        gst_amt = round(amount * gst_rate / 100, 2)
        cgst    = round(gst_amt / 2, 2)
        sgst    = round(gst_amt - cgst, 2)
        total   = round(amount + gst_amt, 2)
        subtotal += amount

        processed.append(BillItem(
            name=name.title(), qty=qty, price=price,
            hsn=hsn, gst_rate=gst_rate, amount=amount,
            cgst=cgst, sgst=sgst, total=total,
        ))

    subtotal    = round(subtotal, 2)
    total_cgst  = round(sum(i.cgst for i in processed), 2)
    total_sgst  = round(sum(i.sgst for i in processed), 2)
    total_gst   = round(total_cgst + total_sgst, 2)
    grand_total = round(subtotal + total_gst, 2)

    log.info(f"Bill - {len(processed)} items | subtotal=Rs.{subtotal} | gst=Rs.{total_gst} | total=Rs.{grand_total}")
    return BillResult(
        items=processed, subtotal=subtotal,
        total_cgst=total_cgst, total_sgst=total_sgst,
        total_gst=total_gst, grand_total=grand_total,
        in_words=number_to_words(grand_total),
    )

def _styles() -> dict:
    return {
        "brand_white":   ParagraphStyle("bw",  fontSize=20, textColor=WHITE,      fontName="Helvetica-Bold", alignment=TA_LEFT),
        "tagline_white": ParagraphStyle("tw",  fontSize=8,  textColor=colors.HexColor("#cce0ff"), fontName="Helvetica", alignment=TA_LEFT),
        "invoice_white": ParagraphStyle("iw",  fontSize=15, textColor=WHITE,      fontName="Helvetica-Bold", alignment=TA_RIGHT),
        "label":         ParagraphStyle("lb",  fontSize=7,  textColor=TEXT_GRAY,  fontName="Helvetica-Bold"),
        "small":         ParagraphStyle("sm",  fontSize=8,  textColor=BLACK,      fontName="Helvetica"),
        "small_bold":    ParagraphStyle("sb",  fontSize=8,  textColor=BLACK,      fontName="Helvetica-Bold"),
        "meta_right":    ParagraphStyle("mr",  fontSize=9,  textColor=BLACK,      fontName="Helvetica-Bold", alignment=TA_RIGHT),
        "gstin":         ParagraphStyle("gs",  fontSize=9,  textColor=BRAND_BLUE, fontName="Helvetica-Bold"),
        "grand_label":   ParagraphStyle("gl",  fontSize=10, textColor=WHITE,      fontName="Helvetica-Bold", alignment=TA_RIGHT),
        "grand_value":   ParagraphStyle("gv",  fontSize=11, textColor=WHITE,      fontName="Helvetica-Bold", alignment=TA_RIGHT),
        "words":         ParagraphStyle("wd",  fontSize=7,  textColor=TEXT_GRAY,  fontName="Helvetica"),
        "sig":           ParagraphStyle("sg",  fontSize=7,  textColor=TEXT_GRAY,  fontName="Helvetica",      alignment=TA_RIGHT),
        "footer":        ParagraphStyle("ft",  fontSize=7,  textColor=TEXT_GRAY,  fontName="Helvetica",      alignment=TA_CENTER),
        "total_right":   ParagraphStyle("trr", fontSize=9,  textColor=TEXT_GRAY,  fontName="Helvetica",      alignment=TA_RIGHT),
        "total_label":   ParagraphStyle("trl", fontSize=9,  textColor=TEXT_GRAY,  fontName="Helvetica"),
    }

def generate_pdf_bill(
    shop:           ShopProfile,
    customer:       CustomerInfo,
    items:          list,
    invoice_number: str,
    gst_client=None,
    save_path:      str | None = None,
) -> tuple[str, BillResult]:
    log.info(f"Generating bill {invoice_number} for {shop.name}")
    shop.validate()
    customer.validate()
    if not items:
        raise ValueError("Items list is empty")
    if not invoice_number.strip():
        raise ValueError("Invoice number cannot be empty")

    bill = calculate_bill(items, gst_client)

    from config import BILLS_FOLDER, PLATFORM_NAME, PLATFORM_TAGLINE, PLATFORM_SUPPORT
    os.makedirs(BILLS_FOLDER, exist_ok=True)

    if not save_path:
        safe      = re.sub(r"[^\w\-]", "_", invoice_number)
        save_path = os.path.join(BILLS_FOLDER, f"{safe}.pdf")

    # 14mm margins each side → 182mm usable
    doc = SimpleDocTemplate(
        save_path, pagesize=A4,
        rightMargin=14*mm, leftMargin=14*mm,
        topMargin=12*mm, bottomMargin=12*mm,
        compress=1,
    )

    s     = _styles()
    story = []
    today = datetime.now().strftime("%d %B %Y")
    HW    = PAGE_W / 2   # half width for two-column layouts

    # ── HEADER ──
    ht = Table([[
        [Paragraph(PLATFORM_NAME, s["brand_white"]),
         Paragraph(PLATFORM_TAGLINE, s["tagline_white"])],
        Paragraph("TAX INVOICE", s["invoice_white"]),
    ]], colWidths=[HW, HW])
    ht.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), BRAND_BLUE),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
        ("LEFTPADDING",   (0,0), (0,-1),  10),
        ("RIGHTPADDING",  (-1,0),(-1,-1), 10),
    ]))
    story.append(ht)
    story.append(Spacer(1, 3*mm))

    # ── INVOICE META ──
    mt = Table([[
        Paragraph(f"<b>Invoice No:</b>  {invoice_number}", s["small_bold"]),
        Paragraph(f"<b>Date:</b>  {today}", s["meta_right"]),
    ]], colWidths=[HW, HW])
    mt.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), LIGHT_GRAY),
        ("BOX",           (0,0), (-1,-1), 0.5, MID_GRAY),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LEFTPADDING",   (0,0), (-1,-1), 10),
        ("RIGHTPADDING",  (0,0), (-1,-1), 10),
    ]))
    story.append(mt)
    story.append(Spacer(1, 3*mm))

    # ── SELLER + BUYER ──
    seller_b = [
        Paragraph("SELLER", s["label"]),
        Spacer(1, 1.5*mm),
        Paragraph(f"<b>{shop.name}</b>",                              s["small_bold"]),
        Paragraph(shop.address,                                        s["small"]),
        Paragraph(f"Phone: {shop.phone}",                             s["small"]),
        Paragraph(f"<b>GSTIN: {shop.gstin.upper()}</b>",              s["gstin"]),
        Paragraph(f"State: {shop.state}  |  Code: {shop.state_code}", s["small"]),
    ]
    buyer_b = [
        Paragraph("BILL TO", s["label"]),
        Spacer(1, 1.5*mm),
        Paragraph(f"<b>{customer.name}</b>", s["small_bold"]),
    ]
    if customer.address:
        buyer_b.append(Paragraph(customer.address, s["small"]))
    if customer.phone:
        buyer_b.append(Paragraph(f"Phone: {customer.phone}", s["small"]))
    if customer.gstin:
        buyer_b.append(Paragraph(f"<b>GSTIN: {customer.gstin.upper()}</b>", s["gstin"]))

    pt = Table([[seller_b, buyer_b]], colWidths=[HW, HW])
    pt.setStyle(TableStyle([
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("BACKGROUND",    (0,0), (-1,-1), LIGHT_GRAY),
        ("BOX",           (0,0), (0,-1),  0.5, MID_GRAY),
        ("BOX",           (1,0), (1,-1),  0.5, MID_GRAY),
        ("TOPPADDING",    (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("LEFTPADDING",   (0,0), (-1,-1), 10),
        ("RIGHTPADDING",  (0,0), (-1,-1), 10),
    ]))
    story.append(pt)
    story.append(Spacer(1, 3*mm))

    # ── ITEMS TABLE ──
    # Total = 182mm exactly
    # No. | Description | HSN  | Qty | Rate  | Amount | GST | CGST  | SGST  | Total
    #  7     52           14     9     20       20       10    15      15      20
    cw = [7*mm, 52*mm, 14*mm, 9*mm, 20*mm, 20*mm, 10*mm, 15*mm, 15*mm, 20*mm]

    hdr = [Paragraph(f"<b>{t}</b>", s["small_bold"]) for t in
           ["No.", "Description", "HSN", "Qty", "Rate", "Amount", "GST", "CGST", "SGST", "Total"]]
    rows = [hdr]

    for idx, item in enumerate(bill.items, 1):
        rows.append([
            Paragraph(str(idx),                        s["small"]),
            Paragraph(item.name,                       s["small"]),
            Paragraph(item.hsn,                        s["small"]),
            Paragraph(str(int(item.qty) if item.qty == int(item.qty) else item.qty), s["small"]),
            Paragraph(f"Rs.{item.price:.2f}",          s["small"]),
            Paragraph(f"Rs.{item.amount:.2f}",         s["small"]),
            Paragraph(f"{item.gst_rate}%",             s["small"]),
            Paragraph(f"Rs.{item.cgst:.2f}",           s["small"]),
            Paragraph(f"Rs.{item.sgst:.2f}",           s["small"]),
            Paragraph(f"<b>Rs.{item.total:.2f}</b>",   s["small_bold"]),
        ])

    it = Table(rows, colWidths=cw, repeatRows=1)
    it.setStyle(TableStyle([
        ("BACKGROUND",     (0,0), (-1,0),  BRAND_DARK),
        ("TEXTCOLOR",      (0,0), (-1,0),  WHITE),
        ("FONTNAME",       (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",       (0,0), (-1,-1), 8),
        ("TOPPADDING",     (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",  (0,0), (-1,-1), 4),
        ("LEFTPADDING",    (0,0), (-1,-1), 3),
        ("RIGHTPADDING",   (0,0), (-1,-1), 3),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [WHITE, LIGHT_GRAY]),
        ("GRID",           (0,0), (-1,-1), 0.3, MID_GRAY),
        ("BOX",            (0,0), (-1,-1), 0.5, MID_GRAY),
        ("ALIGN",          (0,0), (0,-1),  "CENTER"),
        ("ALIGN",          (2,0), (2,-1),  "CENTER"),
        ("ALIGN",          (3,0), (-1,-1), "RIGHT"),
        ("VALIGN",         (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(it)
    story.append(Spacer(1, 3*mm))

    # ── TOTALS — right-aligned, 3-col ──
    # left spacer | label | value
    TL = 110*mm   # left spacer
    TM = 42*mm    # label
    TR = 30*mm    # value
    totals_data = [
        ["", Paragraph("Subtotal",       s["total_label"]), Paragraph(f"Rs.{bill.subtotal:.2f}",   s["total_right"])],
        ["", Paragraph("CGST collected", s["total_label"]), Paragraph(f"Rs.{bill.total_cgst:.2f}", s["total_right"])],
        ["", Paragraph("SGST collected", s["total_label"]), Paragraph(f"Rs.{bill.total_sgst:.2f}", s["total_right"])],
        ["", Paragraph("Total GST",      s["total_label"]), Paragraph(f"Rs.{bill.total_gst:.2f}",  s["total_right"])],
    ]
    tt = Table(totals_data, colWidths=[TL, TM, TR])
    tt.setStyle(TableStyle([
        ("FONTSIZE",      (0,0), (-1,-1), 9),
        ("TOPPADDING",    (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ("RIGHTPADDING",  (-1,0),(-1,-1), 4),
        ("LINEABOVE",     (1,0), (-1,0),  0.5, MID_GRAY),
        ("LINEBELOW",     (1,-1),(-1,-1), 1.0, MID_GRAY),
    ]))
    story.append(tt)

    # ── GRAND TOTAL ──
    GW = 110*mm
    gt = Table([[
        Paragraph(f"<b>Amount in words:</b><br/><i>{bill.in_words}</i>", s["words"]),
        Paragraph("GRAND TOTAL", s["grand_label"]),
        Paragraph(f"Rs.{bill.grand_total:.2f}", s["grand_value"]),
    ]], colWidths=[GW, TM, TR])
    gt.setStyle(TableStyle([
        ("BACKGROUND",    (1,0), (-1,-1), BRAND_BLUE),
        ("BACKGROUND",    (0,0), (0,-1),  LIGHT_GRAY),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("ALIGN",         (1,0), (-1,-1), "RIGHT"),
        ("TOPPADDING",    (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("LEFTPADDING",   (0,0), (0,-1),  10),
        ("RIGHTPADDING",  (-1,0),(-1,-1), 4),
        ("BOX",           (0,0), (-1,-1), 0.5, MID_GRAY),
    ]))
    story.append(gt)
    story.append(Spacer(1, 5*mm))

    # ── UPI ──
    if shop.upi:
        ut = Table([[
            Paragraph(f"<b>Pay via UPI:</b>  {shop.upi}", s["small"]),
            Paragraph("Computer generated invoice.<br/>No physical signature required.", s["sig"]),
        ]], colWidths=[HW, HW])
        ut.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), LIGHT_GRAY),
            ("BOX",           (0,0), (-1,-1), 0.5, MID_GRAY),
            ("TOPPADDING",    (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
            ("LEFTPADDING",   (0,0), (-1,-1), 10),
            ("RIGHTPADDING",  (0,0), (-1,-1), 10),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ]))
        story.append(ut)
        story.append(Spacer(1, 4*mm))

    # ── FOOTER ──
    story.append(HRFlowable(width="100%", thickness=0.5, color=MID_GRAY, spaceAfter=2*mm))
    story.append(Paragraph(
        f"{PLATFORM_NAME}  |  {PLATFORM_TAGLINE}  |  Support: {PLATFORM_SUPPORT}",
        s["footer"]
    ))
    story.append(Paragraph(
        "This invoice was generated automatically by BillEasy. Subject to Hyderabad jurisdiction.",
        s["footer"]
    ))

    try:
        doc.build(story)
    except Exception as e:
        log.error(f"PDF build failed: {e}")
        raise RuntimeError(f"PDF generation failed: {e}")

    abs_path = os.path.abspath(save_path)
    size_kb  = os.path.getsize(abs_path) / 1024
    log.info(f"Bill saved: {abs_path} ({size_kb:.1f} KB)")
    if size_kb > 500:
        log.warning(f"Bill is {size_kb:.0f}KB - may be slow on WhatsApp")
    return abs_path, bill


# ── Unit tests ──
def run_tests():
    print("\n" + "="*50)
    print("Running BillEasy unit tests...")
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
    test("ShopProfile valid",   lambda: ShopProfile("S1","Test Shop","Hyd","36AABCU9603R1ZX","+91 9876543210").validate())
    test("ShopProfile bad GSTIN", lambda: araises(ValueError, lambda: ShopProfile("S1","Test","Hyd","INVALID","+91 9876543210").validate()))
    test("ShopProfile empty name",lambda: araises(ValueError, lambda: ShopProfile("S1","","Hyd","36AABCU9603R1ZX","+91 9876543210").validate()))
    test("CustomerInfo valid",    lambda: CustomerInfo("Suresh", "+91 9000000000", "Hyd").validate())
    test("CustomerInfo empty",    lambda: araises(ValueError, lambda: CustomerInfo("").validate()))
    test("Invoice format",        lambda: atrue(generate_invoice_number("DEMO").startswith("INV-")))
    test("Invoice sequential",    lambda: _test_sequential())

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


# ── Demo ──
if __name__ == "__main__":
    import anthropic
    from config import ANTHROPIC_API_KEY

    if not run_tests():
        print("\nFix failing tests before generating bills.")
        exit(1)

    print("\nGenerating sample bill...\n")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    shop = ShopProfile(
        shop_id="RAVI", name="Ravi Mobile Accessories",
        address="Shop No. 14, Koti Market, Hyderabad - 500095",
        gstin="36AABCU9603R1ZX", phone="+91 98765 43210",
        state="Telangana", state_code="36", upi="ravi@ybl",
    )
    customer = CustomerInfo(
        name="Suresh Kumar", phone="+91 90000 11111",
        address="Dilsukhnagar, Hyderabad",
    )
    items = [
        BillItem("phone case",     qty=1, price=299),
        BillItem("charger 20w",    qty=1, price=499),
        BillItem("earphones",      qty=2, price=199),
        BillItem("tempered glass", qty=1, price=149),
        BillItem("power bank",     qty=1, price=899),
    ]
    invoice_number = generate_invoice_number(shop.shop_id)
    path, bill_result = generate_pdf_bill(
        shop=shop, customer=customer, items=items,
        invoice_number=invoice_number, gst_client=client,
    )
    print(f"\nSuccess! Open your bill here:\n  {path}")
    print(f"Grand total: Rs.{bill_result.grand_total:.2f}")
