import logging
from contextlib import contextmanager
from threading import Event, Thread

from django.db import OperationalError, connections, transaction

from .models import BackgroundTask as Task
from .services import LeaseLost, claim, finish, recover_one, renew, retry

logger = logging.getLogger(__name__)


@contextmanager
def heartbeat(task):
    stopped = Event()

    def maintain():
        try:
            while not stopped.wait(task.lease_duration.total_seconds() / 3):
                renew(task)
        except LeaseLost:
            logger.info("Task %s lost its lease during renewal", task.pk)
        except Exception:
            logger.exception("Lease renewal failed for task %s", task.pk)
        finally:
            connections.close_all()

    thread = Thread(target=maintain, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()


def run_one(handlers):
    """Run an internal handler, then persist its returned database operation atomically."""
    while recover_one(handlers):
        pass
    task = claim(handlers)
    if task is None:
        return False

    try:
        with heartbeat(task):
            save_result = handlers[task.task_type](task)
        with transaction.atomic():
            result = save_result()
            finish(task, result)
    except LeaseLost:
        logger.info("Task %s lost its lease", task.pk)
    except Exception as exc:
        logger.exception("Task %s failed", task.pk)
        try:
            if isinstance(exc, (OperationalError, OSError)):
                retry(task)
            else:
                finish(task, {"reason": "handler_error"}, status=Task.Status.FAILED)
        except LeaseLost:
            logger.info("Task %s lost its lease before recording failure", task.pk)
    return True
