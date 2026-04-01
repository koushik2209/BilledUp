"""
whatsapp_client.py
Meta WhatsApp Cloud API — send messages (Graph API).
https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from config import WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID

log = logging.getLogger("billedup.whatsapp_client")

GRAPH_API_VERSION = "v22.0"
MAX_RETRIES = 3
RETRY_DELAYS = (1, 2, 4)


def _graph_messages_url() -> str:
    if not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID is not set")
    return (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
    )


def _headers() -> dict[str, str]:
    if not WHATSAPP_ACCESS_TOKEN:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is not set")
    return {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def digits_only(phone: str) -> str:
    """E.164 digits only (no +) for Graph API `to` field."""
    return re.sub(r"\D", "", phone or "")


def normalize_whatsapp_sender(phone_raw: str) -> str:
    """
    Normalize Meta `from` (digits) to internal form used by the app:
    whatsapp:+<countrycode><number>
    """
    d = digits_only(phone_raw)
    if not d:
        return phone_raw
    return f"whatsapp:+{d}"


def _post_payload(payload: dict[str, Any]) -> dict[str, Any]:
    url = _graph_messages_url()
    data = json.dumps(payload).encode("utf-8")
    last_body = ""
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            time.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
        try:
            req = urllib.request.Request(
                url, data=data, headers=_headers(), method="POST"
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode()
                out = json.loads(raw) if raw else {}
                if "error" in out:
                    log.error("WhatsApp Graph API error: %s", out["error"])
                return out
        except urllib.error.HTTPError as e:
            last_body = e.read().decode() if e.fp else ""
            log.warning(
                "WhatsApp HTTP %s (attempt %s): %s",
                e.code,
                attempt + 1,
                last_body[:500],
            )
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                continue
            try:
                err_json = json.loads(last_body) if last_body else {}
            except json.JSONDecodeError:
                err_json = {"raw": last_body}
            log.error("WhatsApp send failed: %s", err_json)
            return {"error": err_json, "http_status": e.code}
        except Exception as e:
            log.exception("WhatsApp request failed: %s", e)
            return {"error": str(e)}
    return {"error": last_body or "max retries"}


def send_text_message(to: str, message: str) -> dict[str, Any]:
    """
    Send a plain text WhatsApp message.
    `to` may be digits, +digits, or whatsapp:+...
    """
    to_digits = digits_only(to)
    if not to_digits:
        log.error("send_text_message: empty recipient")
        return {"error": "empty recipient"}

    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_digits,
        "type": "text",
        "text": {"preview_url": False, "body": message[:4096]},
    }
    return _post_payload(payload)


def send_template_message(
    to: str,
    template_name: str,
    variables: list[str] | None = None,
    language_code: str = "en_US",
) -> dict[str, Any]:
    """
    Send an approved template. `variables` map to body component parameters in order.
    """
    to_digits = digits_only(to)
    if not to_digits:
        return {"error": "empty recipient"}

    components: list[dict[str, Any]] = []
    if variables:
        components.append(
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": str(v)} for v in variables
                ],
            }
        )

    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_digits,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            **({"components": components} if components else {}),
        },
    }
    return _post_payload(payload)


def send_document_by_link(
    to: str,
    document_url: str,
    filename: str,
    caption: str = "",
) -> dict[str, Any]:
    """Send a PDF/document from a publicly reachable HTTPS URL."""
    to_digits = digits_only(to)
    if not to_digits:
        return {"error": "empty recipient"}

    doc: dict[str, Any] = {
        "link": document_url,
        "filename": filename,
    }
    if caption:
        doc["caption"] = caption[:1024]

    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_digits,
        "type": "document",
        "document": doc,
    }
    return _post_payload(payload)


def parse_meta_webhook_payload(body: dict[str, Any]) -> list[dict[str, str]]:
    """
    Parse Meta webhook JSON into a list of:
    {"from": "whatsapp:+<digits>", "text": "<message>"}
    """
    out: list[dict[str, str]] = []
    try:
        for entry in body.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                for msg in value.get("messages") or []:
                    if msg.get("type") != "text":
                        continue
                    text_obj = msg.get("text") or {}
                    body_text = (text_obj.get("body") or "").strip()
                    from_raw = msg.get("from") or ""
                    if not from_raw or not body_text:
                        continue
                    out.append(
                        {
                            "from": normalize_whatsapp_sender(from_raw),
                            "text": body_text,
                        }
                    )
    except Exception as e:
        log.warning("parse_meta_webhook_payload: %s", e)
    return out


def send_test_message() -> dict[str, Any]:
    """
    Send a single test string to WHATSAPP_TEST_TO (digits or whatsapp:+...).
    """
    to = os.getenv("WHATSAPP_TEST_TO", "").strip()
    if not to:
        raise RuntimeError(
            "Set WHATSAPP_TEST_TO in .env to a recipient phone number to run send_test_message()"
        )
    return send_text_message(
        to,
        "BilledUp — Meta WhatsApp Cloud API test message OK.",
    )
