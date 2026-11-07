from datetime import timedelta

from django.db import models
from django.db.models import Q
from django.utils import timezone


class BackgroundTask(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        PROCESSING = "processing"
        RETRY = "retry_scheduled"
        DONE = "done"
        FAILED = "failed"
        UNKNOWN = "unknown_outcome"
        DEAD = "dead_letter"

    task_type = models.CharField(max_length=100)
    idempotency_key = models.UUIDField()
    payload = models.JSONField(default=dict)
    payload_version = models.PositiveSmallIntegerField(default=1)
    priority = models.IntegerField(default=0)
    scheduled_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=Status, default=Status.PENDING)
    lease_duration = models.DurationField(default=timedelta(minutes=1))
    lease_token = models.UUIDField(null=True, editable=False)
    locked_until = models.DateTimeField(null=True)
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)
    admitted_at = models.DateTimeField(null=True)
    result = models.JSONField(null=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "background_task"
        constraints = [
            models.UniqueConstraint(fields=("task_type", "idempotency_key"), name="job_type_key_unique"),
            models.CheckConstraint(condition=Q(max_attempts__gt=0), name="job_positive_attempt_limit"),
            models.CheckConstraint(condition=Q(lease_duration__gt=timedelta(0)), name="job_positive_lease"),
            models.CheckConstraint(
                condition=Q(status="processing", lease_token__isnull=False, locked_until__isnull=False)
                | (~Q(status="processing") & Q(lease_token__isnull=True, locked_until__isnull=True)),
                name="job_processing_has_lease",
            ),
        ]
        indexes = [
            models.Index(fields=("-priority", "id"), name="job_ready_order",
                         condition=Q(status__in=("pending", "retry_scheduled"))),
            models.Index(fields=("locked_until",), name="job_expired_lease",
                         condition=Q(status="processing")),
        ]
