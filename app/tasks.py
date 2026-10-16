from datetime import timedelta
import time

from celery import shared_task
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Task, TaskStatus, DLQEvent
from app.celery_app import celery
from app.timing import now_utc, backoff_seconds

@shared_task(name="app.tasks.dlq_handler")
def dlq_handler(task_id: int, reason: str, error: str | None, payload: dict):
    """Persists DLQ events for auditability."""
    with SessionLocal() as db:
        db.add(DLQEvent(task_id=task_id, reason=reason, error=error, payload=payload))
        db.commit()
    return {"ok": True}

@shared_task(bind=True, name="app.tasks.run_task", max_retries=0)
def run_task(self, task_id: int):
    # load + mark STARTED
    with SessionLocal() as db:
        task = db.execute(select(Task).where(Task.id == task_id)).scalar_one_or_none()
        if not task:
            return {"ok": False, "error": "task not found"}

        payload = dict(task.payload)
        task.status = TaskStatus.STARTED
        task.started_at = now_utc()
        db.commit()

    try:
        # --- Demo-only hooks (remove later if you want) ---
        # 1) Simulate "hung" task
        if payload.get("sleep_sec"):
            time.sleep(int(payload["sleep_sec"]))

        # 2) Simulate failure
        if payload.get("fail"):
            raise RuntimeError("Simulated failure requested by payload")

        # --- Put real work here ---
        result = {"ok": True, "echo": payload}

        with SessionLocal() as db:
            t2 = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
            t2.status = TaskStatus.SUCCEEDED
            t2.finished_at = now_utc()
            t2.last_error = None
            db.commit()

        return result

    except Exception as e:
        err = str(e)

        with SessionLocal() as db:
            t2 = db.execute(select(Task).where(Task.id == task_id)).scalar_one()
            t2.attempts += 1
            t2.last_error = err

            # exceeded retry budget → DEAD + send to DLQ
            if t2.attempts > t2.max_retries:
                t2.status = TaskStatus.DEAD
                t2.finished_at = now_utc()
                db.commit()

                celery.send_task(
                    "app.tasks.dlq_handler",
                    args=[t2.id, "max_retries_exceeded", err, t2.payload],
                    queue="dlq",
                )
            else:
                # scheduler will retry later with backoff
                t2.status = TaskStatus.FAILED
                t2.run_at = now_utc() + timedelta(seconds=backoff_seconds(t2.attempts))
                db.commit()

        raise
