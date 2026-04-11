"""
services.router — Main Message Dispatch
-----------------------------------------
Routes every incoming WhatsApp message based on the shopkeeper's
registration state. Extracted from whatsapp_webhook.py.
"""

import logging
from datetime import datetime

from services.registration import (
    get_registration, upsert_registration,
    is_trial_active, days_left, activate_trial,
    get_shop_id, is_valid_gstin,
    resolve_state, INDIAN_STATES,
    log_message,
)
from services.pending import (
    get_pending_bill, cleanup_expired_pending,
)
from services.billing import (
    send, msg_today_summary, msg_history,
    msg_preview,
    _is_confirmation_command,
    _handle_gst_report, _handle_new_bill,
    _handle_confirmation, _handle_state_selection,
    _handle_myitems, _handle_gst_update,
)
from api.formatters import (
    msg_welcome, msg_ask_address, msg_ask_gstin,
    msg_ask_state, msg_activated, msg_help,
    msg_trial_expired, msg_invalid_gstin,
    _STATE_MENU,
)

log = logging.getLogger("billedup.router")


def handle_message(from_number: str, message: str):
    """
    Main conversation state machine.
    Routes every message based on shopkeeper's current state.
    """
    log_message(from_number, "IN", message)
    msg_lower = message.lower().strip()

    # Load registration
    reg = get_registration(from_number)

    # ── STATE: NEW — never seen this number ──
    if not reg:
        if send(from_number, msg_welcome()):
            upsert_registration(from_number, state="ASKED_NAME")
        return

    state = reg.get("state", "NEW")

    # ── STATE: ASKED_NAME — waiting for shop name ──
    if state == "ASKED_NAME":
        if len(message.strip()) < 3:
            send(from_number,
                "Please enter your shop name.\n"
                "_Example: Ravi Mobile Accessories_"
            )
            return
        shop_name = message.strip().title()
        if send(from_number, msg_ask_address(shop_name)):
            upsert_registration(from_number, shop_name=shop_name, state="ASKED_ADDRESS")
        return

    # ── STATE: ASKED_ADDRESS — waiting for address ──
    if state == "ASKED_ADDRESS":
        if len(message.strip()) < 5:
            send(from_number,
                "Please enter your shop address.\n"
                "_Example: Shop No. 14, Koti Market, Hyderabad - 500095_"
            )
            return
        address = message.strip()
        if send(from_number, msg_ask_gstin()):
            upsert_registration(from_number, address=address, state="ASKED_GSTIN")
        return

    # ── STATE: ASKED_GSTIN — waiting for GSTIN or skip ──
    if state == "ASKED_GSTIN":
        if msg_lower == "skip":
            # Skip GSTIN — move to state selection
            if send(from_number, msg_ask_state()):
                upsert_registration(from_number, gstin="", state="ASKED_STATE")
            return

        gstin = message.strip().upper()
        if not is_valid_gstin(gstin):
            send(from_number, msg_invalid_gstin())
            return

        # Valid GSTIN — save and move to state selection
        if send(from_number, msg_ask_state()):
            upsert_registration(from_number, gstin=gstin, state="ASKED_STATE")
        return

    # ── STATE: ASKED_STATE — waiting for shop state ──
    if state == "ASKED_STATE":
        shop_name = reg.get("shop_name", "Your Shop")
        address   = reg.get("address", "")
        gstin     = reg.get("gstin", "")

        chosen_state = None
        chosen_code  = None

        # Check if user sent a menu number
        if msg_lower.isdigit():
            idx = int(msg_lower)
            if 1 <= idx <= len(_STATE_MENU):
                chosen_code, chosen_state = _STATE_MENU[idx - 1]
            elif idx == len(_STATE_MENU) + 1:
                # "Other states" — ask them to type the name
                send(from_number, "Please type your state name.\n_Example: Goa_")
                return

        # Not a menu number — try fuzzy matching the typed state name
        if not chosen_state:
            # Try resolve_state (exact code, exact name, partial match)
            resolved = resolve_state(message.strip())
            if resolved:
                chosen_state, chosen_code = resolved
            else:
                # Fuzzy match against INDIAN_STATES values
                from rapidfuzz import process as rfprocess, fuzz as rffuzz
                state_names = list(INDIAN_STATES.values())
                match = rfprocess.extractOne(
                    message.strip(), state_names,
                    scorer=rffuzz.WRatio, score_cutoff=60,
                )
                if match:
                    matched_name = match[0]
                    # Find code for matched name
                    for code, name in INDIAN_STATES.items():
                        if name == matched_name:
                            chosen_state, chosen_code = name, code
                            break

                if not chosen_state:
                    # Unrecognized — ask again instead of assigning a fake code.
                    # A bogus state_code would break intra/inter-state (IGST) detection.
                    send(from_number,
                        f"❌ Could not recognize \"{message.strip()}\" as an Indian state.\n\n"
                        + msg_ask_state()
                    )
                    return

        # Activate with the chosen state
        invoice_type = "TAX_INVOICE" if gstin else "BILL_OF_SUPPLY"
        shop_id, api_key = activate_trial(
            from_number, shop_name, address, gstin,
            state_name=chosen_state, state_code=chosen_code,
        )
        d_left = days_left(get_registration(from_number))
        send(from_number, msg_activated(
            shop_name, d_left, api_key,
            invoice_type=invoice_type, state_name=chosen_state,
        ))
        return

    # ── STATE: ACTIVE — registered shopkeeper ──
    if state == "ACTIVE":
        # Check trial expiry
        if not is_trial_active(reg):
            upsert_registration(from_number, state="EXPIRED")
            send(from_number, msg_trial_expired(reg.get("shop_name", "Shop")))
            return

        shop_name = reg.get("shop_name", "Shop")
        shop_id   = get_shop_id(from_number)
        d_left    = days_left(reg)

        # Handle commands
        if msg_lower in ("help", "?"):
            send(from_number, msg_help(shop_name, d_left))
            return

        if msg_lower in ("today", "aaj", "summary"):
            send(from_number, msg_today_summary(shop_id, shop_name, d_left))
            return

        if msg_lower in ("history", "bills", "recent"):
            send(from_number, msg_history(shop_id))
            return

        if msg_lower.startswith("gst report"):
            _handle_gst_report(from_number, msg_lower, shop_id, shop_name)
            return

        if msg_lower in ("myitems", "my items", "items"):
            _handle_myitems(from_number, shop_id)
            return

        if msg_lower.startswith("gst ") and not msg_lower.startswith("gst report"):
            _handle_gst_update(from_number, message, shop_id)
            return

        if msg_lower in ("hi", "hello", "hai", "start"):
            send(from_number, msg_help(shop_name, d_left))
            return

        # ── Cleanup expired pending bills ──
        cleanup_expired_pending()

        # ── Check for pending bill (confirmation mode) ──
        pending = get_pending_bill(from_number)
        if pending:
            if pending.awaiting_state:
                _handle_state_selection(from_number, message, pending, d_left)
            else:
                _handle_confirmation(from_number, msg_lower, message, pending, reg, d_left)
            return

        # ── Catch orphan confirmation commands (no pending bill / expired) ──
        if _is_confirmation_command(msg_lower):
            send(from_number,
                "⏰ Session expired. Please send items again.\n\n"
                "_Example: phone case 299 charger 499 customer Suresh_"
            )
            return

        # ── New bill message → parse and show preview ──
        _handle_new_bill(from_number, message, reg, shop_id, shop_name, d_left)
        return

    # ── STATE: EXPIRED ──
    if state == "EXPIRED":
        send(from_number, msg_trial_expired(reg.get("shop_name", "Shop")))
        return

    # ── Unknown state — reset ──
    upsert_registration(from_number, state="ASKED_NAME")
    send(from_number, msg_welcome())
