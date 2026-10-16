import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Task, TaskStatus
from app.tasks import run_task


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{self.temp.name}/tasks.db")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.session_patch = patch("app.tasks.SessionLocal", self.sessions)
        self.session_patch.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.session_patch.stop)

    def make_task(self, **values):
        defaults = dict(name="test", payload={}, run_at=datetime.now(timezone.utc),
                        status=TaskStatus.ENQUEUED)
        defaults.update(values)
        with self.sessions() as db:
            task = Task(**defaults)
            db.add(task)
            db.commit()
            return task.id

    def read_task(self, task_id):
        with self.sessions() as db:
            return db.execute(select(Task).where(Task.id == task_id)).scalar_one()


class ExecutionTests(DatabaseTestCase):
    def test_worker_registers_handlers_without_api_import(self):
        subprocess.run([sys.executable, "-c", "from app.celery_app import celery; "
                        "celery.loader.import_default_modules(); "
                        "assert 'app.tasks.run_task' in celery.tasks; "
                        "assert 'app.tasks.dlq_handler' in celery.tasks"], check=True)

    def test_success_reads_payload_after_claim_session_closes(self):
        task_id = self.make_task(payload={"msg": "hello"})
        result = run_task.run(task_id)
        self.assertEqual(result, {"ok": True, "echo": {"msg": "hello"}})
        self.assertEqual(self.read_task(task_id).status, TaskStatus.SUCCEEDED)

    def test_failure_records_original_error(self):
        task_id = self.make_task(payload={"fail": True})
        with self.assertRaisesRegex(RuntimeError, "Simulated failure"):
            run_task.run(task_id)
        task = self.read_task(task_id)
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.attempts, 1)
        self.assertIn("Simulated failure", task.last_error)
