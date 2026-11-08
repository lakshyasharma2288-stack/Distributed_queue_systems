# Distributed Job Queue System

A beginner-friendly project to build a distributed job queue system in milestones.

## Current scope

Week 2 adds PostgreSQL-backed job submission and status retrieval. Week 3 adds standalone task handlers and a local dispatcher. Week 4 adds a PostgreSQL transactional outbox, a Redis sorted-set queue, a standalone outbox publisher, and a read-only queue inspection endpoint. Week 5 adds independent workers. Week 6 adds atomic multi-worker claims, heartbeats, leases and fencing, retries, attempt history, a dead-letter queue, crash recovery, queue reconciliation, and idempotent job submission and task effects.

## Project structure

```text
.
├── alembic/
│   ├── versions/
│   ├── env.py
│   └── script.py.mako
├── app/
│   ├── api/routes/
│   │   ├── jobs.py
│   │   └── queue.py
│   ├── core/config.py
│   ├── db/database.py
│   ├── models/
│   │   ├── job.py
│   │   ├── outbox_event.py
│   │   ├── worker.py
│   │   ├── attempt.py
│   │   └── dead_letter.py
│   ├── publisher/
│   │   ├── outbox_publisher.py
│   │   └── run_publisher.py
│   ├── worker/
│   │   ├── worker.py
│   │   ├── heartbeat.py
│   │   ├── leases.py
│   │   ├── retry_scheduler.py
│   │   ├── recovery.py
│   │   └── dlq.py
│   ├── queue/
│   │   ├── queue_manager.py
│   │   └── redis_client.py
│   ├── schemas/job.py
│   ├── tasks/
│   │   ├── dispatcher.py
│   │   ├── handlers/
│   │   └── schemas.py
│   └── main.py
├── tests/
│   ├── conftest.py
│   ├── test_health.py
│   ├── test_jobs.py
│   └── test_task_*.py
├── alembic.ini
├── .env.example
└── requirements.txt
```

- `app/db/` creates the SQLAlchemy engine, session factory, model base, and FastAPI session dependency.
- `app/models/` defines jobs, outbox events, worker heartbeats, job attempts, and dead letters.
- `app/schemas/` validates job requests and shapes API responses.
- `app/api/routes/` contains job endpoints and the local-only queue inspection endpoint.
- `app/queue/` manages Redis queue messages and read-only sorted-set inspection.
- `app/publisher/` publishes committed outbox events to Redis; it is a separate command, not part of API startup.
- `app/worker/` consumes queued job IDs, claims them atomically, renews leases, records attempts, schedules retries, and recovers interrupted work.
- `app/tasks/` contains Pydantic task payloads, result/error models, standalone handlers, and the dispatcher. It does not depend on FastAPI or database sessions.
- `alembic/` and `alembic.ini` configure and store database migrations.
- Unit tests use an isolated in-memory SQLite database and fakeredis. Integration tests use only `TEST_DATABASE_URL` and `REDIS_TEST_URL` when explicitly configured.

## Configure PostgreSQL

Install PostgreSQL locally, start its server, and create a database and login. For example, in `psql` as a PostgreSQL administrator:

```sql
CREATE ROLE queue_user WITH LOGIN PASSWORD 'choose-a-local-password';
CREATE DATABASE distributed_queue_db OWNER queue_user;
```

Copy `.env.example` to `.env`, then set `DATABASE_URL` to your local values:

```text
DATABASE_URL=postgresql+psycopg://queue_user:your-password@localhost:5432/job_queue
```

Use `distributed_queue_db` (or your chosen database name) consistently in both PostgreSQL and `DATABASE_URL`.
`DATABASE_URL` is required. URL-encode special characters in the password if necessary. `DEBUG` defaults to `false`. `REDIS_URL` configures the queue connection; Redis is connected lazily, so the API can start while Redis is unavailable. `QUEUE_KEY` selects the sorted-set key. Never commit `.env` or real credentials.

The optional `TEST_DATABASE_URL` must point to a dedicated PostgreSQL database named exactly `distributed_queue_test`, and it must not identify the same host, port, and database as `DATABASE_URL` (`localhost` and `127.0.0.1` are treated as the same host). The integration tests create and drop a unique schema there; they do not use the development database. `REDIS_TEST_URL` must point to the dedicated Redis database index `/15`; tests use and remove only uniquely prefixed test keys and never flush Redis. Leave either variable unset to skip the corresponding external-service tests during ordinary runs.

`ENABLE_QUEUE_INSPECTION` defaults to `true`; set it to `false` to disable the local debug endpoint. `OUTBOX_BATCH_SIZE` and `OUTBOX_POLL_INTERVAL_SECONDS` configure the standalone publisher. Worker heartbeat and lease behavior is controlled by `WORKER_HEARTBEAT_INTERVAL_SECONDS`, `WORKER_HEARTBEAT_TIMEOUT_SECONDS`, `WORKER_LEASE_DURATION_SECONDS`, and `WORKER_LEASE_RENEWAL_INTERVAL_SECONDS`. Retry behavior is controlled by `DEFAULT_MAX_ATTEMPTS`, `RETRY_BASE_DELAY_SECONDS`, `RETRY_MAX_DELAY_SECONDS`, `RETRY_JITTER_RATIO`, and `RETRY_SCHEDULER_BATCH_SIZE`. `WORKER_POLL_INTERVAL_SECONDS` controls how long the worker waits when the queue is empty or unavailable.

## Configure task handlers

The task handlers read their settings from the same `.env` file. Add values for the tasks you intend to run:

```text
RESEND_API_KEY=
RESEND_FROM_EMAIL=
IMAGE_INPUT_DIR=
IMAGE_OUTPUT_DIR=
PDF_OUTPUT_DIR=
MAX_IMAGE_PIXELS=40000000
```

Keep the API key empty in `.env.example`; put a real key only in your local `.env` or environment. In the Resend dashboard, verify a sending domain and use a sender address on that domain for `RESEND_FROM_EMAIL`. An email handler success means Resend **accepted the message request**; it does not mean the email was delivered.

`IMAGE_INPUT_DIR` is the only allowed image input tree. Relative input paths are resolved inside it, and absolute paths must also resolve inside it (including through symlinks). Image output and PDFs are written to their configured output directories, which are created if needed. Image input formats supported are JPEG, PNG, and WEBP, determined from the file contents rather than the filename. The default pixel limit is 40,000,000 pixels. Existing output files are never overwritten: choose a different `output_filename` or omit it to use a generated name. Output filenames are reduced to safe basenames; directory components are rejected. When a handler runs as a queued job, its outputs are isolated under a subdirectory named for the job ID; a retry validates and reuses an existing artifact.

## Install dependencies and apply migrations

From the project folder in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
alembic upgrade head
```

`alembic upgrade head` is required before calling `POST /jobs`; it creates all job-queue and Week 6 reliability tables and indexes. Apply migrations only to the database selected by `DATABASE_URL`. To see the current migration revision:

```powershell
alembic current
```

If your `public` schema appears empty in pgAdmin, run:

```powershell
alembic upgrade head
```

Then verify table creation:

```sql
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public';
```

When the model changes in a future step, create and review a migration before applying it:

```powershell
alembic revision --autogenerate -m "describe the schema change"
alembic upgrade head
```

## Run the application

With PostgreSQL running, `.env` configured, and the migration applied:

```powershell
python -m uvicorn app.main:app --reload
```

The health endpoint remains at <http://127.0.0.1:8000/health>. Interactive API documentation is available at <http://127.0.0.1:8000/docs>.

## Job API examples

Submit a job:

```http
POST /jobs
Content-Type: application/json

{"task_type":"send_email","payload":{"to":"person@example.com"}}
```

Successful response (HTTP 201):

```json
{"id":"d5cb4e91-34e9-4a80-b6fc-c1e514a5fdc9","status":"pending"}
```

Retrieve its status using the returned ID:

```http
GET /jobs/d5cb4e91-34e9-4a80-b6fc-c1e514a5fdc9
```

Successful response (HTTP 200; timestamp values are examples):

```json
{
  "id": "d5cb4e91-34e9-4a80-b6fc-c1e514a5fdc9",
  "task_type": "send_email",
  "payload": {"to": "person@example.com"},
  "status": "pending",
  "priority": 0,
  "attempts": 0,
  "max_attempts": 3,
  "created_at": "2026-01-01T12:00:00",
  "updated_at": "2026-01-01T12:00:00",
  "started_at": null,
  "completed_at": null,
  "error_message": null,
  "result": null
}
```

An invalid UUID or invalid request body returns HTTP 422. A valid UUID that is not in the database returns HTTP 404.

`POST /jobs` accepts an optional `Idempotency-Key` header containing 1–255 non-blank characters. Repeating the same key with the same task type and payload returns the original job ID and status without creating another job or outbox event. Reusing the key for a different request returns HTTP 409. Keys are unique across the job table and should be scoped by the caller when multiple clients share this API.

```http
POST /jobs
Idempotency-Key: weekly-report-2026-11
Content-Type: application/json

{"task_type":"generate_pdf","payload":{"title":"Weekly report","sections":[{"paragraphs":["Queue status is healthy."]}]}}
```

## Transactional outbox and queue inspection

`POST /jobs` keeps its existing 201 response. It writes the job and its `job_created` outbox event in one PostgreSQL transaction; it does not connect to Redis. Redis being unavailable therefore does not change successful job creation. The queue stays empty until the standalone publisher runs.

The publisher can be run once with the exact command below. It reads pending outbox events, uses `ZADD` to publish each minimal message to the configured sorted set, and marks the event published after Redis accepts it:

```powershell
python -m app.publisher.run_publisher --once
```

For continuous polling during local development, use:

```powershell
python -m app.publisher.run_publisher --loop
```

The publisher does not invoke task handlers. Workers use PostgreSQL as the source of truth and atomically move eligible jobs from `pending` to `processing` before dispatching the task.

Outbox publication has **at-least-once** semantics: if Redis accepts a message but the publisher cannot commit the outbox update, the event remains pending and can be published again. The queue uses `ZADD NX` so replaying the same message does not create another member. Redis outages leave events pending for a later publisher run; malformed events are marked `failed` and require manual review.

Jobs created before the outbox migration are not backfilled and have no corresponding outbox event, so they will not be published by the Week 4 publisher. The worker uses PostgreSQL as the source of truth and skips missing jobs and jobs that are not `pending`.

### Run the independent worker

Start the API and publisher as described above. To run multiple independent workers, open a separate PowerShell terminal for each process and run:

```powershell
python -m app.worker.runner
```

For example, start this command in three separate terminals. Each process receives a unique worker ID containing its host, process ID, and a random suffix; the worker logs its startup and claimed jobs.

The worker can also be started with an explicit empty-queue polling interval:

```powershell
python -m app.worker.runner --interval 0.5
```

The worker uses atomic Redis `ZPOPMIN` and a conditional PostgreSQL claim, so only one worker can claim a job. Each process sends heartbeats independently of task execution. A renewable lease and fencing version prevent an expired worker from committing a stale result. The worker responds to Ctrl+C (`SIGINT`) and, on Windows, Ctrl+Break (`SIGBREAK`).

Run the publisher continuously alongside the API and workers. Each publisher batch publishes outbox events, recovers expired or abandoned `processing` jobs, publishes due retries, and reconciles `pending` jobs whose Redis hint was lost. Retryable failures use capped exponential backoff with jitter and the configured maximum attempt count. Exhausted jobs become `failed` and are recorded in the dead-letter table. PostgreSQL remains the source of truth; Redis messages are delivery hints and can be recreated.

Database fencing cannot undo an external side effect already started by an old worker. Email requests use the stable job ID as the Resend idempotency key (subject to the provider's idempotency retention and semantics). PDF and image outputs are job-scoped and validated before a retry reuses them. External providers without idempotency support can still have a duplicate-effect window if a worker crashes after performing the effect but before committing the job result.

Inspect the dead-letter queue and attempt history without changing them:

```powershell
python -m app.worker.dlq --limit 50
python -m app.worker.dlq --job-id <job-uuid>
```

The CLI is read-only. Requeueing a dead-lettered job is not currently supported.

### Check the queue from Swagger

1. Start the API as described above and open <http://127.0.0.1:8000/docs>.
2. Expand `POST /jobs`, select **Try it out**, and submit a job such as:

   ```json
   {
     "task_type": "send_email",
     "payload": {"to": "person@example.com"}
   }
   ```

3. Copy the `id` from the 201 response.
4. Before running the publisher, call `GET /queue/jobs`. With an initially empty Redis queue, the response is:

   ```json
   {"queue": [], "total_queued": 0, "limit": 50}
   ```

   The job is persisted in PostgreSQL with a pending outbox event; it has not yet been published to Redis.
5. In a terminal using the same `.env`, run `python -m app.publisher.run_publisher --once`.
6. Call `GET /queue/jobs` again. The response includes the copied `job_id`, its `task_type`, and `priority`:

   ```json
   {
     "queue": [
       {"job_id": "<copied-job-id>", "task_type": "send_email", "priority": 0}
     ],
     "total_queued": 1,
     "limit": 50
   }
   ```

`GET /queue/jobs?limit=50` accepts `limit` values from 1 through 200; invalid limits return 422. `total_queued` is the full Redis sorted-set length and may be larger than the returned list. The endpoint calls `peek` and `length` separately, so the response is a non-atomic debugging snapshot. Invalid queue members are skipped with a warning and still count toward `total_queued`. Redis unavailability returns HTTP 503 with `{"detail":"Queue unavailable"}`.

For an independent Redis check, run:

```powershell
redis-cli ZCARD djq:queue:default
redis-cli ZRANGE djq:queue:default 0 -1
```

Memurai installations may provide the compatible `memurai-cli` command; use it in place of `redis-cli` if that is what your installation provides. These CLI commands have not been independently verified here.

**Local-development safety:** this endpoint has no authentication and exposes queue contents, although it omits payloads and Redis connection details. It is inspection-only: it does not pop, consume, execute, enqueue, or otherwise change the queue. `ENABLE_QUEUE_INSPECTION=false` makes it return 404. Do not deploy it publicly without adding authentication or disabling it.

### Verify PDF generation end to end

1. Configure `PDF_OUTPUT_DIR` in your local `.env`, apply migrations with `alembic upgrade head`, and start the API, publisher (`python -m app.publisher.run_publisher --loop`), and at least one worker (`python -m app.worker.runner`).
2. In Swagger, submit this exact task payload to `POST /jobs`:

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

3. Keep the returned job ID. Confirm the publisher and worker logs show publication, claim, and `generate_pdf` execution.
4. Poll `GET /jobs/{job_id}` until `status` is `completed`. In `result.data.output_path`, confirm the PDF exists under `PDF_OUTPUT_DIR\<job-id>\`. In PowerShell, run `Test-Path "<output_path>"` and then `Start-Process "<output_path>"` to verify it opens.

## Run task handlers manually

The worker calls the dispatcher after loading a pending job from PostgreSQL and supplies its job ID for job-aware idempotency. You can still call the dispatcher manually for development; it validates a raw dictionary with its task-specific Pydantic model and returns a `HandlerResult` without changing a job record. Start Python from the project directory with `.env` configured (`DATABASE_URL` remains required by the existing settings module):

```powershell
python
```

Then call the dispatcher with one of these payloads:

```python
from app.tasks.dispatcher import dispatch

email_result = dispatch(
    "send_email",
    {
        "to": "person@example.com",
        "subject": "Queue report",
        "body": "The report is ready.",
        "body_type": "text",
    },
)
image_result = dispatch(
    "resize_image",
    {
        "input_path": "source.png",
        "width": 640,
        "height": 480,
        "keep_aspect_ratio": True,
    },
)
pdf_result = dispatch(
    "generate_pdf",
    {
        "title": "Weekly report",
        "sections": [
            {
                "heading": "Summary",
                "paragraphs": ["Queue processing is operating normally."],
            }
        ],
    },
)

print(email_result.model_dump(mode="json"))
print(image_result.model_dump(mode="json"))
print(pdf_result.model_dump(mode="json"))
```

Successful results have `success: true`, the task type, and task-specific `data`. Typical `data` fields are:

```text
send_email:   {"provider_message_id": "<Resend message ID>"}
resize_image: {"output_path": "...", "original_dimensions": {...},
               "new_dimensions": {...}, "format": "PNG"}
generate_pdf: {"output_path": "...", "file_size": 1234}
```

Failures use `success: false` and an `error` object. For example, an unconfigured email task returns `MISSING_CONFIG`, an invalid email payload returns `VALIDATION_ERROR`, and an image input outside `IMAGE_INPUT_DIR` is rejected with `VALIDATION_ERROR`. Unsupported task names return `UNSUPPORTED_TASK`; unexpected handler exceptions become `INTERNAL_ERROR`. Retryable failures are scheduled by the worker and later republished by the publisher. Each claim and final outcome is recorded in `job_attempts`; terminal failures are also recorded in `dead_letters`.

The API response shape and JSON body remain unchanged; `Idempotency-Key` is an optional header. The worker dispatches using the persisted job ID so task handlers can apply job-aware idempotency.

## Run tests

```powershell
python -m pytest
```

## Run an opt-in performance benchmark

The Week 7 benchmark is kept separate from normal tests and requires the dedicated PostgreSQL and Redis integration resources. Configure `TEST_DATABASE_URL` for the separate `distributed_queue_test` database and `REDIS_TEST_URL` for Redis DB 15 in the current PowerShell session. The existing safety checks reject targets matching the configured application database or Redis database. Do not copy production/development URLs to test variables or paste credentials into reports or chat.

Run one scenario at a time; each run is limited to 100, 500, or 1,000 jobs and 1, 2, or 4 worker instances. For example:

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://<user>:<password>@<host>:5432/distributed_queue_test"
$env:REDIS_TEST_URL = "redis://<host>:6379/15"
$env:REQUIRE_INTEGRATION = "1"
$env:WEEK7_RUN = "1"
.\.venv\Scripts\python.exe -m pytest -m performance -q tests\test_performance_benchmark.py
```

The benchmark runs one job-count/worker-count pair per invocation. To run the full matrix serially:

```powershell
foreach ($jobs in @(100, 500, 1000)) {
    foreach ($workers in @(1, 2, 4)) {
        $env:WEEK7_JOB_COUNT = "$jobs"
        $env:WEEK7_WORKER_COUNT = "$workers"
        .\.venv\Scripts\python.exe -m pytest -m performance -q tests\test_performance_benchmark.py
        if ($LASTEXITCODE -ne 0) { throw "Benchmark failed for $jobs jobs / $workers workers" }
    }
}
```

Each scenario submits the same `generate_pdf` task type and payload through FastAPI `TestClient`, runs the existing outbox publisher and Redis queue against the isolated services, and runs `Worker` instances as threads. The real PDF handler writes each job's output under a temporary directory, which is removed after workers stop. Reports include jobs completed per second, successful-job completion average/P95 latency, failures, verified PDF count, handler execution time, worker claim/result-update/total time, publisher, Redis enqueue/dequeue, and queue-wait durations. Percentiles are included where sample counts support them. Four lifecycle spans from existing PostgreSQL timestamps are also reported: job creation-to-start, start-to-completion, creation-to-completion, and attempt start-to-end. These intervals use timestamps from the same database clock; they do not represent exact transaction commit time.

Same-process spans use `time.perf_counter()`. The benchmark does not subtract database timestamps from `OutboxEvent.published_at`, which is set using the application clock. Submission-to-Redis-enqueue timing instead uses `perf_counter()` values collected by the benchmark process. End-to-end completion is observed by database polling, so it includes a small polling observation delay.

Each run uses its own temporary PostgreSQL schema, Redis queue key, and PDF output directory, and removes only those resources. JSON and Markdown reports are created with unique names in `benchmark-results`; existing reports are not overwritten. This local in-process benchmark excludes real HTTP socket/server overhead and process startup. CPU and memory sampling are not provided.

The unit tests use temporary directories, programmatically generated images/PDFs, an isolated in-memory SQLite database, and fakeredis. Email-provider calls are mocked; tests do not send email. PostgreSQL/Redis integration tests are marked `integration`. The PostgreSQL fixture validates the dedicated database URL before connecting and runs migrations in a unique schema. Redis integration tests require index 15, verify that `REDIS_TEST_URL` is not the same endpoint/database as `REDIS_URL`, and only remove their own uniquely prefixed keys. The process-level end-to-end test uses those dedicated services and skips unless both integration URLs are configured.

For ordinary runs, unset integration URLs skip their tests. To require every integration test to run, configure both URLs in the current PowerShell session and enable strict mode:

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://TEST_USER:TEST_PASSWORD@localhost:5432/distributed_queue_test"
$env:REDIS_TEST_URL = "redis://localhost:6379/15"
$env:REQUIRE_INTEGRATION = "1"
python -m pytest -q
```

In strict mode, a missing or unreachable PostgreSQL/Redis integration dependency fails the test instead of silently skipping it. To run only tests marked `integration` after configuration, use:

```powershell
python -m pytest -m integration -q
```

The PostgreSQL-to-Redis end-to-end path is only verified when its integration test runs against those dedicated services; skipped integration tests are not evidence of an end-to-end pass. Do not copy `DATABASE_URL` into `TEST_DATABASE_URL` or `REDIS_URL` into `REDIS_TEST_URL`; use the dedicated `distributed_queue_test` database and Redis database index 15.

When the project virtual environment is available but integration URLs are not configured, run the safe non-integration regression suite with:

```powershell
.\.venv\Scripts\python.exe -m pytest -m 'not integration' -q
```

## Docker Compose startup

Set `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` in the environment used by Docker Compose. `POSTGRES_PASSWORD` is inserted directly into `DATABASE_URL`, so use a strong password containing only URI-unreserved characters: ASCII letters, digits, `.`, `_`, `~`, and `-`. Do not put credentials in the Dockerfiles or build arguments.

Build the images, start PostgreSQL and Redis first, apply Alembic migrations manually to the new Compose database, and only then start the application services:

```powershell
docker compose build api publisher worker frontend
docker compose up -d postgres redis
docker compose run --rm --no-deps api alembic upgrade head
docker compose up -d api publisher worker frontend
```

Migrations are never run automatically during container startup. The Compose PostgreSQL service uses its own persistent volume and does not import the existing local database. The existing `storage/` and `generated_pdfs/` directories are mounted into the API and worker containers; do not remove or recreate those directories when preserving their files. Avoid `docker compose down -v` when you need to retain database or Redis volumes.
