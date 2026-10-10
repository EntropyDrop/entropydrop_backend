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

- Revision `e6b93f1a0c25` adds `users.figure_print_terms_version` and
  `users.figure_print_terms_accepted_at`. Run migrations, deploy the API, then
  deploy the frontend's download-confirmation flow. Existing rows remain null;
  accepting the general Terms of Service does not accept the printing terms.
- Authenticated GET/POST `/api/users/me/figure_print_terms` reads/records the
  printing agreement. POST requires `{ "version": "1.0", "accepted": true }`;
  the server supplies the user identity and UTC time, rejects outdated versions,
  and preserves the first acceptance time on retries. This is the latest user
  acceptance, not a download history or a history of all agreement versions.
- Keep `figure_print_terms.py` synchronized with the frontend's terms version
  and its archived text in `docs/legal/figure-print-terms/`. Publish changed
  terms under a new version; never replace text for an already published one.
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

### Figure kit review and refunds

Apply migration `a4e19c7d8032` before starting this version. Figure orders paid
through the checkout enter human review. Administrators listed in `ADMIN_EMAILS`
can use `/figure/manage` and the protected `/api/figure/orders` endpoints to
approve, reject with a reason, produce, ship with tracking, and complete orders.
Rejection applies to the entire paid order and requests a full PayPal refund.
The decision and customer mailbox notification commit before the payment call;
only a verified completed refund marks the order refunded. Refunds preserve the
same idempotency key across retries and restore known consumed stock once.

Keep `background_service.py` running for payment and refund reconciliation.
Pending refunds are checked at five-minute intervals; administrators can also
sync them manually. Partial/multiple refunds, mismatched amounts, and unknown
results older than six hours require manual PayPal reconciliation. Failed refund
IDs are queried rather than issuing a second refund blindly. Existing orders
already in production or fulfilled keep their workflow; unstarted paid orders
enter review. Historical stock deductions without evidence are not restored.

### CUTE-7cm kit price

Migration `b8d62a4f901c` updates the `Cute DIY Kit` print SKU to US$40 for new purchases. Stock and existing order/item price snapshots are preserved. The frontend reads the live price from `/api/orders/model-stock?order_type=print`. A schema downgrade does not revert operational pricing.


### Figure kit specifications

Apply `alembic upgrade head` before enabling the updated order API. Migration
`c7a31d902ef4` adds `model_sales_limits.kit_specifications` (the current English
product name, dimensions, material quantities/descriptions and assembly note)
and `order_items.kit_specifications_snapshot`. It seeds the CUTE-7cm catalog
without changing prices, inventory or historical orders.

The stock endpoint returns catalog specifications. Order creation validates the
server-side configuration against `schemas.KitSpecifications` and snapshots it
for every kit; client-supplied specifications cannot replace it. Updating a
product does not rewrite earlier order snapshots. User and administrator order
responses expose the saved snapshot. For historical items without a snapshot,
`kit_specifications_current` is a clearly identified current-catalog fallback.
Do not backfill current specifications as historical purchase terms.


### Order sticker snapshots and production sources

Migration `d2f81a604bc9` adds `order_items.sticker_snapshot`. New CUTE orders
save skin ID/name, publisher ID/username, canonical source URL, brand, model
label and the actual localized sticker labels. These values come from server
records; only `sticker_language` (`en` or `zh-hans`) is accepted from the client.
Each kit also keeps its own private PNG at `orders/{order_id}/{item_id}.png`.
The source skin row is locked during copying, coordinating with skin withdrawal;
a missing source or failed copy prevents order creation. Source withdrawal does
not remove order copies. Renaming/deleting the skin or publisher does not alter
saved sticker metadata. The order's `user_id` continues to identify the buyer.

The administrator-only endpoint
`GET /api/figure/orders/{order_id}/items/{item_id}/production-source` requires a
paid/fulfilled, approved order, a matching CUTE item, its private image key and
complete sticker snapshot. It signs the order copy on each request and returns
the saved sticker information without looking up the original skin or user.
The frontend preparation link contains only order/item IDs, so refreshing or
reopening it works after source deletion or signed URL expiry.

Legacy CUTE metadata is recovered from prior snapshots and remaining records,
marked `origin=legacy_backfill`. Names read from a current profile are recovery
values, not guaranteed historical names. Unrecoverable fields are enumerated in
`missing_fields`; production is blocked until those records are reviewed and
corrected from reliable evidence. This migration does not change prices, stock,
order totals or skin files. Apply migrations before enabling the new API.

The active CUTE-10cm kit catalog is installed by `a9c64e280fb1`: approximately 10 × 6.7 × 4.1 cm, one pre-cut sticker sheet, six printed body parts, one long joint and four short joints. It uses no PTFE tubes. Existing order specifications and sticker snapshots retain the model purchased.
