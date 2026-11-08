"""Celery application wiring - configuration only.

This module must stay free of psycopg2 / MinIO / business logic so the API
can import it (to call `send_task`) without pulling in worker-side
dependencies or opening a database connection at import time.
"""
import os

from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1")

celery_app = Celery("pipeline", broker=REDIS_URL, backend=RESULT_BACKEND, include=["tasks"])

celery_app.conf.update(
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=86400,
    broker_connection_retry_on_startup=True,
    # Reliability: a task whose worker dies mid-run is redelivered instead of
    # being lost, and each worker takes one task at a time.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
)
