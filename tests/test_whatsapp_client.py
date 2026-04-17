"""Tests for Meta WhatsApp Cloud API client (no network calls)."""

import os

from whatsapp_client import (
    digits_only,
    normalize_whatsapp_sender,
    parse_meta_webhook_payload,
)


def test_digits_only_strips_formatting():
    assert digits_only("whatsapp:+919876543210") == "919876543210"
    assert digits_only("+1 415 555 1212") == "14155551212"


def test_normalize_whatsapp_sender_meta_format():
    assert normalize_whatsapp_sender("919876543210") == "whatsapp:+919876543210"


def test_parse_meta_webhook_payload_extracts_text():
    body = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "919876543210",
                                    "type": "text",
                                    "text": {"body": "hello bill"},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    msgs = parse_meta_webhook_payload(body)
    assert len(msgs) == 1
    assert msgs[0]["text"] == "hello bill"
    assert msgs[0]["from"] == "whatsapp:+919876543210"


def test_parse_meta_webhook_skips_non_text():
    body = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"from": "91x", "type": "image", "image": {"id": "1"}}
                            ]
                        }
                    }
                ]
            }
        ]
    }
    assert parse_meta_webhook_payload(body) == []


def test_meta_webhook_get_verification():
    """GET /webhook returns hub.challenge when verify_token matches VERIFY_TOKEN."""
    from whatsapp_webhook import app

    client = app.test_client()
    resp = client.get(
        "/webhook",
        query_string={
            "hub.mode": "subscribe",
            "hub.verify_token": os.environ.get("VERIFY_TOKEN", "test-verify-token"),
            "hub.challenge": "CHALLENGE_ACCEPTED",
        },
    )
    assert resp.status_code == 200
    assert resp.data.decode() == "CHALLENGE_ACCEPTED"

    resp_bad = client.get(
        "/webhook",
        query_string={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "x",
        },
    )
    assert resp_bad.status_code == 403


# ── Webhook signature verification tests ──

import hmac
import hashlib
import json


def _make_webhook_body():
    """Minimal valid Meta webhook payload."""
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"messages": []}}]}],
    }).encode()


def _sign(secret: str, body: bytes) -> str:
    """Compute the X-Hub-Signature-256 header value."""
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256,
    ).hexdigest()


def test_webhook_post_missing_signature_returns_403(monkeypatch):
    """POST /webhook without X-Hub-Signature-256 must be rejected."""
    import whatsapp_webhook
    monkeypatch.setattr(whatsapp_webhook, "WHATSAPP_APP_SECRET", "test-secret-key")

    client = whatsapp_webhook.app.test_client()
    body = _make_webhook_body()
    resp = client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        # No X-Hub-Signature-256 header
    )
    assert resp.status_code == 403


def test_webhook_post_wrong_signature_returns_403(monkeypatch):
    """POST /webhook with an incorrect HMAC signature must be rejected."""
    import whatsapp_webhook
    monkeypatch.setattr(whatsapp_webhook, "WHATSAPP_APP_SECRET", "test-secret-key")

    client = whatsapp_webhook.app.test_client()
    body = _make_webhook_body()
    resp = client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature-256": "sha256=000000deadbeef"},
    )
    assert resp.status_code == 403


def test_webhook_post_valid_signature_accepted(monkeypatch):
    """POST /webhook with a correct HMAC signature must be accepted (HTTP 200)."""
    import whatsapp_webhook
    secret = "test-secret-key"
    monkeypatch.setattr(whatsapp_webhook, "WHATSAPP_APP_SECRET", secret)

    client = whatsapp_webhook.app.test_client()
    body = _make_webhook_body()
    sig = _sign(secret, body)
    resp = client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature-256": sig},
    )
    assert resp.status_code == 200


def test_webhook_post_no_secret_configured_returns_403(monkeypatch):
    """POST /webhook when WHATSAPP_APP_SECRET is unset must be rejected, not bypassed."""
    import whatsapp_webhook
    monkeypatch.setattr(whatsapp_webhook, "WHATSAPP_APP_SECRET", "")

    client = whatsapp_webhook.app.test_client()
    body = _make_webhook_body()
    resp = client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature-256": "sha256=anything"},
    )
    assert resp.status_code == 403
