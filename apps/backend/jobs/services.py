import random
from datetime import timedelta
from uuid import uuid4

from django.db import connection, transaction
from django.db.models import DateTimeField, F, Func
from django.db.models.functions import Now

from .models import BackgroundTask as Task

CHANNEL = "background_task_channel"
READY = (Task.Status.PENDING, Task.Status.RETRY)


class ClockTimestamp(Func):
    function = "clock_timestamp"
    output_field = DateTimeField()


class LeaseLost(Exception):
    pass


def enqueue(task_type, key, payload, *, payload_version=1, priority=0,
            scheduled_at=None, lease_duration=timedelta(minutes=1), max_attempts=3):
    if not connection.in_atomic_block:
        raise RuntimeError("Enqueue requires the caller's transaction")
    task, created = Task.objects.get_or_create(
        task_type=task_type, idempotency_key=key, defaults={
            "payload": payload, "payload_version": payload_version, "priority": priority,
            "scheduled_at": scheduled_at if scheduled_at is not None else ClockTimestamp(),
            "lease_duration": lease_duration, "max_attempts": max_attempts,
        },
    )
    if not created and (task.payload != payload or task.payload_version != payload_version):
        raise ValueError("The task key already identifies a different payload")
    if created:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, '')", [CHANNEL])
        task.refresh_from_db()
    return task


def claim(task_types, *, task_ids=None):
    with transaction.atomic(durable=True):
        candidates = (Task.objects.select_for_update(skip_locked=True)
                .filter(task_type__in=task_types, status__in=READY, scheduled_at__lte=Now(),
                        attempt_count__lt=F("max_attempts")))
        if task_ids is not None:
            candidates = candidates.filter(pk__in=task_ids)
        task = candidates.order_by("-priority", "id").first()
        if task is None:
            return None
        Task.objects.filter(pk=task.pk).update(
            status=Task.Status.PROCESSING, lease_token=uuid4(),
            locked_until=ClockTimestamp() + F("lease_duration"), attempt_count=F("attempt_count") + 1,
            updated_at=ClockTimestamp(),
        )
        task.refresh_from_db()
        return task


def _owned_update(task, **changes):
    with transaction.atomic():
        # Acquire the row before sampling the database clock, even if the lock waits.
        Task.objects.select_for_update().filter(pk=task.pk).first()
        count = Task.objects.filter(
            pk=task.pk, status=Task.Status.PROCESSING, lease_token=task.lease_token,
            locked_until__gt=ClockTimestamp(),
        ).update(updated_at=ClockTimestamp(), **changes)
        if count != 1:
            raise LeaseLost(task.pk)


def renew(task):
    _owned_update(task, locked_until=ClockTimestamp() + F("lease_duration"))


def admit_external_call(task):
    with transaction.atomic():
        locked = Task.objects.select_for_update().get(pk=task.pk)
        if locked.admitted_at is not None:
            raise LeaseLost(task.pk)
        _owned_update(task, admitted_at=ClockTimestamp())


def finish(task, result=None, *, status=Task.Status.DONE):
    if status not in (Task.Status.DONE, Task.Status.FAILED, Task.Status.UNKNOWN):
        raise ValueError("Invalid terminal task status")
    _owned_update(task, status=status, result=result, locked_until=None, lease_token=None)


def _after_failure(task):
    if task.admitted_at is not None:
        return {"status": Task.Status.UNKNOWN}
    if task.attempt_count >= task.max_attempts:
        return {"status": Task.Status.DEAD, "result": {"reason": "attempt_budget_exhausted"}}
    delay = random.uniform(0, min(300, 2 ** min(task.attempt_count, 9)))
    return {"status": Task.Status.RETRY, "scheduled_at": ClockTimestamp() + timedelta(seconds=delay)}


def retry(task):
    with transaction.atomic():
        locked = Task.objects.select_for_update().get(pk=task.pk)
        _owned_update(task, locked_until=None, lease_token=None, **_after_failure(locked))


def recover_one(task_types):
    """Recover internal tasks only. External handlers must reconcile their domain records."""
    with transaction.atomic():
        task = (Task.objects.select_for_update(skip_locked=True)
                .filter(status=Task.Status.PROCESSING, task_type__in=task_types,
                        locked_until__lte=ClockTimestamp(), admitted_at__isnull=True)
                .order_by("locked_until", "id").first())
        if task is None:
            return False
        Task.objects.filter(pk=task.pk).update(
            locked_until=None, lease_token=None, updated_at=ClockTimestamp(), **_after_failure(task),
        )
        return True


def seconds_until_work(task_types):
    due_at = (Task.objects.filter(
        task_type__in=task_types, status__in=READY, attempt_count__lt=F("max_attempts"),
    ).order_by("scheduled_at").values_list("scheduled_at", flat=True).first())
    expired_at = (Task.objects.filter(
        task_type__in=task_types, status=Task.Status.PROCESSING, admitted_at__isnull=True,
    ).order_by("locked_until").values_list("locked_until", flat=True).first())
    deadlines = [deadline for deadline in (due_at, expired_at) if deadline is not None]
    if not deadlines:
        return 15
    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        now = cursor.fetchone()[0]
    return min(15, max(0.1, (min(deadlines) - now).total_seconds()))
