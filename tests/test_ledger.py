import datetime
import json

import pytest

import models
from routers import ledger
from payment_utils import anonymize_email, anonymize_name


def add_external_entry(db, **overrides):
    now = datetime.datetime(2026, 5, 24, 8, 30, tzinfo=datetime.timezone.utc)
    values = {
        "id": overrides.get("id", ledger.external_ledger_id(overrides.get("provider", "paypal"), overrides.get("external_id", "TX-1"))),
        "provider": "paypal",
        "provider_account": None,
        "external_id": "TX-1",
        "entry_type": "revenue",
        "category": "payment",
        "amount": 19.01,
        "gross_amount": 20.00,
        "fee_amount": -0.99,
        "net_amount": 19.01,
        "currency": "USD",
        "description": "Pro Subscription - H*** A***",
        "public_description": "Pro Subscription - H*** A***",
        "status": "posted",
        "source": ledger.PAYPAL_SOURCE,
        "posted_at": now,
        "period_start": None,
        "period_end": None,
        "synced_at": now,
        "raw_payload": {},
    }
    values.update(overrides)
    entry = models.ExternalLedgerEntry(**values)
    db.add(entry)
    return entry


def test_get_public_ledger_endpoint_groups_paypal_and_aws(client, db):
    now = datetime.datetime(2026, 5, 24, 8, 30, tzinfo=datetime.timezone.utc)
    add_external_entry(db)
    add_external_entry(
        db,
        id=ledger.external_ledger_id("aws", "2026-05-24:UnblendedCost"),
        provider="aws",
        external_id="2026-05-24:UnblendedCost",
        entry_type="expense",
        category="cloud",
        amount=-3.42,
        gross_amount=None,
        fee_amount=None,
        net_amount=-3.42,
        description="AWS Cloud Services",
        public_description="AWS Cloud Services",
        source=ledger.AWS_SOURCE,
        posted_at=datetime.datetime(2026, 5, 24, 0, 0, tzinfo=datetime.timezone.utc),
        period_start=datetime.datetime(2026, 5, 24, 0, 0, tzinfo=datetime.timezone.utc),
        period_end=datetime.datetime(2026, 5, 25, 0, 0, tzinfo=datetime.timezone.utc),
    )
    db.add(
        models.LedgerSyncRun(
            provider="aws",
            source=ledger.AWS_SOURCE,
            status="ok",
            started_at=now,
            finished_at=now,
            records_inserted=1,
            records_updated=0,
        )
    )
    db.commit()

    response = client.get("/skin/api/public/ledger")
    assert response.status_code == 200
    data = response.json()

    assert set(data.keys()) == {"entries", "summaries", "sync"}
    assert data["summaries"]["paypal"]["total"] == "+$19.01"
    assert data["summaries"]["aws"]["total"] == "-$3.42"
    assert data["summaries"]["all"]["total"] == "+$15.59"
    assert data["sync"]["mode"] == "daily_api_snapshot"
    assert data["sync"]["aws"]["last_synced_at"] == "2026-05-24T08:30:00Z"

    providers = {item["provider"] for item in data["entries"]}
    assert providers == {"paypal", "aws"}
    for item in data["entries"]:
        assert "id" in item
        assert "date" in item
        assert "type" in item
        assert "provider" in item
        assert "source" in item
        assert "desc" in item
        assert "amount" in item
        assert "amount_value" in item
        assert item["type"] in ["revenue", "expense"]


def test_legacy_open_ledger_endpoint_still_works(client):
    response = client.get("/skin/api/open/ledger")
    assert response.status_code == 200


def test_ledger_sync_once_pulls_paypal_and_aws(monkeypatch):
    calls = []
    monkeypatch.setattr(ledger, "sync_paypal_transactions_job", lambda: calls.append("paypal"))
    monkeypatch.setattr(ledger, "sync_aws_billing_job", lambda: calls.append("aws"))

    ledger.sync_ledger_sources_once()

    assert calls == ["paypal", "aws"]


def test_public_ledger_ignores_local_paid_orders(client, db):
    db.add(
        models.Order(
            id="LOCALPAIDORDER01",
            user_id="USER000000000001",
            order_type="print",
            status="paid",
            total_price=45.00,
            paid_at=datetime.datetime(2026, 5, 24, 9, 0, tzinfo=datetime.timezone.utc),
            paypal_order_id="PAYPAL-ORDER-LOCAL",
        )
    )
    db.commit()

    response = client.get("/skin/api/public/ledger")
    assert response.status_code == 200
    data = response.json()

    assert data["entries"] == []
    assert data["summaries"]["paypal"]["count"] == 0
    assert data["summaries"]["all"]["total"] == "+$0.00"


def test_public_ledger_only_shows_external_api_providers(client, db):
    add_external_entry(
        db,
        id=ledger.external_ledger_id("platform", "MANUAL-1"),
        provider="platform",
        external_id="MANUAL-1",
        entry_type="expense",
        category="operations",
        amount=-1240.00,
        gross_amount=None,
        fee_amount=None,
        net_amount=-1240.00,
        description="AWS Cloud Services (Inference Cluster)",
        public_description="AWS Cloud Services (Inference Cluster)",
        source="Manual expense ledger",
    )
    db.commit()

    response = client.get("/skin/api/public/ledger")
    assert response.status_code == 200
    data = response.json()

    assert data["entries"] == []
    assert data["summaries"]["all"]["count"] == 0


def test_paypal_sync_writes_unified_external_ledger(monkeypatch, db):
    payload = {
        "transaction_details": [
            {
                "transaction_info": {
                    "transaction_id": "PAYPAL-API-1",
                    "transaction_event_code": "T0000",
                    "transaction_status": "S",
                    "transaction_subject": "Pro Subscription",
                    "transaction_amount": {"value": "20.00", "currency_code": "USD"},
                    "fee_amount": {"value": "-0.99", "currency_code": "USD"},
                    "transaction_initiation_date": "2026-05-24T08:30:00Z",
                },
                "payer_info": {
                    "payer_email": "ha@example.com",
                    "payer_name": {"given_name": "Han", "surname": "An"},
                },
            }
        ]
    }
    monkeypatch.setattr(ledger.settings, "PAYPAL_CLIENT_ID", "client")
    monkeypatch.setattr(ledger.settings, "PAYPAL_SECRET", "secret")
    monkeypatch.setattr(ledger, "get_paypal_transactions_api", lambda start, end: payload)
    monkeypatch.setattr(ledger, "SessionLocal", lambda: db)

    ledger.sync_paypal_transactions_job()

    entry = db.query(models.ExternalLedgerEntry).filter(
        models.ExternalLedgerEntry.provider == "paypal",
        models.ExternalLedgerEntry.external_id == "PAYPAL-API-1",
    ).first()
    assert entry is not None
    assert entry.source == ledger.PAYPAL_SOURCE
    assert entry.amount == 19.01
    assert entry.status == "posted"
    assert db.query(models.LedgerSyncRun).filter(models.LedgerSyncRun.provider == "paypal").count() == 1


def test_anonymization_in_synced_ledger():
    assert anonymize_email("ha@gmail.com") == "h*@gmail.com"
    assert anonymize_email("admin@entropydrop.com") == "ad***@entropydrop.com"
    assert anonymize_email(None) == "***"

    assert anonymize_name("John", "Doe") == "J*** D***"
    assert anonymize_name("A", "B") == "A B"
    assert anonymize_name("", "") == "User"


@pytest.mark.parametrize("route", [
    "/api/public/ledger", "/skin/api/public/ledger",
    "/api/open/ledger", "/skin/api/open/ledger",
])
def test_public_paypal_monthly_groups_hide_details_and_preserve_internal_records(client, db, route):
    private_title = "Private title for 明 王 private-payer@example.test"
    for external_id, amount, currency, event_code, month in [
        ("PRIVATE-TX-ONE", 19.01, "USD", "T0000", 5),
        ("PRIVATE-TX-TWO", 5.02, "usd", "T0000", 5),
        ("PRIVATE-TX-REFUND", -8.00, "USD", "T1107", 5),
        ("PRIVATE-TX-EUR", 7.00, "EUR", "T0000", 5),
        ("PRIVATE-TX-JUNE", 4.00, "USD", "T0000", 6),
    ]:
        add_external_entry(db, external_id=external_id, amount=amount, currency=currency,
            posted_at=datetime.datetime(2026, month, 24, 8, 30, tzinfo=datetime.timezone.utc),
            provider_account="private-payer@example.test", description=private_title,
            public_description=private_title, source="Private source",
            raw_payload={"event_code": event_code, "private_data": private_title})
    add_external_entry(db, external_id="PRIVATE-PENDING", amount=1000, status="pending")
    add_external_entry(db, external_id="PRIVATE-FAILED", amount=2000, status="failed")
    db.add(models.LedgerSyncRun(provider="paypal", source=ledger.PAYPAL_SOURCE,
        status="error", started_at=ledger.utc_now(), error_message=private_title))
    db.commit()
    before = {row.id: {column.name: getattr(row, column.name) for column in row.__table__.columns}
        for row in db.query(models.ExternalLedgerEntry).all()}

    response = client.get(route)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    serialized = json.dumps(data, ensure_ascii=False)
    for private in ["PRIVATE-", "明", "王", "private-payer", "Private title", "Private source", "08:30"]:
        assert private not in serialized
    rows = {(row["date"], row["currency"], row["category"]): row for row in data["entries"]}
    assert len(rows) == 4
    assert rows[("2026-05", "USD", "payment")]["amount_value"] == 24.03
    assert rows[("2026-05", "USD", "refund_reversal")]["amount_value"] == -8
    assert rows[("2026-05", "EUR", "payment")]["amount_value"] == 7
    assert rows[("2026-06", "USD", "payment")]["amount_value"] == 4
    for row in rows.values():
        assert set(row) == {"id", "date", "aggregation", "type", "provider", "category",
            "source", "desc", "amount", "amount_value", "revenue_value", "expense_value", "currency", "status"}
        assert row["aggregation"] == "monthly"
        assert row["id"] == f"paypal-monthly:{row['date']}:{row['currency']}:{row['category']}"
    summary = data["summaries"]["paypal"]
    assert summary["total"] is None
    assert summary["total_value"] is None
    assert summary["by_currency"]["USD"]["total_value"] == 20.03
    assert summary["by_currency"]["EUR"]["total_value"] == 7
    assert summary["by_currency"]["USD"]["revenue_value"] == 28.03
    assert summary["by_currency"]["USD"]["expense_value"] == -8
    assert data["sync"]["paypal"]["status"] == "error"
    db.expire_all()
    after = {row.id: {column.name: getattr(row, column.name) for column in row.__table__.columns}
        for row in db.query(models.ExternalLedgerEntry).all()}
    assert after == before


def test_public_paypal_keeps_inflows_and_outflows_within_same_group(client, db):
    for external_id, amount in [("POSITIVE", 10), ("NEGATIVE", -3), ("CENT-A", 0.1), ("CENT-B", 0.2)]:
        add_external_entry(db, external_id=external_id, amount=amount)
    db.commit()
    data = client.get("/api/public/ledger").json()
    assert len(data["entries"]) == 1
    row = data["entries"][0]
    assert row["amount_value"] == 7.3
    assert row["revenue_value"] == 10.3
    assert row["expense_value"] == -3
    assert data["summaries"]["paypal"]["total_value"] == 7.3


def test_public_paypal_uses_utc_month_and_fixed_categories():
    row = models.ExternalLedgerEntry(provider="paypal", posted_at=datetime.datetime(
        2026, 5, 1, 0, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=8))),
        currency="eur", category="PRIVATE-CATEGORY", amount=2, raw_payload={})
    public = ledger.public_paypal_monthly_entries([row])[0]
    assert public["date"] == "2026-04"
    assert public["currency"] == "EUR"
    assert public["category"] == "other"
    assert "PRIVATE" not in json.dumps(public)
    row.currency = "PRIVATE"
    assert ledger.public_paypal_monthly_entries([row])[0]["currency"] == "XXX"
    with pytest.raises(ValueError):
        ledger.public_entry_from_model(row)


def test_public_sync_does_not_expose_fallback_errors(client, monkeypatch):
    monkeypatch.setattr(ledger, "_paypal_sync_state", {
        "status": "error", "message": "PRIVATE-TX payer@example.test", "raw_payload": {"name": "Private"},
    })
    data = client.get("/api/public/ledger").json()
    assert data["sync"]["paypal"]["status"] == "error"
    assert "PRIVATE" not in json.dumps(data)
    assert "message" not in data["sync"]["paypal"]


@pytest.mark.parametrize("event_code,category", [
    ("T0002", "payment"), ("T1107", "refund_reversal"), ("T0107", "fee"),
    ("T0201", "currency_conversion"), ("T0300", "transfer"), ("T0400", "transfer"),
    ("T1700", "transfer"), ("T2000", "transfer"), ("T9900", "other"),
])
def test_public_paypal_classifies_historical_records_from_structured_event_codes(event_code, category):
    row = models.ExternalLedgerEntry(category="payment", raw_payload={"event_code": event_code},
        description="PRIVATE-TITLE", public_description="PRIVATE-TITLE")
    assert ledger.paypal_public_category(row) == category
