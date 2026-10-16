"""Optional row-lock tests using a dedicated schema in TEST_DATABASE_URL."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import TaskStatus
from app.tasks import run_task
from scheduler.scheduler import dispatch_due_tasks
from tests.test_execution import DatabaseTestCase


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class PostgresConcurrencyTests(DatabaseTestCase):
    def setUp(self):
        url = os.environ["TEST_DATABASE_URL"]
        schema = "test_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        self.engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        self.sessions = sessionmaker(bind=self.engine)
        Base.metadata.create_all(self.engine)

        def cleanup():
            self.engine.dispose()
            with admin.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()

        self.addCleanup(cleanup)
        self.session_patch = patch("app.tasks.SessionLocal", self.sessions)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    def test_two_workers_execute_one_delivery_once(self):
        task_id = self.make_task(payload={"sleep_sec": 1})
        started, finish = Event(), Event()

        def work(_):
            started.set()
            self.assertTrue(finish.wait(5))

        with ThreadPoolExecutor(max_workers=2) as pool, patch("app.tasks.time.sleep", side_effect=work) as sleep:
            first = pool.submit(run_task.run, task_id, 0)
            try:
                self.assertTrue(started.wait(5))
                self.assertTrue(pool.submit(run_task.run, task_id, 0).result(5)["skipped"])
            finally:
                finish.set()
            self.assertTrue(first.result(5)["ok"])
            sleep.assert_called_once()

    def test_schedulers_skip_locked_rows_and_worker_waits_for_commit(self):
        task_id = self.make_task(status=TaskStatus.PENDING)
        publishing, finish = Event(), Event()

        def publish(*args, **kwargs):
            publishing.set()
            self.assertTrue(finish.wait(5))

        with ThreadPoolExecutor(max_workers=3) as pool, \
             patch("scheduler.scheduler.SessionLocal", self.sessions), \
             patch("scheduler.scheduler.celery.send_task", side_effect=publish) as send:
            first = pool.submit(dispatch_due_tasks)
            worker = None
            try:
                self.assertTrue(publishing.wait(5))
                pool.submit(dispatch_due_tasks).result(5)
                worker = pool.submit(run_task.run, task_id, 0)
                with self.assertRaises(TimeoutError):
                    worker.result(0.2)
            finally:
                finish.set()
            first.result(5)
            self.assertTrue(worker.result(5)["ok"])
            send.assert_called_once()
        self.assertEqual(self.read_task(task_id).status, TaskStatus.SUCCEEDED)
