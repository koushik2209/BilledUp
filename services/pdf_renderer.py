"""
services.pdf_renderer — ReportLab PDF Generation
---------------------------------------------------
Generates GST invoice PDFs in memory (returns bytes, no filesystem).
Handles TAX INVOICE, BILL OF SUPPLY, and CREDIT NOTE layouts.
"""

import logging
from datetime import datetime
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

import re

from core.entities import BillItem, BillResult
from core.billing import calculate_bill
from core.reports import GSTReport, format_indian_number

log = logging.getLogger("billedup.generator")

BRAND_BLUE  = colors.HexColor("#1a73e8")
BRAND_DARK  = colors.HexColor("#1a1a2e")
LIGHT_GRAY  = colors.HexColor("#f8f9fa")
MID_GRAY    = colors.HexColor("#dee2e6")
TEXT_GRAY   = colors.HexColor("#6c757d")
WHITE       = colors.white
BLACK       = colors.black

PAGE_W = 182 * mm   # A4 usable width after 14mm margins


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
    shop,
    customer,
    items:          list,
    invoice_number: str,
    gst_client=None,
    is_return:      bool = False,
    is_inclusive:   bool = False,
    bill_discount_type:  str   = "none",
    bill_discount_value: float = 0.0,
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
        is_inclusive=is_inclusive,
        bill_discount_type=bill_discount_type,
        bill_discount_value=bill_discount_value,
    )

    # For credit notes, negate all monetary values in the result
    if is_return:
        bill = BillResult(
            items=[BillItem(
                name=i.name, qty=i.qty, price=-i.price, hsn=i.hsn,
                gst_rate=i.gst_rate, amount=-i.amount, cgst=-i.cgst,
                sgst=-i.sgst, igst=-i.igst, total=-i.total,
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
# GST REPORT PDF
# ════════════════════════════════════════════════

def export_gst_report_pdf(report: GSTReport, label: str, shop_name: str = "") -> str:
    """
    Generate a clean PDF summary of the GST report.
    Returns (pdf_bytes, filename).
    """
    from io import BytesIO
    from config import PLATFORM_NAME

    safe_label = re.sub(r"[^a-zA-Z0-9_-]", "_", label)
    filename = f"GST_Report_{report.shop_id}_{safe_label}.pdf"

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )

    # Styles
    title_style = ParagraphStyle(
        "ReportTitle", fontSize=18, textColor=BRAND_DARK,
        alignment=TA_CENTER, spaceAfter=6,
        fontName="Helvetica-Bold",
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle", fontSize=11, textColor=colors.HexColor("#6c757d"),
        alignment=TA_CENTER, spaceAfter=4,
    )
    label_style = ParagraphStyle(
        "Label", fontSize=10, textColor=BRAND_DARK,
        fontName="Helvetica-Bold",
    )
    value_style = ParagraphStyle(
        "Value", fontSize=10, textColor=BRAND_DARK,
        alignment=TA_RIGHT,
    )

    elements = []

    # Header
    elements.append(Paragraph(f"{PLATFORM_NAME}", title_style))
    if shop_name:
        elements.append(Paragraph(shop_name, subtitle_style))
    elements.append(Spacer(1, 4 * mm))
    elements.append(Paragraph(f"GST Report — {label}", ParagraphStyle(
        "PeriodTitle", fontSize=14, textColor=BRAND_BLUE,
        alignment=TA_CENTER, fontName="Helvetica-Bold",
    )))
    elements.append(Paragraph(
        f"{report.start_date.strftime('%d %b %Y')} to {report.end_date.strftime('%d %b %Y')}",
        subtitle_style,
    ))
    elements.append(Spacer(1, 8 * mm))

    # Summary table
    page_w = 170 * mm  # usable width
    col_w = [page_w * 0.6, page_w * 0.4]

    data = [
        [Paragraph("Description", label_style),
         Paragraph("Amount (Rs.)", ParagraphStyle(
             "AmtHeader", fontSize=10, textColor=BRAND_DARK,
             fontName="Helvetica-Bold", alignment=TA_RIGHT,
         ))],
        [Paragraph("Total Invoices", label_style),
         Paragraph(str(report.total_invoices), value_style)],
        [Paragraph("Total Sales", label_style),
         Paragraph(format_indian_number(report.total_sales), value_style)],
        ["", ""],  # spacer row
        [Paragraph("GST Breakdown", ParagraphStyle(
             "SectionHead", fontSize=11, textColor=BRAND_BLUE,
             fontName="Helvetica-Bold",
         )), ""],
        [Paragraph("  CGST", label_style),
         Paragraph(format_indian_number(report.total_cgst), value_style)],
        [Paragraph("  SGST", label_style),
         Paragraph(format_indian_number(report.total_sgst), value_style)],
        [Paragraph("  IGST", label_style),
         Paragraph(format_indian_number(report.total_igst), value_style)],
        ["", ""],  # spacer row
        [Paragraph("Total GST", ParagraphStyle(
             "TotalLabel", fontSize=12, textColor=BRAND_DARK,
             fontName="Helvetica-Bold",
         )),
         Paragraph(format_indian_number(report.total_gst), ParagraphStyle(
             "TotalValue", fontSize=12, textColor=BRAND_DARK,
             fontName="Helvetica-Bold", alignment=TA_RIGHT,
         ))],
    ]

    table = Table(data, colWidths=col_w)
    table.setStyle(TableStyle([
        # Header row
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        # Alternating rows
        ("BACKGROUND", (0, 1), (-1, 2), LIGHT_GRAY),
        ("BACKGROUND", (0, 4), (-1, 4), colors.HexColor("#e8f0fe")),
        # Total row
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e8f0fe")),
        ("LINEABOVE", (0, -1), (-1, -1), 1.5, BRAND_BLUE),
        # Grid
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dee2e6")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(table)

    # Footer
    elements.append(Spacer(1, 10 * mm))
    elements.append(Paragraph(
        f"Generated by {PLATFORM_NAME} on {datetime.utcnow().strftime('%d %b %Y, %I:%M %p')} UTC",
        ParagraphStyle("Footer", fontSize=8, textColor=colors.HexColor("#999999"),
                        alignment=TA_CENTER),
    ))
    elements.append(Paragraph(
        "This is a system-generated summary. Verify with your CA before filing.",
        ParagraphStyle("Disclaimer", fontSize=7, textColor=colors.HexColor("#bbbbbb"),
                        alignment=TA_CENTER, spaceBefore=2),
    ))

    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    log.info(f"GST report PDF generated: {filename} ({len(pdf_bytes) / 1024:.1f} KB)")
    return pdf_bytes, filename
