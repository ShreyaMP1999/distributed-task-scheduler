Distributed Task Scheduler (Fault-Tolerant) 🚀

A simple, clean distributed task scheduling system built with: Python + FastAPI + Celery + Redis + PostgreSQL + Docker Compose

This project demonstrates core distributed systems reliability patterns:
-Scheduling tasks to run at a specific time (run_at)
-Distributed execution via Celery workers
-Priority queues (high, default, low)
-Automatic retries with backoff
-Dead Letter Queue (DLQ) when retry budget is exhausted
-Stuck-task sweeper to recover from worker crashes / hung tasks

ARCHITECTURE OVERVIEW
Components
-FastAPI (api): Create tasks + check status
-PostgreSQL (postgres): Source of truth for task state + DLQ audit events
-Redis (redis): Message broker for Celery queues
-Scheduler (scheduler): Polls DB for due tasks → enqueues to Redis + sweeps stuck tasks
-Celery Workers
    worker_high: executes priority 8–10 tasks
    worker_default: executes priority 4–7 tasks
    worker_low: executes priority 1–3 tasks
    worker_dlq: consumes DLQ queue and writes DLQ audit events
Task Lifecycle
PENDING → ENQUEUED → STARTED → SUCCEEDED
failures: FAILED → (retry) → SUCCEEDED
exhausted retries: DEAD → DLQ

PROJECT STRUCTURE
distributed-task-scheduler/
  docker-compose.yml
  .env
  requirements.txt
  Dockerfile

  app/
    config.py
    db.py
    models.py
    schemas.py
    celery_app.py
    wait_for_db.py
    tasks.py
    main.py

  scheduler/
    scheduler.py

PREREQUISITES

Install:
-Docker Desktop
-Docker Compose (included in Docker Desktop) That’s it. You do not need Python installed locally.

QUICK START (Run from Scratch)

1) Clone and enter the project folder
    git clone <your-repo-url>
    cd distributed-task-scheduler

2) Create .env
Make sure you have a .env file in the project root:
    POSTGRES_USER=postgres
    POSTGRES_PASSWORD=postgres
    POSTGRES_DB=tasks
    DATABASE_URL=postgresql+psycopg2://postgres:postgres@postgres:5432/tasks
    REDIS_URL=redis://redis:6379/0

3) Start the system
    docker compose up --build
You should see:
Postgres: ready to accept connections
API: Application startup complete
Workers: celery@... ready
Scheduler: Scheduler started...

4) Open API Docs (Swagger)
Open in browser:
http://localhost:8000/docs
How to Use (Create + Track Tasks)
Create a task (Swagger UI)
Go to POST /tasks
Click Try it out
Use JSON like this:
✅ IMPORTANT: run_at must be in the future and in ISO 8601 format.
    Example (UTC):
        {
        "name": "demo_ok",
        "payload": { "msg": "hello" },
        "priority": 9,
        "run_at": "2025-12-17T19:55:00Z",
        "max_retries": 2
        }
Click Execute.
You will get a response with an id.
Check status
Go to GET /tasks/{task_id} in Swagger:
Enter the task id
Execute
Expected transitions:
Before run_at: PENDING
Around/after run_at: ENQUEUED → STARTED → SUCCEEDED

TESTING FAULT TOLERANCE

A) Retry + DLQ Test (simulate failure)
Create a task that fails:
{
  "name": "demo_fail",
  "payload": { "fail": true },
  "priority": 5,
  "run_at": "2025-12-17T19:56:00Z",
  "max_retries": 1
}
Expected:
It will fail, retry once, then move to:
DEAD
A DLQ event will be recorded in dlq_events

B) Stuck Task Sweeper Test (simulate “hang”)
Create a task that “hangs” by sleeping:
    {
    "name": "demo_hang",
    "payload": { "sleep_sec": 9999 },
    "priority": 5,
    "run_at": "2025-12-17T19:57:00Z",
    "max_retries": 1
    }
Then simulate worker crash:
    docker compose kill worker_default
    docker compose up -d worker_default
After STUCK_AFTER_SEC (default: 180s), scheduler will detect it as stuck and:
retry it if retries remain otherwise mark DEAD and push to DLQ

How to Inspect DLQ Events (Postgres)

1) Find container name
    docker compose ps
You’ll see something like:
    postgres-1

2) Query DLQ events
    docker exec -it postgres-1 psql -U postgres -d tasks \
  -c "select id, task_id, reason, created_at from dlq_events order by created_at desc limit 20;"
You should see reasons like:
max_retries_exceeded
stuck_task_timeout

VIEWING LOGS (Debugging)
Follow scheduler and high-priority worker logs
    docker compose logs -f scheduler worker_high
Follow all logs
    docker compose logs -f
Check if all services are up
    docker compose ps
