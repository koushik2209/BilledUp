"""
services.router — Main Message Dispatch (Hardened)
-------------------------------------------------
Production-ready router with error handling, validation,
and reliability improvements.
"""

import logging
import re
from datetime import datetime

from services.registration import (
    get_registration, upsert_registration,
    is_trial_active, days_left, activate_trial,
    get_shop_id, is_valid_gstin,
    resolve_state, INDIAN_STATES,
    log_message,
)
from services.pending import cleanup_expired_pending
from services.billing import send
from api.formatters import (
    msg_welcome, msg_ask_address, msg_ask_gstin,
    msg_ask_state, msg_activated, msg_help,
    msg_trial_expired, msg_invalid_gstin,
    _STATE_MENU,
)

log = logging.getLogger("billedup.router")


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _clean_input(text: str) -> str:
    try:
        return " ".join(text.strip().split())
    except Exception:
        return text or ""


def _safe_send(phone: str, message: str) -> bool:
    try:
        ok = send(phone, message)
        if ok:
            log_message(phone, "OUT", message)
        return ok
    except Exception as e:
        log.error(f"Send failed to {phone}: {e}")
        return False


def _is_skip_gstin(text: str) -> bool:
    t = text.lower()
    return any(x in t for x in ["skip", "no gst", "dont have", "don't have", "none"])


def _fallback_reply() -> str:
    return (
        "I didn’t understand that. 😅\n"
        "Try: *shirt 500 pant 800*\n"
        "Or type *help*."
    )


# ─────────────────────────────────────────────
# Main Handler
# ─────────────────────────────────────────────

def handle_message(from_number: str, message: str):
    try:
        message = _clean_input(message)
        msg_lower = message.lower()

        log_message(from_number, "IN", message)

        reg = get_registration(from_number)

        # ── NEW USER ──
        if not reg:
            if _safe_send(from_number, msg_welcome()):
                upsert_registration(from_number, state="ASKED_NAME")
            return

        state = reg.get("state", "NEW")

        # ── ASKED_NAME ──
        if state == "ASKED_NAME":
            if len(message) < 3 or message.isdigit():
                _safe_send(
                    from_number,
                    "Please enter your shop name.\n"
                    "_Example: Ravi Mobile Accessories_"
                )
                return

            shop_name = message.title()
            if _safe_send(from_number, msg_ask_address(shop_name)):
                upsert_registration(from_number, shop_name=shop_name, state="ASKED_ADDRESS")
            return

        # ── ASKED_ADDRESS ──
        if state == "ASKED_ADDRESS":
            if len(message) < 5:
                _safe_send(
                    from_number,
                    "Please enter your shop address.\n"
                    "_Example: Shop No. 14, Koti Market, Hyderabad - 500095_"
                )
                return

            if _safe_send(from_number, msg_ask_gstin()):
                upsert_registration(from_number, address=message, state="ASKED_GSTIN")
            return

        # ── ASKED_GSTIN ──
        if state == "ASKED_GSTIN":

            if _is_skip_gstin(message):
                if _safe_send(from_number, msg_ask_state()):
                    upsert_registration(from_number, gstin="", state="ASKED_STATE")
                return

            gstin = message.upper()

            if not is_valid_gstin(gstin):
                _safe_send(from_number, msg_invalid_gstin())
                return

            if _safe_send(from_number, msg_ask_state()):
                upsert_registration(from_number, gstin=gstin, state="ASKED_STATE")
            return

        # ── ASKED_STATE ──
        if state == "ASKED_STATE":
            shop_name = reg.get("shop_name", "Your Shop")
            address   = reg.get("address", "")
            gstin     = reg.get("gstin", "")

            chosen_state = None
            chosen_code = None

            # Menu number
            if msg_lower.isdigit():
                idx = int(msg_lower)
                if 1 <= idx <= len(_STATE_MENU):
                    chosen_code, chosen_state = _STATE_MENU[idx - 1]
                elif idx == len(_STATE_MENU) + 1:
                    _safe_send(from_number, "Please type your state name.\n_Example: Goa_")
                    return

            # Text match
            if not chosen_state:
                resolved = resolve_state(message)
                if resolved:
                    chosen_state, chosen_code = resolved
                else:
                    from rapidfuzz import process as rfprocess, fuzz as rffuzz
                    match = rfprocess.extractOne(
                        message,
                        list(INDIAN_STATES.values()),
                        scorer=rffuzz.WRatio,
                        score_cutoff=60,
                    )
                    if match:
                        matched_name = match[0]
                        for code, name in INDIAN_STATES.items():
                            if name == matched_name:
                                chosen_state, chosen_code = name, code
                                break

            if not chosen_state:
                _safe_send(
                    from_number,
                    f"❌ Could not recognize \"{message}\".\n\n" + msg_ask_state()
                )
                return

            invoice_type = "TAX_INVOICE" if gstin else "BILL_OF_SUPPLY"

            shop_id, api_key = activate_trial(
                from_number, shop_name, address, gstin,
                state_name=chosen_state, state_code=chosen_code,
            )

            d_left = days_left(get_registration(from_number))

            _safe_send(
                from_number,
                msg_activated(
                    shop_name, d_left, api_key,
                    invoice_type=invoice_type,
                    state_name=chosen_state,
                )
            )
            return

        # ── ACTIVE ──
        if state == "ACTIVE":

            if not is_trial_active(reg):
                upsert_registration(from_number, state="EXPIRED")
                _safe_send(from_number, msg_trial_expired(reg.get("shop_name", "Shop")))
                return

            # Cleanup (safe)
            try:
                cleanup_expired_pending()
            except Exception as e:
                log.warning(f"Pending cleanup failed: {e}")

            # Conversational bot
            try:
                from conversation.manager import handle_message as conv_handle
                reply = conv_handle(from_number, message)

                if not reply:
                    reply = _fallback_reply()

            except Exception as e:
                log.error(f"Conversation error: {e}")
                reply = _fallback_reply()

            _safe_send(from_number, reply)
            return

        # ── EXPIRED ──
        if state == "EXPIRED":
            _safe_send(from_number, msg_trial_expired(reg.get("shop_name", "Shop")))
            return

        # ── UNKNOWN STATE ──
        upsert_registration(from_number, state="ASKED_NAME")
        _safe_send(from_number, msg_welcome())

    except Exception as e:
        log.exception(f"Router crash for {from_number}: {e}")
        _safe_send(
            from_number,
            "Something went wrong. 😅\nPlease try again or type *help*."
        )