"""
api.formatters — Pure Message Formatting Functions
----------------------------------------------------
These functions take data in and return formatted strings.
No database queries, no send calls, no Flask request access.
"""

from config import PLATFORM_NAME


# ── State selection menu (maps menu number → GST state code) ──
_STATE_MENU = [
    ("37", "Andhra Pradesh"),
    ("07", "Delhi"),
    ("24", "Gujarat"),
    ("29", "Karnataka"),
    ("32", "Kerala"),
    ("27", "Maharashtra"),
    ("23", "Madhya Pradesh"),
    ("03", "Punjab"),
    ("08", "Rajasthan"),
    ("33", "Tamil Nadu"),
    ("36", "Telangana"),
    ("09", "Uttar Pradesh"),
    ("19", "West Bengal"),
]


def msg_welcome() -> str:
    return (
        "👋 *Welcome to BilledUp!*\n\n"
        "Generate GST bills in 10 seconds on WhatsApp.\n"
        "No Tally. No computer. No training needed.\n\n"
        "Let's set up your free 10-day trial. 🚀\n\n"
        "First — *what is your shop name?*\n\n"
        "_Example: Ravi Mobile Accessories_"
    )


def msg_ask_address(shop_name: str) -> str:
    return (
        f"✅ Great! *{shop_name}*\n\n"
        f"Now — *what is your shop address?*\n\n"
        f"_Example: Shop No. 14, Koti Market, Hyderabad - 500095_"
    )


def msg_ask_gstin() -> str:
    return (
        "Almost done! 🎉\n\n"
        "*Do you have a GSTIN number?*\n\n"
        "If yes — type it now.\n"
        "Example: _36AABCU9603R1ZX_\n\n"
        "If no — type *skip*\n\n"
        "_You can add GSTIN later anytime._"
    )


def msg_ask_state() -> str:
    lines = ["🏪 Almost there! One last thing.\n"]
    lines.append("Which state is your shop in?\n")
    lines.append("Reply with your state number:\n")
    for i, (_, name) in enumerate(_STATE_MENU, 1):
        lines.append(f"{i}. {name}")
    lines.append(f"{len(_STATE_MENU) + 1}. Other states (type your state name)")
    lines.append("\nThis ensures your GST (CGST/SGST) is calculated correctly.")
    return "\n".join(lines)


def msg_activated(shop_name: str, days: int, api_key: str = "",
                  invoice_type: str = "TAX_INVOICE", state_name: str = "") -> str:
    key_line = f"\n🔑 *Your API Key:*\n`{api_key}`\n_Keep this safe — use it for API access._\n" if api_key else ""
    if invoice_type == "BILL_OF_SUPPLY":
        bill_type_line = (
            "✅ Since you are not GST registered, your bills will be *Bill of Supply* (no GST).\n"
            "_You can add GSTIN later to switch to Tax Invoice._\n"
        )
    else:
        bill_type_line = "✅ Your bills will include GST (*Tax Invoice*).\n"
    state_line = f"📍 Shop state: {state_name} (for GST calculation)\n" if state_name else ""
    return (
        f"🎊 *You are all set, {shop_name}!*\n\n"
        f"{bill_type_line}\n"
        f"{state_line}"
        f"Your *{days}-day free trial* has started.\n"
        f"After trial: just Rs.299/month.\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"*How to generate a bill:*\n\n"
        f"Just type your items and prices:\n\n"
        f"_phone case 299 charger 499 customer Suresh_\n\n"
        f"Your bill will be ready in 10 seconds! ⚡\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"*Commands:*\n"
        f"• *today* — Today's sales summary\n"
        f"• *history* — Last 5 bills\n"
        f"• *gst report* — Monthly GST summary\n"
        f"• *help* — Show this message\n"
        f"{key_line}\n"
        f"Try generating your first bill now! 👆"
    )


def msg_help(shop_name: str, days: int) -> str:
    return (
        f"📖 *BilledUp Help*\n\n"
        f"Shop: {shop_name}\n"
        f"Trial days left: {days}\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"*Generate a bill:*\n"
        f"Type items and prices naturally:\n\n"
        f"_phone case 299 charger 499 customer Suresh_\n"
        f"_rice 50 dal 80 oil 120 customer Ramesh_\n"
        f"_shirt 599 jeans 999 2 customer Priya_\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"*Commands:*\n"
        f"• *today* — Today's summary\n"
        f"• *history* — Last 5 bills\n"
        f"• *gst report* — This month's GST summary\n"
        f"• *gst report last 7 days* — Custom range\n"
        f"• *myitems* — Your saved items & GST rates\n"
        f"• *gst <item> <rate>* — Fix an item's GST rate\n"
        f"• *help* — This message\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"*Support:*\n"
        f"WhatsApp: +91 7981053846\n"
        f"Mon-Sat, 9am to 7pm"
    )


def msg_bill_summary(bill_result, invoice_number: str, customer_name: str,
                     days: int, is_return: bool = False, is_bill_of_supply: bool = False) -> str:
    sign = "-" if is_return else ""
    total_label = "REFUND" if is_return else "TOTAL"

    if is_return:
        doc_label = "Credit Note"
        header = "🔁 *Credit Note Generated!*"
    elif is_bill_of_supply:
        doc_label = "Bill of Supply"
        header = "✅ *Bill of Supply Generated!*"
    else:
        doc_label = "Tax Invoice"
        header = "✅ *Bill Generated!*"

    lines = [
        f"{header}\n",
        f"📋 {doc_label}: *{invoice_number}*",
        f"👤 Customer: *{customer_name}*\n",
        f"*Items:*",
    ]
    for item in bill_result.items:
        qty = int(item.qty) if item.qty == int(item.qty) else item.qty
        if is_bill_of_supply:
            lines.append(f"• {item.name} x{qty} — {sign}Rs.{abs(item.amount):.2f}")
        else:
            lines.append(f"• {item.name} x{qty} — {sign}Rs.{abs(item.total):.2f} ({item.gst_rate}% GST)")

    lines.append(f"\n━━━━━━━━━━━━━━━━━")
    if is_bill_of_supply:
        # No GST breakdown for Bill of Supply
        lines.append(f"*{total_label}: {sign}Rs.{abs(bill_result.subtotal):.2f}*\n")
    else:
        lines.append(f"Subtotal:  {sign}Rs.{abs(bill_result.subtotal):.2f}")
        if bill_result.is_igst:
            lines.append(f"IGST:      {sign}Rs.{abs(bill_result.total_igst):.2f}")
        else:
            lines.append(f"CGST:      {sign}Rs.{abs(bill_result.total_cgst):.2f}")
            lines.append(f"SGST:      {sign}Rs.{abs(bill_result.total_sgst):.2f}")
        lines += [
            f"Total GST: {sign}Rs.{abs(bill_result.total_gst):.2f}",
            f"━━━━━━━━━━━━━━━━━",
            f"*{total_label}: {sign}Rs.{abs(bill_result.grand_total):.2f}*\n",
        ]
    lines += [
        f"_{bill_result.in_words}_\n",
        f"📄 PDF attached below. Forward to customer.",
        f"Trial days left: {days}",
        f"\n_{PLATFORM_NAME}_",
    ]
    return "\n".join(lines)


def msg_trial_expired(shop_name: str) -> str:
    return (
        f"⏰ *{shop_name}, your 10-day free trial has ended.*\n\n"
        f"To continue generating bills — upgrade to BilledUp Standard:\n\n"
        f"*Rs.299/month*\n"
        f"• Unlimited GST bills\n"
        f"• Telugu and Hindi support\n"
        f"• Monthly CA report\n\n"
        f"To upgrade — contact us:\n"
        f"WhatsApp: +91 7981053846\n\n"
        f"_We will activate your account within 5 minutes._"
    )


def msg_invalid_gstin() -> str:
    return (
        f"❌ That GSTIN format looks incorrect.\n\n"
        f"A valid GSTIN has 15 characters like:\n"
        f"_36AABCU9603R1ZX_\n\n"
        f"Please try again — or type *skip* to add GSTIN later."
    )


def msg_state_prompt() -> str:
    """Prompt user to enter customer state."""
    return (
        "📍 *Enter customer's state:*\n\n"
        "Type the state name or GST code:\n\n"
        "_Examples:_\n"
        "• *Karnataka* or *29*\n"
        "• *Maharashtra* or *27*\n"
        "• *Tamil Nadu* or *33*\n"
        "• *Delhi* or *07*\n"
        "• *Gujarat* or *24*\n"
        "• *Kerala* or *32*\n"
        "• *UP* or *09*\n\n"
        "Type *BACK* to keep current state."
    )
