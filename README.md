# Distributed Task Scheduler

A distributed task scheduler built with **FastAPI, Celery, Redis, PostgreSQL, and Docker Compose**. Submit tasks through an HTTP API, schedule them for a specific time, and track their execution across priority queues.

This project demonstrates retries with exponential backoff, recovery after worker failures, and durable dead-letter audit records. The task handler currently echoes its payload and provides optional failure and sleep hooks for demonstrations.

## Architecture

```text
Client → FastAPI → PostgreSQL ← Scheduler → Redis → Celery workers
                       ↑                               │
                       └──── task status + DLQ audit ───┘
```

| Component | Responsibility |
| --- | --- |
| `api` | Create tasks and retrieve their current status. |
| `postgres` | Store task state and dead-letter audit events. |
| `redis` | Broker Celery messages and store Celery results. |
| `scheduler` | Poll for due tasks every second, dispatch them, and recover stuck tasks. |
| `worker_high` | Process priorities 8–10 with concurrency 4. |
| `worker_default` | Process priorities 4–7 with concurrency 2. |
| `worker_low` | Process priorities 1–3 with concurrency 1. |
| `worker_dlq` | Consume legacy DLQ messages. New terminal failures save audit records directly in PostgreSQL. |

Priority selects a queue; it does not guarantee execution order across workers or preempt running tasks.

## Quick start

Install Docker with Docker Compose and ensure Docker is running. Local Python is only needed if you want to run the regression tests outside Docker.

### 1. Clone the repository

```sh
git clone https://github.com/ShreyaMP1999/distributed-task-scheduler.git
cd distributed-task-scheduler
```

### 2. Configure the environment

Create or update `.env` in the repository root with these local development values:

```dotenv
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB=tasks
DATABASE_URL=postgresql+psycopg2://postgres:postgres@postgres:5432/tasks
REDIS_URL=redis://redis:6379/0
```

The hostnames `postgres` and `redis` refer to services inside Compose. When running Python directly on your host, use `localhost` in the corresponding URLs instead. The Python configuration reads environment variables; it does not automatically load `.env` outside Compose.

### 3. Start the services

```sh
docker compose up --build
```

The API and scheduler wait for PostgreSQL and create the tables if needed. Check the services and logs in a second terminal:

```sh
docker compose ps
docker compose logs -f api scheduler worker_default
```

Open [interactive API documentation](http://localhost:8000/docs). Compose exposes the API on port `8000`, PostgreSQL on `5432`, and Redis on `6379`; these ports must be available on your host.

## Create and track tasks

### Create a task

Use `POST /tasks` in Swagger UI, or run:

```sh
curl -X POST http://localhost:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "demo_ok",
    "payload": {"msg": "hello"},
    "priority": 9,
    "run_at": "2000-01-01T00:00:00Z",
    "max_retries": 2
  }'
```

The examples intentionally use a past timestamp so they become due immediately, regardless of when you try them. To schedule a task for later, replace `run_at` with your desired future timestamp.

| Field | Accepted value | Default |
| --- | --- | --- |
| `name` | String containing 1–120 characters | Required |
| `payload` | JSON object passed to the handler | `{}` |
| `priority` | Integer from 1 to 10 | `5` |
| `run_at` | ISO 8601 datetime with a timezone, such as `Z` or `+05:30` | Required |
| `max_retries` | Integer from 0 to 20, counting retries after the first attempt | `3` |

A timestamp without a timezone is rejected. Past timestamps are accepted and become eligible on the next scheduler poll.

### Check task status

The response includes the task's `id`. Substitute that value below:

```sh
curl http://localhost:8000/tasks/1
```

`GET /tasks/{task_id}` returns the task state, payload, retry count (`attempts`), next scheduled time (`run_at`), and last error. An unknown ID returns HTTP `404`.

```text
PENDING → ENQUEUED → STARTED → SUCCEEDED
                        │
                        ├─ failure, retries remain → FAILED → ENQUEUED → STARTED
                        └─ retry budget exhausted → DEAD + DLQ audit event
```

`attempts` counts failed or timed-out attempts, not total executions. A task with `max_retries: 1` can execute up to twice. Retries wait 2, 4, 8, 16, 32, then 60 seconds, with subsequent delays capped at 60 seconds. The scheduler may dispatch later depending on polling and service availability.

## Try failure recovery

### Retry and dead-letter audit

```sh
curl -X POST http://localhost:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "demo_fail",
    "payload": {"fail": true},
    "priority": 5,
    "run_at": "2000-01-01T00:00:00Z",
    "max_retries": 1
  }'
```

The handler fails, retries once after the backoff delay, then enters `DEAD` with `attempts: 2`. The terminal state and a `max_retries_exceeded` audit event are committed together in PostgreSQL, without requiring Redis to be available for the audit write.

### Worker crash or timeout

Create a task that sleeps longer than the recovery timeout:

```sh
curl -X POST http://localhost:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "demo_hang",
    "payload": {"sleep_sec": 9999},
    "priority": 5,
    "run_at": "2000-01-01T00:00:00Z",
    "max_retries": 1
  }'
```

Wait until its status is `STARTED`, then simulate a worker crash:

```sh
docker compose kill worker_default
docker compose up -d worker_default
```

The scheduler recovers tasks that have remained `STARTED` or `ENQUEUED` for at least 180 seconds. Recovery consumes one failed attempt and schedules a retry with backoff, or marks the task `DEAD` and saves a `stuck_task_timeout` audit event when its retry budget is exhausted. The sleep hook runs again on every retry, so this example eventually becomes `DEAD`.

The poll interval, recovery timeout, and batch size are constants in `scheduler/scheduler.py`: `POLL_INTERVAL_SEC = 1`, `STUCK_AFTER_SEC = 180`, and `LIMIT = 200`.

### Inspect dead-letter events

Use the Compose service name instead of guessing a container name. With the example database credentials:

```sh
docker compose exec postgres psql -U postgres -d tasks \
  -c 'SELECT id, task_id, reason, error, created_at FROM dlq_events ORDER BY created_at DESC LIMIT 20;'
```

## Reliability behavior and limits

- PostgreSQL row locks prevent concurrent schedulers from dispatching the same row and concurrent workers from claiming the same attempt. Workers wait for the dispatch transaction to commit before checking task state.
- Each dispatched message includes its attempt's failure count. Duplicate deliveries and delayed messages cannot claim a newer retry. A stale worker cannot overwrite the replacement attempt's database state.
- A failed publication rolls back the dispatch transaction so a later scheduler poll can try again. The sweeper recovers queued messages that never start.
- Execution is **at least once**. Recovery does not stop code that is already running, so real handlers must make external side effects idempotent. This project does not guarantee exactly-once execution.
- A queue backlog longer than 180 seconds also consumes retry budget. Adjust the timeout and worker capacity for your workload.
- Deploy scheduler and worker changes together. Old messages lack an attempt argument and can only claim an initial attempt; queued retries from an older scheduler recover through the sweeper.

The Compose setup is intended for local development. PostgreSQL has no named volume configured; removing its container can remove its data. Keep backups or configure persistent storage before relying on it for durable deployments.

## Run regression tests

Use Python 3.11 or later:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -v
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1` in PowerShell.

The default suite uses temporary SQLite databases and mocks broker publication. The two PostgreSQL concurrency tests are skipped unless `TEST_DATABASE_URL` is set. To run them against the Compose PostgreSQL service from your host:

```sh
TEST_DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/tasks \
  python -m unittest discover -v
```

Each PostgreSQL test creates and removes its own schema. The database user needs permission to create schemas. GitHub Actions runs the suite with PostgreSQL 16 and Python 3.11 on pushes and pull requests.

These tests cover handler registration, payload access, retry timing, request validation, duplicate deliveries, stale workers, stuck tasks, publication failures, DLQ persistence, and concurrent row locking. They do not exercise a live Redis broker or the full Compose stack.

## Project structure

```text
distributed-task-scheduler/
├── .github/workflows/tests.yml  # Regression tests with PostgreSQL
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── app/
│   ├── celery_app.py            # Celery configuration and handler imports
│   ├── config.py                # Database and Redis URLs
│   ├── db.py                    # SQLAlchemy engine and sessions
│   ├── main.py                  # FastAPI endpoints
│   ├── models.py                # Tasks and DLQ audit records
│   ├── schemas.py               # Request and response validation
│   ├── tasks.py                 # Execution, retries, and terminal failures
│   ├── timing.py                # UTC time and retry backoff
│   └── wait_for_db.py           # Database readiness polling
├── scheduler/
│   └── scheduler.py            # Dispatch and timeout recovery
└── tests/                      # Regression and concurrency tests
```

## Useful commands

```sh
# Follow all service logs
docker compose logs -f

# Follow scheduler and high-priority worker logs
docker compose logs -f scheduler worker_high

# Stop services while keeping their containers
docker compose stop

# Start existing containers again
docker compose start
```
