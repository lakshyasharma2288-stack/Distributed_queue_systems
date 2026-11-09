# Distributed Job Queue System

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Queue-Redis-DC382D?logo=redis&logoColor=white)](https://redis.io/)

A Python distributed job processing system built with FastAPI, PostgreSQL, Redis, SQLAlchemy, and independent publisher and worker processes. It demonstrates durable job submission, a transactional outbox, leased work claims, retry scheduling, dead-letter recording, and recovery when Redis messages or workers are lost.

The repository also includes a small React dashboard and task handlers for email submission, image resizing, and PDF generation. The design is intended as a practical backend and distributed-systems project; the API does not include authentication and should not be exposed publicly without additional controls.

## Project overview

The system accepts jobs through a REST API and stores them in PostgreSQL. A publisher transfers committed outbox events to a Redis sorted-set queue. Independent workers consume queue hints, claim jobs conditionally in PostgreSQL, execute registered task handlers, and persist their results and attempt history.

PostgreSQL is the source of truth for job state. Redis is a delivery mechanism, not the authoritative record of whether a job exists or has completed. The publisher can republish pending work and recover or reconcile jobs when Redis loses a message.

### Problem statement

Running long or failure-prone work inside an API request couples user-facing availability to task duration and process lifetime. This project separates request handling from execution and explores how durable persistence, independent workers, retries, and recovery can make asynchronous processing more reliable.

### Objectives

- Separate job submission from task execution.
- Persist accepted jobs and their publication intent atomically.
- Support independent publisher and worker processes.
- Make job claims conditional and protect completion with renewable leases and fencing versions.
- Record attempts and terminal failures, and retry retryable failures with capped exponential backoff and jitter.
- Make queue messages recoverable from PostgreSQL state.
- Keep task payload validation and handlers separate from the API.

## Key features

- FastAPI endpoints for health, jobs, image uploads and stored images, plus optional queue inspection.
- PostgreSQL job state, outbox events, worker records, attempt history, dead letters, and image metadata, managed by Alembic.
- Redis sorted-set queue ordered by priority and creation time; enqueue uses `ZADD NX`.
- A separately runnable outbox publisher, including retry scheduling, interrupted-job recovery, and pending-job reconciliation.
- Independent workers with atomic job claims, heartbeats, renewable leases, fencing, and graceful signal handling.
- Optional `Idempotency-Key` support for job submission and job-aware idempotency for handlers where supported.
- Registered handlers for `send_email`, `resize_image`, and `generate_pdf`.
- Image upload validation by file contents, configured upload and pixel limits, and storage constrained to configured directories.
- Unit tests using SQLite and fakeredis; separately marked PostgreSQL and Redis integration tests.
- Docker Compose configuration for PostgreSQL, Redis, API, publisher, worker, and the React frontend.

## Technology stack

| Area | Technologies |
|---|---|
| API and validation | Python 3.12, FastAPI, Pydantic |
| Persistence and migrations | PostgreSQL, SQLAlchemy 2, Alembic, psycopg |
| Queue and test double | Redis sorted sets, redis-py, fakeredis |
| Task handlers | Resend, Pillow, ReportLab |
| Tests | pytest, HTTPX, SQLite |
| Dashboard | React, Vite, Nginx |
| Container deployment | Docker, Docker Compose |

## Architecture

```mermaid
flowchart LR
    Client[Client or React dashboard] -->|HTTP| API[FastAPI API]
    UI[React dashboard] --> Proxy[Nginx reverse proxy]
    Proxy --> API

    API -->|one transaction: job + outbox event| PG[(PostgreSQL)]
    Publisher[Outbox publisher] -->|claim pending events| PG
    Publisher -->|ZADD NX queue hint| Redis[(Redis sorted set)]
    Redis -->|ZPOPMIN job hint| Worker[Worker process]
    Worker -->|conditional claim, lease, attempts| PG
    Worker --> Dispatcher[Task dispatcher]
    Dispatcher --> Email[Email handler]
    Dispatcher --> Image[Image handler]
    Dispatcher --> PDF[PDF handler]
    Worker -->|status, result, attempt, dead letter| PG
    Publisher -->|retry scheduling, recovery, reconciliation| PG
    Publisher -->|republish eligible job hints| Redis
```

### Component responsibilities

- **FastAPI API gateway:** validates requests, writes jobs and outbox events, reads job/image data, and optionally exposes read-only queue inspection. Job submission does not publish directly to Redis.
- **PostgreSQL:** source of truth for jobs, statuses, outbox events, leases, worker heartbeats, attempts, dead letters, and image metadata.
- **Outbox publisher:** publishes pending events to Redis and, on each batch, schedules due retries, recovers interrupted work, and reconciles pending jobs.
- **Redis queue:** stores compact job hints in a sorted set. The score prioritizes higher priority values and then earlier creation times. Redis messages may be replayed or recreated.
- **Workers:** consume hints, conditionally transition eligible jobs from `pending` to `processing`, increment attempts, hold renewable leases, and run the dispatcher.
- **Dispatcher and task handlers:** validate task-specific payloads and call the matching handler. The currently registered types are `send_email`, `resize_image`, and `generate_pdf`.
- **Retry and state management:** successful jobs become `completed`; retryable failures may enter `retry_scheduled`; terminal failures become `failed` and are recorded in `dead letters`. `app/worker/state_machine.py` restricts supported state transitions.
- **Recovery and reconciliation:** the publisher detects expired leases or stale worker ownership, schedules another attempt when allowed, and rebuilds queue hints for pending jobs.
- **Dashboard:** the React/Vite frontend is served by Nginx in Compose; Nginx proxies `/api/` to the API service.

### Job processing lifecycle

1. **Submission:** `POST /jobs` validates the request and inserts a `pending` job plus a `job_created` outbox event in one PostgreSQL transaction.
2. **Publishing:** a separate publisher reads pending outbox events and adds compact job messages to Redis. It marks an event published only after Redis accepts the message. Redis failure leaves the event pending for a later attempt.
3. **Consumption:** a worker removes the minimum-scored message from Redis. The message is a hint; the worker checks PostgreSQL before doing any work.
4. **Claim and execution:** the worker atomically claims a still-pending job, increments its attempt count and lease version, records an attempt, then dispatches the task.
5. **Completion:** while its lease is valid, the worker commits the result, timestamps, attempt outcome, and `completed` status to PostgreSQL.
6. **Failure and retry:** non-retryable failures or exhausted attempts become `failed` and create a dead-letter record. Retryable failures below the attempt limit become `retry_scheduled`; the publisher later makes them pending and republishes them after the scheduled delay.
7. **Recovery:** if a worker disappears or its lease expires, the publisher records the interrupted attempt and either schedules a retry or dead-letters the job. Pending jobs with lost Redis hints can be re-enqueued.

Outbox delivery is **at least once**, not exactly once. Redis may accept a message even if the publisher cannot commit its database update; `ZADD NX` and conditional PostgreSQL claims make replay safe for queue processing. Fencing prevents a stale worker from committing a job result after losing its lease, but cannot undo an external side effect that already occurred. The email handler uses the job ID as a Resend idempotency key; provider behavior and retention still apply.

### Project structure

```text
.
├── app/
│   ├── api/routes/             # jobs, queue inspection, uploads, images
│   ├── core/config.py          # environment-backed settings
│   ├── db/database.py          # SQLAlchemy engine and sessions
│   ├── models/                 # jobs, outbox, workers, attempts, DLQ, images
│   ├── publisher/              # outbox publisher and CLI
│   ├── queue/                  # Redis client and sorted-set queue
│   ├── schemas/                # API response schemas
│   ├── tasks/                  # dispatcher, payload schemas, handlers
│   ├── worker/                 # claims, leases, retry, recovery, worker CLI
│   └── main.py                 # FastAPI app and health endpoint
├── alembic/                    # migrations and migration environment
├── benchmark-results/          # generated benchmark reports
├── docs/                       # benchmark methodology and report
├── frontend/                   # React/Vite dashboard and Nginx config
├── tests/                      # unit, integration, and opt-in benchmark tests
├── .env.example                # configuration template; no real secrets
├── Dockerfile
├── docker-compose.yml
├── alembic.ini
├── pytest.ini
└── requirements.txt
```

Generated files, local environment files, installed dependencies, and local storage are intentionally not shown.

## Prerequisites

For local development, install:

- Python 3.12.
- PostgreSQL and Redis.
- Git, when you are ready to publish the repository.

Docker Desktop with Docker Compose can run the services in containers instead of requiring local PostgreSQL and Redis. Node.js is only needed to work on the frontend outside Docker; the frontend Dockerfile builds it in a Node container.

## Installation and local setup

Run these commands from the repository root in PowerShell.

1. Create a local environment file and replace the example values. Do not commit `.env`.

   ```powershell
   Copy-Item .env.example .env
   ```

2. Create and activate a virtual environment, then install the project dependencies.

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Create a PostgreSQL role and database, if they do not already exist. Substitute a local password for the placeholder:

   ```sql
   CREATE ROLE queue_user WITH LOGIN PASSWORD 'replace-with-a-local-password';
   CREATE DATABASE distributed_queue_db OWNER queue_user;
   ```

4. Set `DATABASE_URL` in `.env` to the database you created. For example:

   ```text
   DATABASE_URL=postgresql+psycopg://queue_user:replace-with-a-local-password@localhost:5432/distributed_queue_db
   ```

   If the password contains reserved URL characters, percent-encode it in `DATABASE_URL`. Set `REDIS_URL` to your Redis instance; the local default is `redis://localhost:6379/0`.

5. Apply the database migrations:

   ```powershell
   alembic upgrade head
   alembic current
   ```

Migrations are not run automatically by API or worker startup. Apply them before submitting jobs.

### Configuration reference

`app/core/config.py` loads settings from environment variables and the repository-root `.env`. `DATABASE_URL` is required. The table lists setting defaults; `.env.example` uses safer or task-specific values where noted.

| Variable | Default | Purpose |
|---|---:|---|
| `DATABASE_URL` | Required | SQLAlchemy database URL. Use PostgreSQL for application operation. |
| `DEBUG` | `false` | Accepted setting; it is not currently wired to FastAPI's debug mode. |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL. |
| `REDIS_CONNECT_TIMEOUT_SECONDS` | `1.0` | Redis connection timeout. |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | `1.0` | Redis socket timeout. |
| `QUEUE_KEY` | `djq:queue:default` | Redis sorted-set key. |
| `ENABLE_QUEUE_INSPECTION` | `true` | Enables the unauthenticated read-only queue endpoint. The example file sets this to `false`. |
| `OUTBOX_BATCH_SIZE` | `100` | Outbox and reconciliation batch size. |
| `OUTBOX_POLL_INTERVAL_SECONDS` | `1.0` | Publisher loop interval. |
| `WORKER_POLL_INTERVAL_SECONDS` | `1.0` | Worker idle/unavailable queue wait. |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | `5.0` | Worker heartbeat interval. |
| `WORKER_HEARTBEAT_TIMEOUT_SECONDS` | `30.0` | Threshold used to mark workers stale; must be at least three heartbeat intervals. |
| `WORKER_LEASE_DURATION_SECONDS` | `60.0` | Job lease duration. |
| `WORKER_LEASE_RENEWAL_INTERVAL_SECONDS` | `10.0` | Lease renewal interval; must be no more than one third of the lease duration. |
| `DEFAULT_MAX_ATTEMPTS` | `3` | Maximum claims/attempts for newly submitted jobs. |
| `RETRY_BASE_DELAY_SECONDS` | `5.0` | Base retry delay. |
| `RETRY_MAX_DELAY_SECONDS` | `300.0` | Retry delay cap. |
| `RETRY_JITTER_RATIO` | `0.2` | Retry jitter ratio, from 0 to 1. |
| `RETRY_SCHEDULER_BATCH_SIZE` | `100` | Due-retry scheduler batch size. |
| `RESEND_API_KEY` | Unset | Optional provider key required by `send_email`. |
| `RESEND_FROM_EMAIL` | Unset | Verified sender address required by `send_email`. |
| `IMAGE_INPUT_DIR` | Unset | Required root directory for image input and uploaded originals. |
| `IMAGE_OUTPUT_DIR` | Unset | Required output directory for resized images. |
| `PDF_OUTPUT_DIR` | Unset | Required output directory for generated PDFs. |
| `MAX_IMAGE_UPLOAD_SIZE_BYTES` | `10485760` | Maximum uploaded image size (10 MiB). |
| `MAX_IMAGE_PIXELS` | `40000000` | Maximum image pixel count. |
| `TEST_DATABASE_URL` | Unset | Optional dedicated PostgreSQL integration-test database. |
| `REDIS_TEST_URL` | Unset | Optional dedicated Redis integration-test database; tests require database index 15. |

For Docker Compose, `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` are Compose inputs. The password is inserted into `DATABASE_URL` without URL encoding, so use URI-unreserved characters only: letters, digits, `.`, `_`, `~`, and `-`. Compose passes only variables explicitly listed in `docker-compose.yml` into containers; values present in `.env` are not automatically forwarded. The Compose frontend defaults to port `8080`, and `VITE_API_BASE_URL` defaults to `/api`.

### Optional task-handler configuration

`send_email` needs a Resend key and a sender address verified with the provider. Keep these values out of source control. A successful handler result means that the provider accepted the request, not that the email was delivered.

Image and PDF handlers require their corresponding output directories. Uploaded images are limited by `MAX_IMAGE_UPLOAD_SIZE_BYTES`; image dimensions are checked against `MAX_IMAGE_PIXELS`. Supported raster formats are JPEG, PNG, and WEBP, determined from file contents.

## Run the services

Start each process in a separate PowerShell terminal from the repository root. Start PostgreSQL and Redis first, and make sure migrations have been applied.

**API:**

```powershell
python -m uvicorn app.main:app --reload
```

**Outbox publisher:**

```powershell
python -m app.publisher.run_publisher --loop
```

Use `python -m app.publisher.run_publisher --once` to process one publisher batch. Publisher options include `--batch-size` and `--interval`.

**Worker:**

```powershell
python -m app.worker.runner
```

Use `python -m app.worker.runner --interval 0.5` to change the idle polling interval. Start additional worker processes in separate terminals to process jobs concurrently. Workers support Ctrl+C and, on Windows, Ctrl+Break for graceful shutdown.

The API health route is <http://127.0.0.1:8000/health>; interactive OpenAPI documentation is at <http://127.0.0.1:8000/docs>.

### Docker Compose

Copy `.env.example` to `.env`, set the Compose `POSTGRES_*` values and a URI-unreserved password, then build and start the database services. Apply migrations explicitly before starting application services:

```powershell
Copy-Item .env.example .env
# Edit .env before continuing.
docker compose build api publisher worker frontend
docker compose up -d postgres redis
docker compose run --rm --no-deps api alembic upgrade head
docker compose up -d api publisher worker frontend
```

The frontend is served at `http://localhost:8080` by default. PostgreSQL and Redis data use named volumes; `storage/` and `generated_pdfs/` are bind-mounted. Avoid `docker compose down -v` if you need to preserve the database or Redis volumes.

## API reference

All routes are unauthenticated. The interactive OpenAPI page at `/docs` also describes request validation.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Returns `{"status":"ok"}`. This is a process health response, not a PostgreSQL/Redis readiness check. |
| `POST` | `/jobs` | Create a job and its outbox event. |
| `GET` | `/jobs` | List jobs newest first; currently unpaginated. |
| `GET` | `/jobs/{job_id}` | Retrieve a job and its current status/result. |
| `GET` | `/jobs/{job_id}/output` | Serve the output file for a completed `resize_image` job. |
| `GET` | `/queue/jobs` | Optional read-only queue inspection; controlled by `ENABLE_QUEUE_INSPECTION`. |
| `POST` | `/uploads/images` | Upload and register a JPEG, PNG, or WEBP image. |
| `GET` | `/images` | List stored image metadata; accepts `limit` (1–200) and `offset` (0 or greater). |
| `GET` | `/images/{image_id}` | Retrieve stored image metadata. |
| `GET` | `/images/{image_id}/file` | Serve the stored original image. |

### Submit and retrieve a job

`POST /jobs` accepts a `task_type` string and a JSON `payload` object. It returns HTTP 201 with the generated ID and initial status:

```powershell
curl.exe -X POST http://127.0.0.1:8000/jobs `
  -H "Content-Type: application/json" `
  -d '{"task_type":"generate_pdf","payload":{"title":"Weekly report","sections":[{"heading":"Summary","paragraphs":["Queue processing is operating normally."]}]}}'
```

Example response:

```json
{
  "id": "d5cb4e91-34e9-4a80-b6fc-c1e514a5fdc9",
  "status": "pending"
}
```

Retrieve the job with `GET /jobs/{job_id}`. A successful response includes `id`, `task_type`, `payload`, `status`, `priority`, `attempts`, `max_attempts`, timestamps, `error_message`, and `result`. A malformed UUID or invalid request body returns HTTP 422; a well-formed but unknown job ID returns HTTP 404. Database errors are returned as server errors.

The optional `Idempotency-Key` header accepts 1–255 non-blank characters. Repeating the same key with the same task type and payload returns the existing job ID and current status without creating another job or outbox event. Reusing the key for a different request returns HTTP 409. Keys are unique across the jobs table; callers sharing an API should include their own client scope in key values.

### Queue inspection

`GET /queue/jobs?limit=50` accepts limits from 1 to 200. It returns job ID, task type, priority, the number of queued entries, and the requested limit. The result is a non-atomic snapshot, and malformed Redis entries are omitted from the list but remain part of the total count. Redis unavailability returns HTTP 503. Set `ENABLE_QUEUE_INSPECTION=false` to return HTTP 404.

This endpoint has no authentication. It does not expose payloads or Redis credentials and does not modify the queue, but still reveals queued job IDs and task types. Keep it disabled in any externally accessible deployment.

### Image upload and retrieval

Upload using `multipart/form-data` with a `file` field:

```powershell
curl.exe -X POST http://127.0.0.1:8000/uploads/images `
  -F "file=@.\photo.png"
```

The HTTP 201 response includes `id`, a server-generated `filename`, `input_path`, detected `format`, `size_bytes`, `width`, and `height`. The returned `id` can be used as the `image_id` input to a `resize_image` job. The original can be retrieved through `GET /images/{image_id}/file`. Invalid or unsupported image content returns a client error; size and pixel-limit violations return HTTP 413.

### Supported task handlers

The dispatcher currently registers the following `task_type` values. Other task names produce an `UNSUPPORTED_TASK` failure.

**`send_email`** — payload fields: `to` (one address or a list of 1–50 addresses), `subject`, `body`, and optional `body_type` (`text` or `html`). Subject and body must not be blank. Requires `RESEND_API_KEY` and `RESEND_FROM_EMAIL`.

```json
{
  "task_type": "send_email",
  "payload": {
    "to": "person@example.com",
    "subject": "Queue report",
    "body": "The report is ready.",
    "body_type": "text"
  }
}
```

**`resize_image`** — requires exactly one of `input_path` (resolved under `IMAGE_INPUT_DIR`) or `image_id`, plus `width` and/or `height`. By default it preserves aspect ratio and does not upscale. Optional fields include `keep_aspect_ratio`, `allow_upscale`, `output_filename`, and `output_format`. Input and output formats are JPEG, PNG, and WEBP.

```json
{
  "task_type": "resize_image",
  "payload": {
    "image_id": "a9af5099-e059-4c2a-a257-996718c0ec76",
    "width": 640,
    "keep_aspect_ratio": true
  }
}
```

**`generate_pdf`** — requires a non-blank `title` and 1–50 `sections`; sections may include a `heading` and paragraphs. Optional `output_filename` must be a simple filename. The total text limit is 100,000 characters.

```json
{
  "task_type": "generate_pdf",
  "payload": {
    "title": "Weekly report",
    "sections": [
      {
        "heading": "Summary",
        "paragraphs": ["Queue processing is operating normally."]
      }
    ]
  }
}
```

Successful handler results are stored in the job's `result` JSON. Resized-image jobs can be served with `/jobs/{job_id}/output`. PDF output paths are included in the result, but there is currently no PDF-serving API route.

## Job states, retries, and idempotency

The implemented job states are:

| State | Meaning |
|---|---|
| `pending` | Persisted and eligible for a worker claim. |
| `processing` | Claimed by a worker with an active lease. |
| `retry_scheduled` | Retryable failure recorded; waiting for its due time. |
| `completed` | Handler succeeded and its result was stored. |
| `failed` | Terminal failure or attempt limit reached; recorded in the dead-letter table. |

The worker's attempt count increases when it claims a job. New jobs use `DEFAULT_MAX_ATTEMPTS` (default 3). Retryable failures use capped exponential backoff and jitter, configured with `RETRY_BASE_DELAY_SECONDS`, `RETRY_MAX_DELAY_SECONDS`, and `RETRY_JITTER_RATIO`. Non-retryable failures are not retried. The read-only dead-letter CLI shows failed jobs and their attempt history:

```powershell
python -m app.worker.dlq --limit 50
python -m app.worker.dlq --job-id <job-uuid>
```

The CLI does not requeue jobs. Job submission idempotency is enforced by a unique database key and request-content comparison. The email handler sends a stable job-based Resend idempotency key. Image and PDF artifacts are stored under job-specific directories for worker execution and are validated before reuse on retry. External services without idempotency support can still perform an effect twice if a worker crashes after the effect but before committing the result.

## Testing

Run the ordinary suite from the repository root:

```powershell
python -m pytest
```

The regular unit/API tests use in-memory SQLite, fakeredis, temporary files, and mocked email-provider calls. They do not send email or require PostgreSQL/Redis. Tests marked `integration` require explicitly configured, dedicated external services; without the URLs, those tests skip by default. `pytest.ini` also defines a `performance` marker for the opt-in benchmark.

For a quick run that excludes external-service and performance tests:

```powershell
python -m pytest -m "not integration and not performance" -q
```

To run PostgreSQL and Redis integration tests, provision a separate PostgreSQL database named exactly `distributed_queue_test` and a Redis database at index 15. The test safety checks reject a test URL pointing at the configured application database or Redis database. Integration setup creates a unique PostgreSQL schema and uses uniquely prefixed Redis keys; it does not flush Redis.

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://queue_test:<url-encoded-password>@localhost:5432/distributed_queue_test"
$env:REDIS_TEST_URL = "redis://localhost:6379/15"
$env:REQUIRE_INTEGRATION = "1"
python -m pytest -m integration -q
```

Use only disposable test services. Do not copy production or development URLs into the test variables. With `REQUIRE_INTEGRATION=1`, unavailable integration services fail instead of silently skipping.

The performance test in `tests/test_performance_benchmark.py` is opt-in and uses the same dedicated integration resources. It runs real PDF generation and writes uniquely named reports to `benchmark-results/`; see [the benchmark report](docs/week7_performance_final_report.md) for methodology and caveats. Its local, in-process measurements are not production performance guarantees.

## Troubleshooting

| Symptom | Check |
|---|---|
| Settings validation reports missing `DATABASE_URL` | Create `.env` from `.env.example`, set `DATABASE_URL`, and run commands from the repository root. |
| Database connection fails | Start PostgreSQL, verify the database/user/password in `DATABASE_URL`, and apply `alembic upgrade head`. |
| Job remains `pending` | Confirm the publisher is running and can connect to Redis; a job is persisted before it is published. |
| Job stays `retry_scheduled` | Keep the publisher running; it also schedules and republishes due retries. Check publisher logs and retry settings. |
| Worker cannot claim a queued job | PostgreSQL is authoritative; the job may already be claimed, no longer pending, or missing. Check job status and worker logs. |
| Image or PDF task returns `MISSING_CONFIG` | Set the relevant handler key/directory in the process environment. Ensure output directories are writable. |
| Queue inspection returns 404 | Set `ENABLE_QUEUE_INSPECTION=true` only for a trusted local environment. |
| Integration tests skip or fail | Configure disposable `TEST_DATABASE_URL` and `REDIS_TEST_URL` with the required database names/index and use `REQUIRE_INTEGRATION=1` to require execution. |

## Security considerations

- The API has no authentication or authorization. Job listing and retrieval expose stored payloads and results. Do not put sensitive data in job payloads or expose the API publicly without adding access controls and appropriate operational protections.
- Queue inspection is unauthenticated and enabled by the application setting default. The example environment file disables it; explicitly keep it disabled for externally accessible services.
- Keep `.env`, Docker credentials, Resend keys, and real connection strings out of version control and logs. `.gitignore` excludes local `.env*` files other than `.env.example`.
- Uploaded image size, pixel count, file format, and input/output directory boundaries are validated. Keep the configured storage paths private and writable only by the application.
- Test external services must be disposable and separate from application data. The tests enforce dedicated PostgreSQL/Redis targets, but operators should still verify the URLs before enabling integration tests.
- A database lease fences stale job-result writes, but it does not roll back an already-triggered external side effect. Use provider idempotency or design task effects to be repeatable.
- Compose interpolates `POSTGRES_PASSWORD` directly into the database URL. Use URI-unreserved characters as noted in the configuration section, or change the Compose configuration to encode the value safely.

## Current limitations

- There is no API authentication, authorization, tenant isolation, or rate limiting.
- `GET /jobs` returns all jobs without pagination or filtering.
- `/health` returns a static process response; it does not verify PostgreSQL or Redis readiness.
- The dead-letter CLI is read-only; there is no requeue or administrative job-cancellation API.
- The queue and worker design demonstrates recovery but does not guarantee exactly-once external effects.
- `send_email` depends on Resend configuration and provider behavior. The API does not report delivery receipts.
- PDF files are written to configured storage, but only completed image outputs have a serving endpoint.
- There is no automated CI workflow or repository license file in the current project.
- The backend requirements are not pinned to a lockfile; repeatable dependency resolution is not guaranteed.

## Future improvements

- Add authentication, authorization, tenant-aware data access, and request throttling before any public deployment.
- Add paginated/filterable job listing and explicit operational actions with audit history.
- Add a health/readiness distinction that checks required dependencies.
- Define a supported security-reporting and contribution process before inviting external contributions.
- Add CI for unit tests, formatting/type checks, and explicitly provisioned integration services.
- Add dependency locking and a documented upgrade process.
- Expand operational metrics, structured tracing, and alerting for queue age, stale workers, retries, and dead letters.
- Add retention and cleanup policies for job history and generated artifacts.

## GitHub presentation

**Suggested repository name:** `distributed-job-queue-system`

**Suggested description:** `A Python job-processing system with FastAPI, PostgreSQL, a Redis transactional-outbox publisher, leased workers, retries, and recovery.`

**Suggested topics:** `distributed-systems`, `job-queue`, `python`, `fastapi`, `redis`, `postgresql`, `asynchronous-processing`, `backend-engineering`

No license is declared. Choose and add a license before accepting contributions or publishing the repository if you intend to grant reuse rights.

### Manual publishing commands

These are suggestions for you to run when ready; no Git commands were run as part of this documentation update. Create an empty GitHub repository named `distributed-job-queue-system` first. From this project directory, review the staged files carefully to ensure that no local environment or generated data is included:

```powershell
git init
git add .
git status --short
git diff --cached --check
git commit -m "Prepare distributed job queue system for publication" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git branch -M main
git remote add origin https://github.com/<YOUR-USERNAME>/distributed-job-queue-system.git
git push -u origin main
```

If this directory is already a Git repository, do not run `git init`; inspect the existing remotes and branch before staging or pushing.
