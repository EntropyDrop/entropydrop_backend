# Subscription queue acceleration

After a verified paid subscription activates, existing active AI generations
receive `pro_priority`. This is separate from the creation-time `is_pro`, license,
visibility, model parameters, credit charge and refund records.

The API attempts immediate promotion after committing payment. The singleton
background service reconciles every 10 seconds, including existing subscribers,
skipped row locks and submissions concurrent with activation. Durable priority
survives Redis outages and subscription expiry for these already accepted tasks.

Redis promotion checks the RQ status, origin, cancellation marker and actual
source-list membership in one Lua script. It moves the same job to the tail of
the corresponding high-priority queue and changes only its origin. Already
dequeued/running jobs are never recreated or interrupted. Scheduled/deferred
jobs keep their schedules; a retry still waiting after becoming due is promoted
on reconciliation. Workers consult `generation:pro_priority:<log_id>` at each
stage handoff so subsequent GPU stages/provider polls inherit the upgrade.
Markers expire after seven days and are refreshed while their generation is
active. Repeated payment delivery and repeated reconciliation are idempotent.

Queue positions count jobs waiting ahead in the actual normal/Pro queue pair;
they exclude running jobs and are estimates when workers serve multiple pairs.

## Deployment

1. Apply Alembic revision `c82e7a4d901b` (or `alembic upgrade head`). It backfills
   priority for existing Pro generations without changing their rights.
2. Deploy the GPU and real-to-render workers, including `queue_priority.py`.
3. Deploy the API and singleton background service, then the frontend.

Keep the background service running: it repairs missed promotions, scheduled
retries and handoffs crossing payment/worker boundaries. No live provider job
should be canceled or resubmitted as part of this rollout.

## Verification

Run backend `tests/test_generation_priority.py` with `redis-server` available.
The test suite starts a private Unix-socket Redis process with persistence
disabled; it never connects to deployment Redis. Tests cover worker-pop races,
intermediate queues, duplicate payment delivery, unpaid/foreign subscriptions,
outages, migration/backfill, actual queue order, retries and rights/credit
preservation. Worker suites cover inherited priority and cross-lane deduplication.
