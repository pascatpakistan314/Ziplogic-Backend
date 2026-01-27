"""
Celery application for background job processing in SWE Agent
Handles long-running agent tasks asynchronously
"""

import os
from celery import Celery
from celery.signals import task_prerun, task_postrun, task_failure
from dotenv import load_dotenv
from pathlib import Path
import logging

# Load environment variables
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path, override=False)
else:
    load_dotenv(override=False)

logger = logging.getLogger(__name__)

# Redis configuration for broker and result backend
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)

# Create Celery app
celery_app = Celery(
    "swe_agent_tasks",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    include=["celery_tasks"]  # Import tasks module
)

# Celery configuration
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,  # 1 hour max per task
    task_soft_time_limit=3300,  # 55 minutes soft limit
    result_expires=86400,  # Results expire after 24 hours
    worker_prefetch_multiplier=1,  # Process one task at a time
    worker_max_tasks_per_child=50,  # Restart worker after 50 tasks to prevent memory leaks
)

# Signal handlers for logging
@task_prerun.connect
def task_prerun_handler(sender=None, task_id=None, task=None, args=None, kwargs=None, **extra):
    """Log when task starts"""
    logger.info(f"Task {task.name} [{task_id}] started")

@task_postrun.connect
def task_postrun_handler(sender=None, task_id=None, task=None, args=None, kwargs=None, retval=None, **extra):
    """Log when task completes"""
    logger.info(f"Task {task.name} [{task_id}] completed successfully")

@task_failure.connect
def task_failure_handler(sender=None, task_id=None, exception=None, traceback=None, **extra):
    """Log when task fails"""
    logger.error(f"Task [{task_id}] failed: {exception}")
    logger.error(f"Traceback: {traceback}")

if __name__ == "__main__":
    celery_app.start()
