import datetime

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
