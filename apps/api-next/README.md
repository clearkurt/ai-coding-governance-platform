# New FastAPI control plane

This directory is the parallel replacement for the legacy Fastify + SQLite API. Its target control-plane slice is implemented and locally tested, but it is not wired into production traffic and is not production-ready.

## Local development

Use Python 3.12 or newer. Create an isolated virtual environment, install the project, copy `.env.example` to a local `.env`, and point `COMPANY_AGENT_DATABASE_URL` at PostgreSQL:

```powershell
cd apps/api-next
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --port 8081
pytest
ruff check .
```

The API has `GET /health/live` and `GET /health/ready`. The readiness endpoint checks the configured PostgreSQL connection without logging credentials.

The first Alembic revision creates the target control-plane schema. It intentionally stores only credential/token hashes and stable project root identifiers; local absolute paths and raw secrets must never enter this database.

## Current control-plane slice

`POST /auth/login` creates a secure HttpOnly session cookie after checking a scrypt password hash. `POST /auth/logout` revokes that token server-side as well as clearing the cookie. Devices authenticate as the first message on `WSS /ws/devices`; their credentials are stored and checked only as hashes, with expiry and revocation checks.

Logged-in users create a ten-minute single-use code with `POST /pairing-codes`. Rust `enroll` sends that code as the first `/ws/devices` message; successful consumption atomically creates the device, hashed credential, projects and creator grants. `GET /devices`, `GET /projects`, and `GET/POST /conversations` provide the target Vue resource workflow.

`POST /tasks` is idempotent per team, device and idempotency key. A connected target device receives a `task.dispatch` with a durable `delivery_id`; reconnecting devices are redelivered pending/running tasks and acknowledge delivery with `task.dispatch.ack`. Device events are assigned an authoritative persisted sequence and deduplicated by `source_event_id`. Browsers resume `GET /tasks/{id}/events` with `Last-Event-ID`; the default stream follows later persisted events. `?follow=false` is available for bounded replay clients and tests.

`POST /model-tokens` exchanges a valid device credential for a short-lived task-bound token. `POST /v1/responses` validates that token, enforces the selected model, concurrency and daily team quota, then transparently forwards raw streaming or non-streaming Responses API traffic to DeepSeek. The upstream key is never returned to the device.

Approval decisions and rollback requests use stable delivery identifiers, replay after device reconnect, and remain pending until the device ACKs them. `GET /tasks/{id}/audit` exposes task-scoped audit metadata to an authorized project member. `POST /tasks/{id}/rollback` is available only for terminal tasks and asks the local daemon to restore its retained pre-turn snapshot.

For local HTTP development set `COMPANY_AGENT_SESSION_COOKIE_SECURE=false`. Production must use HTTPS/WSS and leave secure cookies enabled.

The current acceptance baseline is 43 passing regular pytest tests, six environment-gated PostgreSQL integration tests, and a clean Ruff check. The migration lifecycle, real `PostgresStore` lifecycle, same-process localhost WebSocket reconnect, subprocess uvicorn restart, and custom-format backup/restore integrations have all been run successfully against a local PostgreSQL 16 instance. A localhost TLS/WSS certificate-validation integration is implemented and awaits an explicit environment-gated run. Real DeepSeek Responses traffic, a production-matched database drill, and production TLS/WSS remain release gates; see [rollout-acceptance.md](../../docs/rollout-acceptance.md).

To repeat the destructive migration check safely, supply an administrative URL for an existing PostgreSQL instance. The test creates a random dedicated login/database, runs upgrade/downgrade/re-upgrade only there, terminates its own remaining connections, and removes both objects in `finally`. It never prints or stores the password:

```powershell
$env:COMPANY_AGENT_TEST_POSTGRES_ADMIN_URL = "postgresql://admin:<password>@127.0.0.1:5432/postgres"
pytest -m integration -q
Remove-Item Env:COMPANY_AGENT_TEST_POSTGRES_ADMIN_URL
```

Without the variable, the integration test is skipped. Use a disposable PostgreSQL instance or a test administrator with `CREATEDB` and `CREATEROLE`; superuser is not required. The test temporarily grants its randomly created child role to that administrator so PostgreSQL permits `CREATE DATABASE ... OWNER`, then revokes the membership during cleanup. Never point application credentials at this test.

The same command also runs the real `PostgresStore` lifecycle against a separately randomized migrated database. It covers sessions, single-use pairing, discovery, conversations, idempotent and single-active tasks, delivery/event deduplication, approvals, task-bound model tokens, usage deduplication, terminal revocation, audit, rollback, and cross-team isolation with real transactions and constraints. It reconstructs `AsyncSession` and `PostgresStore` after task commit, before approval ACK, and before rollback ACK, then verifies persisted replay and ACK removal. This proves Store/database recovery, not a complete ASGI process or real WSS socket restart.

The integration command also starts uvicorn on a random localhost port and uses a real `websockets` client to exercise reconnect before dispatch/event ACK and pending approval/rollback replay. This is intentionally plain local `ws://`; it is not a TLS or production WSS test.

A separate integration starts uvicorn as an actual child process, disconnects before task delivery ACK, terminates that process, starts a fresh uvicorn process with a fresh in-memory connection registry against the same random PostgreSQL database, and verifies the identical task/delivery is replayed and can be acknowledged. It proves database-backed recovery across a local ASGI process boundary, not production orchestration or TLS.

The backup/restore integration uses same-major `pg_dump` and `pg_restore` with PostgreSQL's custom format. It creates random source and restore roles/databases only, seeds pending task and approval state plus events, model usage and audit records, restores into a fresh database, then verifies schema constraints and behavior through a fresh `PostgresStore`. All temporary database objects are removed in `finally`. Put PostgreSQL 16's `bin` directory on `PATH`, or set `COMPANY_AGENT_TEST_PG_DUMP` and `COMPANY_AGENT_TEST_PG_RESTORE` to the two executable paths. See [postgres-backup-restore-drill.md](../../docs/postgres-backup-restore-drill.md) for the exact contract and command.

The localhost TLS/WSS integration starts a real uvicorn TLS child process with a one-use CA and server certificate, then verifies trusted-CA device authentication, durable delivery replay and ACK. It also proves that an unknown CA and a hostname mismatch fail certificate verification. It never disables client verification. This is a local application TLS contract, not evidence for production reverse-proxy, PKI, rotation or deployment configuration; see [tls-wss-integration.md](../../docs/tls-wss-integration.md).
