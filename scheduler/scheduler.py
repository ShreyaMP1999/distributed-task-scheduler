import time
from datetime import timedelta

from sqlalchemy import select, and_, or_

from app.db import SessionLocal, engine, Base
from app.models import Task, TaskStatus, DLQEvent
from app.celery_app import celery
from app.timing import now_utc, backoff_seconds

from app.wait_for_db import wait_for_db
# ✅ Wait for Postgres to be ready before proceeding

POLL_INTERVAL_SEC = 1
STUCK_AFTER_SEC = 180   # 3 minutes
LIMIT = 200

def queue_for_priority(p: int) -> str:
    if p >= 8:
        return "high"
    if p >= 4:
        return "default"
    return "low"

def ensure_tables_exist():
    # Creates tasks + dlq_events if they don't exist yet
    Base.metadata.create_all(bind=engine)

def sweep_stuck_tasks():
    cutoff = now_utc() - timedelta(seconds=STUCK_AFTER_SEC)

    with SessionLocal() as db:
        stuck = db.execute(
            select(Task)
            .where(
                or_(
                    and_(Task.status == TaskStatus.STARTED,
                         Task.started_at.is_not(None), Task.started_at <= cutoff),
                    and_(Task.status == TaskStatus.ENQUEUED, Task.updated_at <= cutoff),
                )
            )
            .order_by(Task.started_at.asc())
            .limit(LIMIT)
            .with_for_update(skip_locked=True)
        ).scalars().all()

        for t in stuck:
            t.attempts += 1
            t.last_error = f"Stuck task detected (status={t.status.value})"

            if t.attempts > t.max_retries:
                t.status = TaskStatus.DEAD
                t.finished_at = now_utc()
                db.add(DLQEvent(task_id=t.id, reason="stuck_task_timeout",
                                error=t.last_error, payload=t.payload))
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
            .with_for_update(skip_locked=True)
        ).scalars().all()

        for t in due:
            q = queue_for_priority(t.priority)

            # Keep the row locked through publication. A worker claiming this row
            # waits until ENQUEUED commits; another scheduler skips the lock.
            t.status = TaskStatus.ENQUEUED
            t.updated_at = now_utc()
            db.flush()
            celery.send_task(
                "app.tasks.run_task",
                args=[t.id, t.attempts],
                queue=q,
            )

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
