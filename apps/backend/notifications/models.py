from uuid import uuid4

from django.db import models
from django.db.models import Q
from django.utils import timezone


class Preference(models.Model):
    person = models.ForeignKey("participants.Person", on_delete=models.PROTECT)
    category = models.CharField(max_length=24)
    scope_kind = models.CharField(max_length=16, default="club")
    subject_id = models.PositiveBigIntegerField(default=0)
    enabled = models.BooleanField(default=False)
    revision = models.PositiveBigIntegerField(default=1)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["person", "category", "scope_kind", "subject_id"], name="notice_preference_unique"),
            models.CheckConstraint(condition=Q(category__in=["announcements", "schedule", "changes", "decisions"]), name="notice_preference_category"),
            models.CheckConstraint(condition=Q(scope_kind="club", subject_id=0)
                                   | Q(scope_kind__in=["event", "section"], subject_id__gt=0), name="notice_preference_scope"),
        ]


class ParticipantSettings(models.Model):
    person = models.OneToOneField("participants.Person", on_delete=models.PROTECT)
    all_disabled = models.BooleanField(default=False)
    revision = models.PositiveBigIntegerField(default=1)
    verification_required = models.BooleanField(default=False)
    onboarding_completed_at = models.DateTimeField(null=True)
    updated_at = models.DateTimeField(default=timezone.now)


class SettingsEvent(models.Model):
    command = models.OneToOneField("participants.ParticipantCommand", on_delete=models.PROTECT)
    settings = models.ForeignKey(ParticipantSettings, on_delete=models.PROTECT)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)


class PreferenceAction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    account = models.ForeignKey("participants.BotAccount", on_delete=models.PROTECT)
    source_link = models.OneToOneField("participants.EntryLink", null=True, on_delete=models.PROTECT)
    person_key = models.PositiveBigIntegerField()
    binding_version = models.PositiveBigIntegerField()
    automation_epoch = models.PositiveBigIntegerField()
    expected_revision = models.PositiveBigIntegerField()
    all_disabled = models.BooleanField()
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True)
    result = models.JSONField(default=dict)


class ChannelMessage(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        SENT = "sent"
        FAILED = "failed"
        UNKNOWN = "unknown_outcome"
        CANCELLED = "cancelled"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    source_key = models.UUIDField(unique=True)
    account = models.ForeignKey("participants.BotAccount", on_delete=models.PROTECT)
    event = models.ForeignKey("events.Event", null=True, on_delete=models.PROTECT)
    person = models.ForeignKey("participants.Person", null=True, on_delete=models.PROTECT)
    kind = models.CharField(max_length=32)
    reference_id = models.CharField(max_length=100)
    binding_version = models.PositiveBigIntegerField()
    automation_epoch = models.PositiveBigIntegerField()
    text = models.TextField()
    text_sha256 = models.CharField(max_length=64, blank=True)
    keyboard = models.JSONField(null=True)
    status = models.CharField(max_length=20, choices=Status, default=Status.PENDING)
    reason = models.CharField(max_length=64, blank=True)
    provider_message_id = models.PositiveBigIntegerField(null=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    recovery_verification_required = models.BooleanField(default=False)


class ChannelAttempt(models.Model):
    message = models.ForeignKey(ChannelMessage, on_delete=models.PROTECT, related_name="attempts")
    task = models.OneToOneField("jobs.BackgroundTask", on_delete=models.PROTECT)
    random_id = models.OneToOneField("support.VKRandomReservation", on_delete=models.PROTECT)
    status = models.CharField(max_length=20, default="pending")
    admitted_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    provider_message_id = models.PositiveBigIntegerField(null=True)
    vk_error_code = models.PositiveIntegerField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["message"], condition=Q(status="pending"), name="notice_one_pending_attempt")]


class CancellationBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    deletion = models.OneToOneField("events.DeletionIntent", on_delete=models.PROTECT)
    text = models.TextField()
    recipients = models.JSONField(default=list)
    created_at = models.DateTimeField(default=timezone.now)
    verification_required = models.BooleanField(default=False)
