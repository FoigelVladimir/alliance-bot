from django.db import models
from django.db.models import Q
from django.utils import timezone


class StaffProfile(models.Model):
    class Access(models.TextChoices):
        NOT_GRANTED = "not_granted"
        GRANTED = "granted"
        REVOKED = "revoked"

    class Role(models.TextChoices):
        MANAGER = "manager"
        ADMINISTRATOR = "administrator"
        LEADER = "leader"

    display_name = models.CharField(max_length=255, blank=True)
    photo_url = models.URLField(max_length=2048, blank=True)
    access_state = models.CharField(max_length=16, choices=Access, default=Access.NOT_GRANTED)
    role = models.CharField(max_length=16, choices=Role, blank=True)
    blocked_at = models.DateTimeField(null=True)
    access_version = models.PositiveIntegerField(default=1)
    identity_version = models.PositiveIntegerField(default=1)
    session_epoch = models.PositiveIntegerField(default=1)
    access_generation = models.PositiveIntegerField(default=1)
    can_appoint_administrator = models.BooleanField(default=False)
    last_revocation = models.ForeignKey("AccessEvent", null=True, on_delete=models.PROTECT, related_name="+")
    regrant_verified_at = models.DateTimeField(null=True)
    regrant_identity_version = models.PositiveIntegerField(null=True)
    regrant_revocation = models.ForeignKey("AccessEvent", null=True, on_delete=models.PROTECT, related_name="+")
    disabled = models.BooleanField(default=False)
    cleared_incident = models.CharField(max_length=128, blank=True)


class IdentityBinding(models.Model):
    profile = models.ForeignKey(StaffProfile, on_delete=models.PROTECT, related_name="bindings")
    provider = models.CharField(max_length=16, default="vk_id")
    external_id = models.CharField(max_length=32)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "external_id"], name="staff_unique_external_identity"),
            models.UniqueConstraint(fields=["profile"], condition=Q(active=True), name="staff_one_active_identity"),
        ]


class BrowserLogin(models.Model):
    session_key = models.CharField(max_length=40, primary_key=True)
    state_hash = models.CharField(max_length=64)
    verifier = models.CharField(max_length=128)
    started_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    exchanging = models.BooleanField(default=False)
    quarantine_incident = models.CharField(max_length=128, blank=True)
    recovery = models.ForeignKey("LeaderRecovery", null=True, on_delete=models.CASCADE)


class AccessEvent(models.Model):
    actor = models.ForeignKey(StaffProfile, null=True, on_delete=models.PROTECT, related_name="+")
    subject = models.ForeignKey(StaffProfile, on_delete=models.PROTECT, related_name="+")
    application = models.ForeignKey("AccessApplication", null=True, on_delete=models.PROTECT, related_name="+")
    operation_id = models.UUIDField(null=True)
    action = models.CharField(max_length=40)
    parameters = models.JSONField(default=dict)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["actor", "operation_id"], name="staff_unique_access_operation"),
        ]


class AccessApplication(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"

    profile = models.ForeignKey(StaffProfile, on_delete=models.PROTECT, related_name="applications")
    client_operation_id = models.UUIDField()
    club_activity = models.TextField(blank=True)
    status = models.CharField(max_length=8, choices=Status, default=Status.PENDING)
    submitted_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True)
    decided_by = models.ForeignKey(StaffProfile, null=True, on_delete=models.PROTECT, related_name="+")
    service_decision_event = models.ForeignKey(AccessEvent, null=True, on_delete=models.PROTECT, related_name="+")
    applicant_message = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["profile", "client_operation_id"], name="staff_unique_application_operation"),
            models.UniqueConstraint(fields=["profile"], condition=Q(status="pending"), name="staff_one_pending_application"),
            models.CheckConstraint(
                condition=(
                    Q(status="pending", decided_at__isnull=True, decided_by__isnull=True, service_decision_event__isnull=True)
                    | (
                        Q(status__in=["approved", "rejected"], decided_at__isnull=False)
                        & (Q(decided_by__isnull=False, service_decision_event__isnull=True)
                           | Q(decided_by__isnull=True, service_decision_event__isnull=False))
                    )
                ),
                name="staff_application_decision_source",
            ),
        ]


class AccessControl(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    quarantine_incident = models.CharField(max_length=128, blank=True)
    initial_event = models.ForeignKey(AccessEvent, null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(id=1), name="staff_single_access_control")]


class LeaderRecovery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        VERIFIED = "verified"
        COMPLETED = "completed"

    profile = models.ForeignKey(StaffProfile, on_delete=models.PROTECT, related_name="recoveries")
    expected_identity_version = models.PositiveIntegerField()
    code_hash = models.CharField(max_length=64, unique=True, null=True)
    expires_at = models.DateTimeField()
    status = models.CharField(max_length=9, choices=Status, default=Status.PENDING)
    external_id = models.CharField(max_length=32, blank=True)
    quarantine_incident = models.CharField(max_length=128, blank=True)
    authority_reference = models.CharField(max_length=255, blank=True)
    result = models.ForeignKey(AccessEvent, null=True, on_delete=models.PROTECT)
