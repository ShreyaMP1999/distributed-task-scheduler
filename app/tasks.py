from datetime import timedelta
import time

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Task, TaskStatus, DLQEvent
from app.celery_app import celery
from app.timing import now_utc, backoff_seconds


@celery.task(name="app.tasks.dlq_handler")
def dlq_handler(task_id: int, reason: str, error: str | None, payload: dict):
    """Accepts legacy DLQ messages; new failures are audited transactionally."""
    with SessionLocal() as db:
        db.add(DLQEvent(task_id=task_id, reason=reason, error=error, payload=payload))
        db.commit()
    return {"ok": True}


@celery.task(bind=True, name="app.tasks.run_task", max_retries=0)
def run_task(self, task_id: int, expected_attempts: int = 0):
    # Only one delivery may claim this attempt. Old messages cannot claim a retry.
    with SessionLocal() as db:
        # Lock by id before checking state: a dispatcher may still be committing
        # ENQUEUED, which is invisible to a status-filtered UPDATE at this point.
        task = db.execute(
            select(Task).where(Task.id == task_id).with_for_update()
        ).scalar_one_or_none()
        if task is None or task.status != TaskStatus.ENQUEUED or task.attempts != expected_attempts:
            return {"ok": False, "skipped": True}
        payload = dict(task.payload)
        task.status = TaskStatus.STARTED
        task.started_at = now_utc()
        task.finished_at = None
        db.commit()

    try:
        # Demo hooks; replace with real work as needed.
        if payload.get("sleep_sec"):
            time.sleep(int(payload["sleep_sec"]))
        if payload.get("fail"):
            raise RuntimeError("Simulated failure requested by payload")

        result = {"ok": True, "echo": payload}
        with SessionLocal() as db:
            # The sweeper may have revoked this attempt while work was running.
            task = db.execute(
                select(Task).where(Task.id == task_id).with_for_update()
            ).scalar_one()
            if task.status != TaskStatus.STARTED or task.attempts != expected_attempts:
                return {"ok": False, "skipped": True}
            task.status = TaskStatus.SUCCEEDED
            task.finished_at = now_utc()
            task.last_error = None
            db.commit()
        return result

    except Exception as exc:
        err = str(exc)
        with SessionLocal() as db:
            task = db.execute(
                select(Task).where(Task.id == task_id).with_for_update()
            ).scalar_one()
            if task.status == TaskStatus.STARTED and task.attempts == expected_attempts:
                task.attempts += 1
                task.last_error = err
                if task.attempts > task.max_retries:
                    task.status = TaskStatus.DEAD
                    task.finished_at = now_utc()
                    db.add(DLQEvent(task_id=task.id, reason="max_retries_exceeded",
                                    error=err, payload=task.payload))
                else:
                    task.status = TaskStatus.FAILED
                    task.run_at = now_utc() + timedelta(seconds=backoff_seconds(task.attempts))
                db.commit()
        raise
