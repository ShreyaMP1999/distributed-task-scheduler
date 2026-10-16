import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pydantic import ValidationError

from app.schemas import TaskCreate
from app.tasks import run_task
from app.timing import backoff_seconds
from scheduler.scheduler import dispatch_due_tasks, queue_for_priority
from tests.test_execution import DatabaseTestCase


class SchedulingTests(DatabaseTestCase):
    def test_retry_is_not_dispatched_until_backoff_expires(self):
        now = datetime.now(timezone.utc)
        task_id = self.make_task(payload={"fail": True}, run_at=now - timedelta(days=1))
        with patch("app.tasks.now_utc", return_value=now):
            with self.assertRaises(RuntimeError):
                run_task.run(task_id)
        retry_at = self.read_task(task_id).run_at
        self.assertEqual(retry_at.replace(tzinfo=timezone.utc), now + timedelta(seconds=2))
        with patch("scheduler.scheduler.SessionLocal", self.sessions), \
             patch("scheduler.scheduler.celery.send_task") as send, \
             patch("scheduler.scheduler.now_utc", return_value=now):
            dispatch_due_tasks()
            send.assert_not_called()
        with patch("scheduler.scheduler.SessionLocal", self.sessions), \
             patch("scheduler.scheduler.celery.send_task") as send, \
             patch("scheduler.scheduler.now_utc", return_value=now + timedelta(seconds=2)):
            dispatch_due_tasks()
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.read_task(task_id).run_at, retry_at)

    def test_backoff_and_priority_boundaries(self):
        self.assertEqual([backoff_seconds(i) for i in range(1, 8)], [2, 4, 8, 16, 32, 60, 60])
        self.assertEqual([queue_for_priority(i) for i in (1, 3, 4, 7, 8, 10)],
                         ["low", "low", "default", "default", "high", "high"])

    def test_invalid_names_and_ambiguous_times_are_rejected(self):
        for values in ({"name": ""}, {"name": "x" * 121}, {"run_at": "2030-01-01T12:00:00"}):
            data = dict(name="demo", run_at="2030-01-01T12:00:00Z")
            data.update(values)
            with self.assertRaises(ValidationError):
                TaskCreate(**data)
        self.assertIsNotNone(TaskCreate(name="demo", run_at="2030-01-01T12:00:00+05:30").run_at.tzinfo)
