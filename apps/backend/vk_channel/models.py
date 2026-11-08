from uuid import uuid4

from django.db import models
from django.db.models import Q
from django.utils import timezone


class LongPollCursor(models.Model):
    community_id = models.PositiveBigIntegerField(unique=True)
    position = models.CharField(max_length=128)
    lease_token = models.UUIDField(null=True)
    leased_until = models.DateTimeField(null=True)
    last_batch_at = models.DateTimeField(null=True)
    receipt_version = models.PositiveBigIntegerField(default=0)
    period = models.PositiveBigIntegerField(default=1)
    automation_epoch = models.PositiveBigIntegerField(default=1)
    history_quota_started_at = models.DateTimeField(null=True)
    history_quota_used = models.PositiveSmallIntegerField(default=0)
    history_auto_quota_used = models.PositiveSmallIntegerField(default=0)
    reconciliation_scan_since = models.DateTimeField(null=True)
    reconciliation_scan_id = models.PositiveBigIntegerField(null=True)
    automation_paused = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(lease_token__isnull=True, leased_until__isnull=True)
                | Q(lease_token__isnull=False, leased_until__isnull=False),
                name="vk_cursor_lease_pair",
            ),
            models.CheckConstraint(
                condition=Q(reconciliation_scan_since__isnull=True, reconciliation_scan_id__isnull=True)
                | Q(reconciliation_scan_since__isnull=False, reconciliation_scan_id__isnull=False),
                name="vk_reconciliation_scan_pair",
            ),
        ]


class LongPollGap(models.Model):
    cursor = models.ForeignKey(LongPollCursor, on_delete=models.PROTECT)
    period = models.PositiveBigIntegerField()
    previous_position = models.CharField(max_length=128)
    next_position = models.CharField(max_length=128, blank=True)
    previous_batch_at = models.DateTimeField(null=True)
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.CharField(max_length=32)
    status = models.CharField(max_length=24, default="needs_review")
    coverage = models.JSONField(default=dict)
    restore_incident = models.CharField(max_length=128, blank=True)
    resumed_at = models.DateTimeField(null=True)
    resumed_by = models.ForeignKey("accounts.StaffProfile", null=True, on_delete=models.PROTECT)
    resumed_position = models.CharField(max_length=128, blank=True)
    resume_notes = models.TextField(blank=True)


class InboundEvent(models.Model):
    class Status(models.TextChoices):
        ACCEPTED = "accepted"
        QUARANTINED = "quarantined"
        UNSUPPORTED = "unsupported"

    cursor = models.ForeignKey(LongPollCursor, on_delete=models.PROTECT)
    event_type = models.CharField(max_length=64)
    peer_id = models.BigIntegerField(null=True)
    conversation_message_id = models.BigIntegerField(null=True)
    received_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=16, choices=Status)
    reason = models.CharField(max_length=32, blank=True)
    automation_epoch = models.PositiveBigIntegerField()
    gap = models.ForeignKey(LongPollGap, null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cursor", "peer_id", "conversation_message_id"],
                condition=Q(event_type="message_new", status="accepted"),
                name="vk_unique_inbound_message",
            ),
        ]


class StudentAction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    ticket = models.ForeignKey("support.Ticket", on_delete=models.PROTECT)
    expected_user_id = models.BigIntegerField()
    action = models.CharField(max_length=24)
    mode_version = models.PositiveIntegerField()
    expected_closed_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True)
    result = models.JSONField(default=dict)


class StudentClick(models.Model):
    event = models.OneToOneField(InboundEvent, on_delete=models.PROTECT)
    cursor = models.ForeignKey(LongPollCursor, on_delete=models.PROTECT)
    peer_id = models.BigIntegerField()
    user_id = models.BigIntegerField()
    provider_event_id = models.CharField(max_length=128)
    action_id = models.UUIDField()
    answer_deadline = models.DateTimeField()
    result = models.JSONField(default=dict)
    answer_text = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cursor", "peer_id", "user_id", "provider_event_id"], name="vk_unique_student_click",
            ),
        ]
