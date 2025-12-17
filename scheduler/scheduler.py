import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, and_

from app.db import SessionLocal, engine, Base
from app.models import Task, TaskStatus
from app.celery_app import celery

from app.wait_for_db import wait_for_db
# ✅ Wait for Postgres to be ready before proceeding

POLL_INTERVAL_SEC = 1
STUCK_AFTER_SEC = 180   # 3 minutes
LIMIT = 200

def now_utc():
    return datetime.now(timezone.utc)

def queue_for_priority(p: int) -> str:
    if p >= 8:
        return "high"
    if p >= 4:
        return "default"
    return "low"

def backoff_seconds(attempts: int, base: int = 2, cap: int = 60) -> int:
    return min(cap, base * (2 ** max(0, attempts - 1)))

def ensure_tables_exist():
    # Creates tasks + dlq_events if they don't exist yet
    Base.metadata.create_all(bind=engine)

def sweep_stuck_tasks():
    cutoff = now_utc() - timedelta(seconds=STUCK_AFTER_SEC)

    with SessionLocal() as db:
        stuck = db.execute(
            select(Task)
            .where(
                and_(
                    Task.status == TaskStatus.STARTED,
                    Task.started_at.is_not(None),
                    Task.started_at <= cutoff,
                )
            )
            .order_by(Task.started_at.asc())
            .limit(LIMIT)
        ).scalars().all()

        for t in stuck:
            t.attempts += 1
            t.last_error = f"Stuck task detected (started_at={t.started_at.isoformat()})"

            if t.attempts > t.max_retries:
                t.status = TaskStatus.DEAD
                t.finished_at = now_utc()
                db.commit()

                celery.send_task(
                    "app.tasks.dlq_handler",
                    args=[t.id, "stuck_task_timeout", t.last_error, t.payload],
                    queue="dlq",
                )
            else:
                t.status = TaskStatus.FAILED
                t.run_at = now_utc() + timedelta(seconds=backoff_seconds(t.attempts))

        db.commit()

def dispatch_due_tasks():
    with SessionLocal() as db:
        due = db.execute(
            select(Task).where(
                and_(
                    Task.status.in_([TaskStatus.PENDING, TaskStatus.FAILED]),
                    Task.run_at <= now_utc(),
                )
            )
            .order_by(Task.priority.desc(), Task.run_at.asc())
            .limit(LIMIT)
        ).scalars().all()

        for t in due:
            q = queue_for_priority(t.priority)

            celery.send_task(
                "app.tasks.run_task",
                args=[t.id],
                queue=q,
            )

            t.status = TaskStatus.ENQUEUED

            if t.attempts > 0 and t.attempts <= t.max_retries:
                t.run_at = now_utc() + timedelta(seconds=backoff_seconds(t.attempts))

        db.commit()

def main():
    print("Scheduler started...")

    wait_for_db(max_seconds=60)

    # ✅ Ensure DB tables exist before any queries
    ensure_tables_exist()

    while True:
        try:
            sweep_stuck_tasks()
            dispatch_due_tasks()
        except Exception as e:
            print("Scheduler error:", e)

        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
