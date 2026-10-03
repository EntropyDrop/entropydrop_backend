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

## Model credit pricing

In `/skin/monitor`, set `SKING_DDJ_v101c`'s **All users** price to **12** and
**Pro only** price to **4**. The image-to-skin model selector shows two entries
with the same model name and distinct access labels and prices. Both execute
the same model and share the maintenance switch. The selector defaults to the
first Pro-only option for everyone. Free users see the subscription action and
can choose All users at its listed price; active Pro users can choose either.
Deploy the backend before the frontend. No database migration is required;
existing prices remain until an administrator saves a new All users price.

`POST /skin/api/monitor/model_prices` accepts `credits` (the existing Pro price)
and `free_credits`, alongside `model_name`, `is_pro` (Pro-only access), and
`under_maintenance`. Prices are stored in Redis as `config:model_price:<model>`
and `config:model_free_price:<model>`. An unset Free price falls back to the
existing price; older clients that omit `free_credits` preserve any override.
`GET /skin/api/models` exposes `image_to_skin_options` with unique option IDs,
the real `model_version`, and `pricing_tier` (`standard` or `pro`). Send the real
model version and selected pricing tier to the quote and generation endpoints.
These options have fixed access rules independent of the legacy model-wide
`is_pro` flag: `standard` is open to everyone; `pro` requires an active, unexpired
subscription. The frontend never sends an option ID as the real model version.
The monitor saves both prices together and clears the legacy Pro-only flag for
this model. Other models and older clients without `pricing_tier` retain their
membership-based pricing. Combined models add each model's price for that tier.
Refunds use the amount recorded when the task was submitted.

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


### Agent browser authorization

`/space/api/v2/agent-authorizations` implements a device-style pairing flow:
`POST /requests` starts a ten-minute request; the signed-in user reviews it with
`POST /inspect` and submits `POST /decision`; the agent polls `POST /token` with
its private `device_code`. All bodies are JSON. Consent uses the normal browser
Bearer login token. An API key cannot approve another agent.

Run `alembic upgrade head` before deploying this version (migration
`d9a7e3b10462`). Set `SPACE_AGENT_VERIFICATION_URI` to the public main-site
`/space/authorize` page; only HTTPS or loopback HTTP URLs are accepted. The URI
is server configuration, never supplied by an agent. Deploy the matching frontend
route and Space discovery/docs together. Existing manual key creation still works.

Pairing codes are stored as hashes, expire after ten minutes and are rate limited.
The key is minted at redemption under the existing per-user quota lock. A retry
with the same device code returns the same key until the request expires; revoking
that key prevents further redemption. This preserves the existing long-lived,
revocable full-Space key contract. Expired request rows are pruned on new requests.
