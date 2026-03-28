"""
reports.py
BilledUp - GST Report Generation
---------------------------------
Monthly and date-range GST summaries for filing.
PDF export via ReportLab.
"""

import os
import re
import logging
from datetime import datetime, date, timedelta
from dataclasses import dataclass

from sqlalchemy import func

from database import db_session, Bill
from config import BILLS_FOLDER, PLATFORM_NAME

log = logging.getLogger("billedup.reports")


# ════════════════════════════════════════════════
# DATA
# ════════════════════════════════════════════════

@dataclass
class GSTReport:
    shop_id: str
    start_date: date
    end_date: date
    total_invoices: int
    total_sales: float
    total_cgst: float
    total_sgst: float
    total_igst: float
    total_gst: float
    total_returns: float = 0.0
    return_count: int = 0


# ════════════════════════════════════════════════
# QUERY
# ════════════════════════════════════════════════

def get_gst_report(shop_id: str, start_date: date, end_date: date) -> GSTReport:
    """
    Generate GST summary for a shop within a date range.
    Sales and returns are reported separately.
    All amounts rounded to 2 decimal places.
    """
    base_filter = [
        Bill.shop_id == shop_id.upper(),
        func.date(Bill.created_at) >= start_date,
        func.date(Bill.created_at) <= end_date,
    ]

    with db_session() as session:
        # Sales (positive bills)
        sales_row = session.query(
            func.count(Bill.id).label("count"),
            func.coalesce(func.sum(Bill.grand_total), 0).label("total_sales"),
            func.coalesce(func.sum(Bill.total_cgst), 0).label("total_cgst"),
            func.coalesce(func.sum(Bill.total_sgst), 0).label("total_sgst"),
            func.coalesce(func.sum(Bill.total_igst), 0).label("total_igst"),
            func.coalesce(func.sum(Bill.total_gst), 0).label("total_gst"),
        ).filter(*base_filter, Bill.is_return == False).first()

        # Returns (credit notes)
        return_row = session.query(
            func.count(Bill.id).label("count"),
            func.coalesce(func.sum(Bill.grand_total), 0).label("total_returns"),
        ).filter(*base_filter, Bill.is_return == True).first()

    total_invoices = (sales_row.count or 0) + (return_row.count or 0)

    return GSTReport(
        shop_id=shop_id.upper(),
        start_date=start_date,
        end_date=end_date,
        total_invoices=total_invoices,
        total_sales=round(float(sales_row.total_sales or 0), 2),
        total_cgst=round(float(sales_row.total_cgst or 0), 2),
        total_sgst=round(float(sales_row.total_sgst or 0), 2),
        total_igst=round(float(sales_row.total_igst or 0), 2),
        total_gst=round(float(sales_row.total_gst or 0), 2),
        total_returns=round(float(return_row.total_returns or 0), 2),
        return_count=return_row.count or 0,
    )


# ════════════════════════════════════════════════
# DATE RANGE PARSING
# ════════════════════════════════════════════════

MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def parse_report_range(text: str) -> tuple[date, date, str]:
    """
    Parse a date range from user text following 'gst report'.

    Returns (start_date, end_date, label).

    Supported patterns:
        ''                    → current month
        'last 7 days'         → last 7 days
        'last 30 days'        → last 30 days
        'march'               → March of current year
        'march 2026'          → March 2026
        'this month'          → current month
        'last month'          → previous month
    """
    text = text.strip().lower()
    today = date.today()

    # Empty or "this month" → current month
    if not text or text == "this month":
        start = today.replace(day=1)
        label = today.strftime("%B %Y")
        return start, today, label

    # "last month"
    if text == "last month":
        first_of_current = today.replace(day=1)
        end = first_of_current - timedelta(days=1)
        start = end.replace(day=1)
        label = start.strftime("%B %Y")
        return start, end, label

    # "last N days"
    m = re.match(r"last\s+(\d+)\s+days?", text)
    if m:
        n = int(m.group(1))
        n = min(n, 365)  # cap at 1 year
        start = today - timedelta(days=n)
        label = f"Last {n} days"
        return start, today, label

    # "march" or "march 2026"
    m = re.match(r"([a-z]+)(?:\s+(\d{4}))?$", text)
    if m and m.group(1) in MONTH_NAMES:
        month = MONTH_NAMES[m.group(1)]
        year = int(m.group(2)) if m.group(2) else today.year
        start = date(year, month, 1)
        # End of month
        if month == 12:
            end = date(year, 12, 31)
        else:
            end = date(year, month + 1, 1) - timedelta(days=1)
        # Don't go past today
        if end > today:
            end = today
        label = start.strftime("%B %Y")
        return start, end, label

    # Fallback → current month
    start = today.replace(day=1)
    label = today.strftime("%B %Y")
    return start, today, label


# ════════════════════════════════════════════════
# INDIAN NUMBER FORMATTING
# ════════════════════════════════════════════════

def format_indian_number(amount: float) -> str:
    """
    Format a number in Indian lakh/crore style.
    1234567.89 → '12,34,567.89'
    """
    if amount < 0:
        return "-" + format_indian_number(-amount)

    int_part = int(amount)
    dec_part = round(amount - int_part, 2)
    dec_str = f"{dec_part:.2f}"[1:]  # ".89"

    s = str(int_part)
    if len(s) <= 3:
        return s + dec_str

    # Last 3 digits, then groups of 2
    result = s[-3:]
    s = s[:-3]
    while s:
        result = s[-2:] + "," + result
        s = s[:-2]
    return result + dec_str


# ════════════════════════════════════════════════
# WHATSAPP MESSAGE FORMAT
# ════════════════════════════════════════════════

def msg_gst_report(report: GSTReport, label: str) -> str:
    """Format GST report for WhatsApp display."""
    if report.total_invoices == 0:
        return (
            f"📊 *GST Report ({label})*\n\n"
            f"No invoices found for this period.\n\n"
            f"_{PLATFORM_NAME}_"
        )

    lines = [
        f"📊 *GST Report ({label})*\n",
        f"🧾 Total Invoices: {report.total_invoices}",
        f"💰 Total Sales: Rs.{format_indian_number(report.total_sales)}",
    ]
    if report.return_count > 0:
        lines.append(f"🔁 Returns ({report.return_count}): Rs.{format_indian_number(abs(report.total_returns))}")
        net = report.total_sales + report.total_returns  # returns are negative
        lines.append(f"📊 *Net Sales: Rs.{format_indian_number(net)}*")
    lines += [
        "",
        f"🧾 *GST Breakdown:*",
    ]
    if report.total_cgst or report.total_sgst:
        lines.append(f"  • CGST: Rs.{format_indian_number(report.total_cgst)}")
        lines.append(f"  • SGST: Rs.{format_indian_number(report.total_sgst)}")
    if report.total_igst:
        lines.append(f"  • IGST: Rs.{format_indian_number(report.total_igst)}")
    if not (report.total_cgst or report.total_sgst or report.total_igst):
        lines.append(f"  • CGST: Rs.0.00")
        lines.append(f"  • SGST: Rs.0.00")

    lines += [
        f"\n━━━━━━━━━━━━━━━",
        f"💸 *Total GST: Rs.{format_indian_number(report.total_gst)}*",
        f"━━━━━━━━━━━━━━━\n",
        f"📅 {report.start_date.strftime('%d %b %Y')} — {report.end_date.strftime('%d %b %Y')}",
        f"\n_{PLATFORM_NAME}_",
    ]
    return "\n".join(lines)


# ════════════════════════════════════════════════
# PDF EXPORT
# ════════════════════════════════════════════════

# ReportLab colors (matching bill_generator)
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

BRAND_BLUE = colors.HexColor("#1a73e8")
BRAND_DARK = colors.HexColor("#1a1a2e")
LIGHT_GRAY = colors.HexColor("#f8f9fa")
WHITE = colors.white
BLACK = colors.black


def export_gst_report_pdf(report: GSTReport, label: str, shop_name: str = "") -> str:
    """
    Generate a clean PDF summary of the GST report.
    Returns the file path.
    """
    reports_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(reports_folder, exist_ok=True)

    safe_label = re.sub(r"[^a-zA-Z0-9_-]", "_", label)
    filename = f"GST_Report_{report.shop_id}_{safe_label}.pdf"
    filepath = os.path.join(reports_folder, filename)

    doc = SimpleDocTemplate(
        filepath,
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
    log.info(f"GST report PDF saved: {filepath}")
    return filepath
