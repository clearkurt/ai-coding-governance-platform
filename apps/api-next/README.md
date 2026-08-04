# New FastAPI control plane

This directory is the parallel replacement for the legacy Fastify + SQLite API. It is not yet wired into production traffic.

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
```

The API has `GET /health/live` and `GET /health/ready`. The readiness endpoint checks the configured PostgreSQL connection without logging credentials.

The first Alembic revision creates the target control-plane schema. It intentionally stores only credential/token hashes and stable project root identifiers; local absolute paths and raw secrets must never enter this database.

## Current control-plane slice

`POST /auth/login` creates a secure HttpOnly session cookie after checking a scrypt password hash. `POST /auth/logout` revokes that token server-side as well as clearing the cookie. Devices authenticate as the first message on `WSS /ws/devices`; their credentials are stored and checked only as hashes, with expiry and revocation checks.

`POST /tasks` is idempotent per team, device and idempotency key. A connected target device receives a `task.dispatch` with a durable `delivery_id`; reconnecting devices are redelivered pending/running tasks and acknowledge delivery with `task.dispatch.ack`. Device events are assigned an authoritative persisted sequence and deduplicated by `source_event_id`. Browsers resume `GET /tasks/{id}/events` with `Last-Event-ID`; the default stream follows later persisted events. `?follow=false` is available for bounded replay clients and tests.
