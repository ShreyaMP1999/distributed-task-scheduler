from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select

from app.models import DLQEvent, Task, TaskStatus
from app.tasks import run_task
from scheduler.scheduler import dispatch_due_tasks, sweep_stuck_tasks
from tests.test_execution import DatabaseTestCase


class RecoveryTests(DatabaseTestCase):
    def events(self):
        with self.sessions() as db:
            return db.execute(select(DLQEvent)).scalars().all()

    def test_duplicate_delivery_does_not_execute_again(self):
        task_id = self.make_task(payload={"sleep_sec": 1})
        with patch("app.tasks.time.sleep") as sleep:
            run_task.run(task_id, 0)
            self.assertTrue(run_task.run(task_id, 0)["skipped"])
            sleep.assert_called_once()
        self.assertEqual(self.read_task(task_id).status, TaskStatus.SUCCEEDED)

    def test_old_delivery_cannot_claim_new_attempt(self):
        task_id = self.make_task(attempts=1)
        self.assertTrue(run_task.run(task_id, 0)["skipped"])
        self.assertEqual(self.read_task(task_id).status, TaskStatus.ENQUEUED)
        self.assertTrue(run_task.run(task_id, 1)["ok"])

    def test_terminal_failure_has_durable_dlq_event(self):
        task_id = self.make_task(payload={"fail": True}, max_retries=0)
        with patch("app.tasks.celery.send_task", side_effect=ConnectionError("broker down")):
            with self.assertRaisesRegex(RuntimeError, "Simulated failure"):
                run_task.run(task_id)
        self.assertEqual(self.read_task(task_id).status, TaskStatus.DEAD)
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reason, "max_retries_exceeded")
        self.assertTrue(run_task.run(task_id)["skipped"])
        self.assertEqual(len(self.events()), 1)

    def test_sweeper_recovers_unclaimed_and_started_tasks(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        queued = self.make_task(updated_at=old)
        dead = self.make_task(status=TaskStatus.STARTED, started_at=old, max_retries=0)
        fresh = self.make_task()
        with patch("scheduler.scheduler.SessionLocal", self.sessions):
            sweep_stuck_tasks()
            sweep_stuck_tasks()
        task = self.read_task(queued)
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.attempts, 1)
        self.assertGreater(task.run_at, datetime.now(timezone.utc).replace(tzinfo=None))
        self.assertEqual(self.read_task(dead).status, TaskStatus.DEAD)
        self.assertEqual(self.read_task(fresh).status, TaskStatus.ENQUEUED)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0].reason, "stuck_task_timeout")

    def test_stale_worker_cannot_overwrite_replacement(self):
        for fails in (False, True):
            task_id = self.make_task(payload={"sleep_sec": 1, "fail": fails})

            def revoke_attempt(_):
                with self.sessions() as db:
                    task = db.get(Task, task_id)
                    task.started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
                    db.commit()
                with patch("scheduler.scheduler.SessionLocal", self.sessions):
                    sweep_stuck_tasks()
                with self.sessions() as db:
                    task = db.get(Task, task_id)
                    task.status = TaskStatus.STARTED
                    task.started_at = datetime.now(timezone.utc)
                    db.commit()

            with patch("app.tasks.time.sleep", side_effect=revoke_attempt):
                if fails:
                    with self.assertRaises(RuntimeError):
                        run_task.run(task_id, 0)
                else:
                    self.assertTrue(run_task.run(task_id, 0)["skipped"])
            replacement = self.read_task(task_id)
            self.assertEqual(replacement.status, TaskStatus.STARTED)
            self.assertEqual(replacement.attempts, 1)
            self.assertEqual(len(self.events()), 0)

    def test_broker_failure_rolls_back_dispatch_for_retry(self):
        task_id = self.make_task(status=TaskStatus.PENDING)
        with patch("scheduler.scheduler.SessionLocal", self.sessions), \
             patch("scheduler.scheduler.celery.send_task", side_effect=ConnectionError("down")):
            with self.assertRaises(ConnectionError):
                dispatch_due_tasks()
        self.assertEqual(self.read_task(task_id).status, TaskStatus.PENDING)
        self.assertTrue(run_task.run(task_id, 0)["skipped"])
        with patch("scheduler.scheduler.SessionLocal", self.sessions), \
             patch("scheduler.scheduler.celery.send_task") as send:
            dispatch_due_tasks()
            send.assert_called_once_with("app.tasks.run_task", args=[task_id, 0], queue="default")
