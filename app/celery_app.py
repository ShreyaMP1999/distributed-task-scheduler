from celery import Celery
from app.config import REDIS_URL

celery = Celery(
    "worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
)

celery.conf.broker_connection_retry_on_startup = True
