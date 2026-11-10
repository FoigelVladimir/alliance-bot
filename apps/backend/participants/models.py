from uuid import uuid4

from django.db import models
from django.db.models import Q
from django.utils import timezone


class Person(models.Model):
    """Club record; a VK hint supplied by a captain never grants ownership."""

    identity = models.OneToOneField("accounts.IdentityBinding", null=True, on_delete=models.PROTECT)
    full_name = models.CharField(max_length=255, blank=True)
    student_number = models.CharField(max_length=64, blank=True)
    vk_url = models.CharField(max_length=500, blank=True)
    study_group = models.CharField(max_length=100, blank=True)
    contact = models.CharField(max_length=500, blank=True)
    revision = models.PositiveBigIntegerField(default=1)
    merged_into = models.ForeignKey("self", null=True, on_delete=models.PROTECT, related_name="merged_records")
    created_at = models.DateTimeField(default=timezone.now)
    identity_verified_at = models.DateTimeField(null=True)
    data_verified_at = models.DateTimeField(null=True)
    data_verified_by = models.ForeignKey("accounts.StaffProfile", null=True, on_delete=models.PROTECT, related_name="+")
    quarantine_incident = models.CharField(max_length=128, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(revision__gt=0), name="participant_person_revision"),
            models.CheckConstraint(condition=~Q(merged_into=models.F("id")), name="participant_no_self_merge"),
        ]


class BotAccount(models.Model):
    cursor = models.ForeignKey("vk_channel.LongPollCursor", on_delete=models.PROTECT)
    sender_id = models.PositiveBigIntegerField()
    person = models.ForeignKey(Person, null=True, on_delete=models.PROTECT)
    binding_version = models.PositiveBigIntegerField(default=1)
    quarantine_incident = models.CharField(max_length=128, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["cursor", "sender_id"], name="participant_bot_sender_unique"),
            models.UniqueConstraint(fields=["cursor", "person"], condition=Q(person__isnull=False), name="participant_bot_person_unique"),
            models.CheckConstraint(condition=Q(sender_id__gt=0, binding_version__gt=0), name="participant_bot_identity_valid"),
        ]


class EntryLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    account = models.ForeignKey(BotAccount, on_delete=models.PROTECT)
    source_event = models.OneToOneField("vk_channel.InboundEvent", on_delete=models.PROTECT)
    purpose = models.CharField(max_length=24)
    target_id = models.PositiveBigIntegerField(null=True)
    binding_version = models.PositiveBigIntegerField()
    automation_epoch = models.PositiveBigIntegerField()
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=Q(purpose__in=["my_requests", "notifications"], target_id__isnull=True)
            | Q(purpose__in=["event", "section", "team"], target_id__isnull=False),
            name="participant_entry_purpose",
        )]


class BindingAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    link = models.ForeignKey(EntryLink, on_delete=models.PROTECT)
    identity = models.ForeignKey("accounts.IdentityBinding", on_delete=models.PROTECT)
    session_key = models.CharField(max_length=40)
    session_epoch = models.PositiveIntegerField()
    binding_version = models.PositiveBigIntegerField()
    status = models.CharField(max_length=16, default="pending")
    person = models.ForeignKey(Person, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    proof = models.OneToOneField("vk_channel.StudentClick", null=True, on_delete=models.PROTECT)

    class Meta:
        indexes = [models.Index(fields=["session_key", "-created_at"], name="participant_binding_browser")]
        constraints = [models.CheckConstraint(
            condition=Q(status="pending", finished_at__isnull=True, proof__isnull=True, person__isnull=True)
            | Q(status="confirmed", finished_at__isnull=False, proof__isnull=False, person__isnull=False)
            | Q(status__in=["cancelled", "conflict", "expired"], finished_at__isnull=False, proof__isnull=True, person__isnull=True),
            name="participant_binding_result",
        )]


class PersonOrigin(models.Model):
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="origins")
    kind = models.CharField(max_length=32)
    source_id = models.CharField(max_length=100)
    values = models.JSONField(default=dict)
    actor = models.ForeignKey("accounts.StaffProfile", null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)


class ParticipantEvent(models.Model):
    person = models.ForeignKey(Person, null=True, on_delete=models.PROTECT)
    actor = models.ForeignKey("accounts.StaffProfile", null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)


class ParticipantCommand(models.Model):
    actor = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT)
    client_operation_id = models.UUIDField()
    action = models.CharField(max_length=40)
    fingerprint = models.CharField(max_length=64)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["actor", "client_operation_id"], name="participant_actor_command")]
