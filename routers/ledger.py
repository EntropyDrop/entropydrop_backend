import asyncio
import datetime
import hashlib
from typing import Any

import boto3
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import models
from config import settings
from database import SessionLocal, get_db
from payment_utils import get_paypal_transactions_api, anonymize_email, anonymize_name

router = APIRouter(prefix="/api/public", tags=["ledger"])
legacy_open_router = APIRouter(prefix="/api/open", tags=["ledger"])

PAYPAL_SOURCE = "PayPal Reporting API"
AWS_SOURCE = "AWS Cost Explorer"
PAYPAL_SUCCESS_STATUSES = {"S", "SUCCESS", "COMPLETED"}
PUBLIC_LEDGER_STATUSES = {"posted", "estimated"}
PUBLIC_LEDGER_PROVIDERS = {"paypal", "aws", "bank"}

_paypal_sync_state: dict[str, Any] = {"status": "not_synced", "last_synced_at": None}
_aws_sync_state: dict[str, Any] = {"status": "not_synced", "last_synced_at": None}


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def isoformat_z(value: datetime.datetime | None = None) -> str:
    dt = value or utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_amount_value(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    cleaned = str(value).strip().replace("$", "").replace(",", "")
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def format_money(amount: float, currency: str = "USD") -> str:
    sign = "+" if amount >= 0 else "-"
    prefix = "$" if currency.upper() == "USD" else f"{currency.upper()} "
    return f"{sign}{prefix}{abs(amount):.2f}"


def external_ledger_id(provider: str, external_id: str) -> str:
    raw = f"{provider}:{external_id}"
    if len(raw) <= 220:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{provider}:{digest}"


def parse_paypal_time(time_str: str | None) -> datetime.datetime:
    if not time_str:
        return utc_now()

    cleaned = time_str.replace("Z", "+00:00")
    if len(cleaned) >= 24 and (cleaned[-5] == "+" or cleaned[-5] == "-") and cleaned[-3] != ":":
        cleaned = cleaned[:-2] + ":" + cleaned[-2:]

    try:
        parsed = datetime.datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed
    except Exception:
        return utc_now()


def parse_aws_date(date_str: str) -> datetime.datetime:
    try:
        parsed = datetime.date.fromisoformat(date_str)
        return datetime.datetime.combine(parsed, datetime.time.min, tzinfo=datetime.timezone.utc)
    except Exception:
        return utc_now()


def paypal_public_status(raw_status: str | None) -> str:
    if raw_status in PAYPAL_SUCCESS_STATUSES:
        return "posted"
    if raw_status in {"P", "PENDING"}:
        return "pending"
    return "failed" if raw_status else "unknown"


def upsert_external_entry(db: Session, values: dict[str, Any]) -> str:
    provider = values["provider"]
    external_id = values["external_id"]
    entry_id = external_ledger_id(provider, external_id)
    existing = db.query(models.ExternalLedgerEntry).filter(
        models.ExternalLedgerEntry.provider == provider,
        models.ExternalLedgerEntry.external_id == external_id,
    ).first()

    now = utc_now()
    values = {**values, "id": entry_id, "updated_at": now}

    if existing:
        for key, value in values.items():
            if key not in {"id", "created_at"}:
                setattr(existing, key, value)
        return "updated"

    db.add(models.ExternalLedgerEntry(**values, created_at=now))
    return "inserted"


def record_sync_run(
    db: Session,
    *,
    provider: str,
    source: str,
    status: str,
    started_at: datetime.datetime,
    range_start: datetime.datetime | None = None,
    range_end: datetime.datetime | None = None,
    records_inserted: int = 0,
    records_updated: int = 0,
    error_message: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> models.LedgerSyncRun:
    run = models.LedgerSyncRun(
        provider=provider,
        source=source,
        status=status,
        range_start=range_start,
        range_end=range_end,
        started_at=started_at,
        finished_at=utc_now(),
        records_inserted=records_inserted,
        records_updated=records_updated,
        error_message=error_message,
        metadata_json=metadata_json,
    )
    db.add(run)
    return run


def sync_state_from_db(db: Session, provider: str, fallback: dict[str, Any]) -> dict[str, Any]:
    run = db.query(models.LedgerSyncRun).filter(
        models.LedgerSyncRun.provider == provider
    ).order_by(models.LedgerSyncRun.started_at.desc()).first()

    if not run:
        return fallback

    return {
        "status": run.status,
        "last_synced_at": isoformat_z(run.finished_at or run.started_at) if run.status == "ok" else None,
        "records_inserted": run.records_inserted or 0,
        "records_updated": run.records_updated or 0,
        "range_start": isoformat_z(run.range_start) if run.range_start else None,
        "range_end": isoformat_z(run.range_end) if run.range_end else None,
        "message": run.error_message,
    }


def public_entry_from_model(entry: models.ExternalLedgerEntry) -> dict[str, Any]:
    posted_at = entry.posted_at or entry.created_at or utc_now()
    amount = round(entry.amount or 0.0, 2)
    public_type = entry.entry_type if entry.entry_type in {"revenue", "expense"} else (
        "revenue" if amount >= 0 else "expense"
    )

    result = {
        "id": entry.id,
        "date": posted_at.strftime("%Y-%m-%d %H:%M"),
        "type": public_type,
        "provider": entry.provider,
        "category": entry.category,
        "source": entry.source,
        "desc": entry.public_description or entry.description or entry.source,
        "amount": format_money(amount, entry.currency or "USD"),
        "amount_value": amount,
        "currency": entry.currency or "USD",
        "status": entry.status,
    }

    optional_values = {
        "period_start": isoformat_z(entry.period_start) if entry.period_start else None,
        "period_end": isoformat_z(entry.period_end) if entry.period_end else None,
        "synced_at": isoformat_z(entry.synced_at) if entry.synced_at else None,
        "gross_amount": entry.gross_amount,
        "fee_amount": entry.fee_amount,
        "net_amount": entry.net_amount,
    }
    for key, value in optional_values.items():
        if value is not None:
            result[key] = value

    return result


def public_external_entries(db: Session) -> list[dict[str, Any]]:
    rows = db.query(models.ExternalLedgerEntry).filter(
        models.ExternalLedgerEntry.provider.in_(PUBLIC_LEDGER_PROVIDERS),
        models.ExternalLedgerEntry.status.in_(PUBLIC_LEDGER_STATUSES)
    ).order_by(models.ExternalLedgerEntry.posted_at.desc()).all()
    return [public_entry_from_model(row) for row in rows]


def build_summary(entries: list[dict[str, Any]], provider: str | None = None) -> dict[str, Any]:
    scoped = [entry for entry in entries if provider is None or entry.get("provider") == provider]
    values = [parse_amount_value(entry.get("amount_value", entry.get("amount"))) for entry in scoped]
    total_value = round(sum(values), 2)
    revenue_value = round(sum(max(value, 0.0) for value in values), 2)
    expense_value = round(sum(min(value, 0.0) for value in values), 2)
    last_entry_at = max((entry.get("date") for entry in scoped), default=None)
    return {
        "count": len(scoped),
        "total": format_money(total_value, "USD"),
        "total_value": total_value,
        "revenue": format_money(revenue_value, "USD"),
        "revenue_value": revenue_value,
        "expense": format_money(expense_value, "USD"),
        "expense_value": expense_value,
        "last_entry_at": last_entry_at,
    }


def fetch_aws_cost_explorer_entries() -> tuple[list[dict[str, Any]], str, datetime.datetime | None, datetime.datetime | None]:
    if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY:
        print("[!] AWS credentials not configured. Skipping Cost Explorer query.")
        return [], "not_configured", None, None

    try:
        ce_client = boto3.client(
            "ce",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name="us-east-1",
        )

        now = utc_now()
        lookback_days = max(1, settings.LEDGER_AWS_LOOKBACK_DAYS)
        start_date = (now.date() - datetime.timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        end_date = now.date().strftime("%Y-%m-%d")
        range_start = parse_aws_date(start_date)
        range_end = parse_aws_date(end_date)

        print(f"[*] Querying AWS Cost Explorer from {start_date} to {end_date}...")
        response = ce_client.get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )

        entries: list[dict[str, Any]] = []
        synced_at = utc_now()
        for row in response.get("ResultsByTime", []):
            time_period = row.get("TimePeriod", {})
            start = time_period.get("Start", "")
            end = time_period.get("End", "")
            cost_str = row.get("Total", {}).get("UnblendedCost", {}).get("Amount", "0")
            cost = float(cost_str)

            if cost < 0.005:
                continue

            period_start = parse_aws_date(start)
            period_end = parse_aws_date(end)
            amount = -round(cost, 2)
            entries.append(
                {
                    "provider": "aws",
                    "provider_account": None,
                    "external_id": f"{start}:UnblendedCost",
                    "entry_type": "expense",
                    "category": "cloud",
                    "amount": amount,
                    "gross_amount": None,
                    "fee_amount": None,
                    "net_amount": amount,
                    "currency": "USD",
                    "description": "AWS Cloud Services",
                    "public_description": "AWS Cloud Services",
                    "status": "estimated" if row.get("Estimated") else "posted",
                    "source": AWS_SOURCE,
                    "posted_at": period_start,
                    "period_start": period_start,
                    "period_end": period_end,
                    "synced_at": synced_at,
                    "raw_payload": {
                        "metric": "UnblendedCost",
                        "amount": cost_str,
                        "estimated": bool(row.get("Estimated")),
                        "period": time_period,
                    },
                }
            )

        return entries, "ok", range_start, range_end
    except Exception as e:
        print(f"[!] Failed to fetch costs from AWS Cost Explorer: {e}")
        return [], "error", None, None


def sync_aws_billing_job() -> None:
    global _aws_sync_state

    started_at = utc_now()
    db = SessionLocal()
    try:
        entries, status, range_start, range_end = fetch_aws_cost_explorer_entries()
        inserted = 0
        updated = 0

        if status == "ok":
            for entry in entries:
                result = upsert_external_entry(db, entry)
                if result == "inserted":
                    inserted += 1
                else:
                    updated += 1

        record_sync_run(
            db,
            provider="aws",
            source=AWS_SOURCE,
            status=status,
            started_at=started_at,
            range_start=range_start,
            range_end=range_end,
            records_inserted=inserted,
            records_updated=updated,
            error_message=None if status == "ok" else "AWS billing sync skipped or failed",
            metadata_json={"lookback_days": settings.LEDGER_AWS_LOOKBACK_DAYS},
        )
        db.commit()

        _aws_sync_state = {
            "status": status,
            "last_synced_at": isoformat_z(started_at) if status == "ok" else None,
            "records_inserted": inserted,
            "records_updated": updated,
        }
        print(f"[*] AWS billing sync complete. Inserted {inserted}, updated {updated}.")
    except Exception as e:
        db.rollback()
        _aws_sync_state = {"status": "error", "last_synced_at": None, "message": str(e)}
        print(f"[!] AWS billing sync failed: {e}")
    finally:
        db.close()


def sync_paypal_transactions_job() -> None:
    global _paypal_sync_state

    print("[*] Starting PayPal transactions synchronization job...")
    db = SessionLocal()
    started_at = utc_now()
    now_dt = utc_now()
    lookback_days = max(1, settings.LEDGER_PAYPAL_LOOKBACK_DAYS)
    start_dt = now_dt - datetime.timedelta(days=lookback_days)

    try:
        if not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_SECRET:
            print("[!] PayPal credentials are not configured in settings. Skipping reporting query.")
            record_sync_run(
                db,
                provider="paypal",
                source=PAYPAL_SOURCE,
                status="not_configured",
                started_at=started_at,
                range_start=start_dt,
                range_end=now_dt,
                error_message="PayPal credentials are not configured",
                metadata_json={"lookback_days": lookback_days},
            )
            db.commit()
            _paypal_sync_state = {
                "status": "not_configured",
                "last_synced_at": None,
                "message": "PayPal credentials are not configured",
            }
            return

        start_str = start_dt.strftime("%Y-%m-%dT00:00:00Z")
        end_str = now_dt.strftime("%Y-%m-%dT23:59:59Z")

        print(f"[*] Querying PayPal Reporting API from {start_str} to {end_str}")
        data = get_paypal_transactions_api(start_str, end_str)
        details = data.get("transaction_details", [])
        print(f"[*] Retrieved {len(details)} raw transaction records from PayPal.")

        inserted = 0
        updated = 0
        synced_at = utc_now()
        for item in details:
            info = item.get("transaction_info", {})
            payer = item.get("payer_info", {})
            tx_id = info.get("transaction_id")
            if not tx_id:
                continue

            gross_amount = float(info.get("transaction_amount", {}).get("value", 0.0))
            fee_amount = float(info.get("fee_amount", {}).get("value", 0.0))
            net_amount = round(gross_amount + fee_amount, 2)
            currency = info.get("transaction_amount", {}).get("currency_code", "USD")

            payer_email = payer.get("payer_email", "")
            payer_name_dict = payer.get("payer_name", {})
            given_name = payer_name_dict.get("given_name", "")
            surname = payer_name_dict.get("surname", "")
            masked_email = anonymize_email(payer_email) if payer_email else None
            masked_name = anonymize_name(given_name, surname) if (given_name or surname) else None

            event_code = info.get("transaction_event_code", "")
            raw_status = info.get("transaction_status")
            desc = info.get("transaction_subject", "")
            if not desc:
                if event_code.startswith("T00"):
                    desc = "PayPal Payment"
                elif event_code.startswith("T11"):
                    desc = "Refund/Reversal"
                else:
                    desc = "PayPal Transaction"
            if masked_name:
                desc = f"{desc} - {masked_name}"

            posted_at = parse_paypal_time(info.get("transaction_initiation_date"))
            result = upsert_external_entry(
                db,
                {
                    "provider": "paypal",
                    "provider_account": masked_email,
                    "external_id": tx_id,
                    "entry_type": "revenue" if net_amount >= 0 else "expense",
                    "category": "payment",
                    "amount": net_amount,
                    "gross_amount": round(gross_amount, 2),
                    "fee_amount": round(fee_amount, 2),
                    "net_amount": net_amount,
                    "currency": currency,
                    "description": desc,
                    "public_description": desc,
                    "status": paypal_public_status(raw_status),
                    "source": PAYPAL_SOURCE,
                    "posted_at": posted_at,
                    "period_start": None,
                    "period_end": None,
                    "synced_at": synced_at,
                    "raw_payload": {
                        "event_code": event_code,
                        "raw_status": raw_status,
                        "masked_payer_email": masked_email,
                        "masked_payer_name": masked_name,
                    },
                },
            )
            if result == "inserted":
                inserted += 1
            else:
                updated += 1

        record_sync_run(
            db,
            provider="paypal",
            source=PAYPAL_SOURCE,
            status="ok",
            started_at=started_at,
            range_start=start_dt,
            range_end=now_dt,
            records_inserted=inserted,
            records_updated=updated,
            metadata_json={"lookback_days": lookback_days},
        )
        db.commit()

        _paypal_sync_state = {
            "status": "ok",
            "last_synced_at": isoformat_z(started_at),
            "records_inserted": inserted,
            "records_updated": updated,
            "lookback_days": lookback_days,
        }
        print(f"[*] PayPal sync complete. Inserted {inserted}, updated {updated}.")
    except Exception as e:
        db.rollback()
        _paypal_sync_state = {"status": "error", "last_synced_at": None, "message": str(e)}
        print(f"[!] PayPal transactions sync failed: {e}")
    finally:
        db.close()


def sync_ledger_sources_once() -> None:
    sync_paypal_transactions_job()
    sync_aws_billing_job()


async def start_ledger_sync_job() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await asyncio.to_thread(sync_ledger_sources_once)
        except Exception as e:
            print("[!] Background ledger sync exception:", e)
        await asyncio.sleep(settings.LEDGER_SYNC_INTERVAL_SECONDS)


async def start_paypal_sync_job() -> None:
    await start_ledger_sync_job()


@legacy_open_router.get("/ledger", include_in_schema=False)
@router.get("/ledger")
async def get_public_ledger(db: Session = Depends(get_db)):
    entries = public_external_entries(db)

    return {
        "entries": entries,
        "summaries": {
            "all": build_summary(entries),
            "paypal": build_summary(entries, "paypal"),
            "aws": build_summary(entries, "aws"),
        },
        "sync": {
            "mode": "daily_api_snapshot",
            "interval_seconds": settings.LEDGER_SYNC_INTERVAL_SECONDS,
            "generated_at": isoformat_z(),
            "paypal": {
                **sync_state_from_db(db, "paypal", _paypal_sync_state),
                "lookback_days": settings.LEDGER_PAYPAL_LOOKBACK_DAYS,
            },
            "aws": {
                **sync_state_from_db(db, "aws", _aws_sync_state),
                "lookback_days": settings.LEDGER_AWS_LOOKBACK_DAYS,
            },
        },
    }
