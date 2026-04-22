"""Tests for POST /api/cron/daily-summary endpoint."""
import os
import pytest
import main


@pytest.fixture()
def client():
    main.init_database()
    from whatsapp_webhook import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_cron_endpoint_returns_401_without_auth(client):
    resp = client.post("/api/cron/daily-summary")
    assert resp.status_code == 401


def test_cron_endpoint_returns_401_wrong_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "correct")
    resp = client.post(
        "/api/cron/daily-summary",
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


def test_cron_endpoint_returns_401_when_no_secret_configured(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "")
    resp = client.post(
        "/api/cron/daily-summary",
        headers={"Authorization": "Bearer anything"},
    )
    assert resp.status_code == 401


def test_cron_endpoint_returns_200_with_correct_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "test-secret-xyz")
    resp = client.post(
        "/api/cron/daily-summary",
        headers={"Authorization": "Bearer test-secret-xyz"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert "sent" in data
    assert "skipped" in data
    assert "failed" in data


def test_cron_endpoint_skips_already_sent_shop(client, monkeypatch):
    """Shop with last_summary_sent_at = today (IST) must be skipped."""
    import pytz
    from datetime import datetime, timedelta
    from db.session import db_session
    from db.models import Shop, Registration

    main.init_database()

    IST = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(IST)

    with db_session() as session:
        shop = Shop(
            shop_id="S77777777",
            name="Skip Shop",
            address="Addr",
            gstin="36AABCU9603R1ZX",
            phone="917777777777",
            state="Telangana",
            state_code="36",
            last_summary_sent_at=datetime.now(IST),  # already sent today (IST-aware)
        )
        session.add(shop)
        reg = Registration(
            phone="917777777777",
            shop_name="Skip Shop",
            address="Addr",
            gstin="36AABCU9603R1ZX",
            state="ACTIVE",
            active=True,
            trial_start=datetime.utcnow(),
            trial_end=datetime.utcnow() + timedelta(days=10),
        )
        session.add(reg)

    monkeypatch.setenv("CRON_SECRET", "test-secret-xyz")
    resp = client.post(
        "/api/cron/daily-summary",
        headers={"Authorization": "Bearer test-secret-xyz"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sent"] == 0
    assert data["skipped"] >= 1
