# EntropyDrop Backend

FastAPI backend for EntropyDrop skin generation, collections, orders, subscriptions, and the public ledger API.

## Local Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Start local infrastructure:

```bash
docker compose up -d db redis
alembic upgrade head
```

Run the API and singleton background worker in separate terminals:

```bash
uvicorn main:app --reload --port 8000
python background_service.py
```

The API is mounted under `/skin`; for example:

```text
http://localhost:8000/skin/api/health
```

To connect the account API to a separate Space service, configure
`SPACE_SERVICE_URL` on the account API and `SPACE_STANDALONE=true` on the Space API.
Keep machine-specific deployment scripts and credentials in the ignored `deploy/`
and `.local/` directories.

## Production Notes

- Keep real credentials in your deployment secret manager or local `.env` files.
- Do not commit `.env`, `.env.prod`, private keys, virtualenvs, coverage output, or deployment scripts with environment-specific details.
- Run migrations with `alembic upgrade head` before starting new API versions.
- Revision `a8e6c4d20918` adds payment idempotency, inventory holds, shipping
  address snapshots, and durable skin withdrawal state. Upgrade the database
  before restarting the API and `background_service.py`. It does not change
  the monolithic Space Protobuf v5 constraint.
- Print checkout reserves stock for 30 minutes. A capture with an uncertain
  result keeps its stock until PayPal reconciliation; do not manually release
  those holds. The background worker reconciles payments every minute.
  Historical completed payments without available stock use
  `goods_status=awaiting_stock` for fulfillment review.
- Shipping addresses are copied into orders. The migration backfills existing
  orders from available address-book records; previously deleted addresses
  cannot be reconstructed from this database.
- Skin visibility is chosen at creation and cannot be changed afterward.
  There are no public-to-private or private-to-public conversion endpoints.
- Public skin deletion requires `AWS_CLOUDFRONT_DISTRIBUTION_ID` when
  `AWS_CDN_DOMAIN` is set, plus CloudFront CreateInvalidation/GetInvalidation
  permissions. S3 or CDN failures return a retryable 503 and retain a manifest.
  The background worker retries every 30 seconds, and success is reported only
  after CloudFront reports completion. Previously downloaded/browser-cached
  copies cannot be recalled. Legacy external asset URLs require storage
  migration before withdrawal.
- Subscription approval alone does not grant credits. A verified PayPal
  `billing_info.last_payment` identifies the paid cycle. Approval preceding
  the first payment preserves the owner binding for the subsequent webhook.

## Tests

```bash
python -m pytest
```

The PostgreSQL regressions create and drop random databases on a disposable
PostgreSQL server. The configured role needs CREATEDB and pg_trgm permissions:

```bash
REVIEW_POSTGRES_URL=postgresql://postgres:password@localhost:5432/review python -m pytest -q tests/test_postgres_review.py
python tests/verify_nginx_limits.py
```

The second command uses an ephemeral Docker Nginx container and an isolated
test upstream to verify the 512 KiB / 9 MiB / 17 MiB request boundaries.
