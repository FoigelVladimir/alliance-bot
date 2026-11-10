from uuid import uuid4

from django.db import models
from django.db.models import Q
from django.utils import timezone


class Event(models.Model):
    stable_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    title = models.CharField(max_length=200)
    sport = models.CharField(max_length=100)
    participation_format = models.CharField(max_length=16)
    description = models.TextField(blank=True)
    planned_start_at = models.DateTimeField()
    timezone_name = models.CharField(max_length=64)
    location = models.CharField(max_length=500)
    responsible = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT, related_name="responsible_events")
    responsible_generation = models.PositiveIntegerField()
    created_by = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveBigIntegerField(default=1)
    state = models.CharField(max_length=24, default="planned")
    current_form = models.ForeignKey("FormRevision", null=True, on_delete=models.PROTECT, related_name="+")
    current_window = models.ForeignKey("RegistrationWindow", null=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    cancelled_at = models.DateTimeField(null=True)
    deleted_at = models.DateTimeField(null=True)
    deletion_pending = models.BooleanField(default=False)
    restoration_verification_required = models.BooleanField(default=False)
    reporting_excluded = models.BooleanField(default=False)
    reporting_exclusion_kind = models.CharField(max_length=16, blank=True)
    reporting_primary = models.ForeignKey("self", null=True, on_delete=models.PROTECT, related_name="excluded_duplicates")
    reporting_reason = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(participation_format__in=["team", "individual"]), name="event_participation_format"),
            models.CheckConstraint(condition=Q(revision__gt=0), name="event_positive_revision"),
            models.CheckConstraint(condition=Q(state__in=["planned", "started", "completed", "cancelled", "stopped", "deleted"]), name="event_known_state"),
            models.CheckConstraint(condition=Q(state__in=["planned", "cancelled", "deleted"], started_at__isnull=True)
                                   | Q(state__in=["started", "completed", "stopped"], started_at__isnull=False), name="event_start_state"),
            models.CheckConstraint(condition=Q(reporting_excluded=False, reporting_exclusion_kind="", reporting_primary__isnull=True)
                                   | Q(reporting_excluded=True, reporting_exclusion_kind="error", reporting_primary__isnull=True, state="completed")
                                   | (Q(reporting_excluded=True, reporting_exclusion_kind="duplicate", reporting_primary__isnull=False, state="completed")
                                      & ~Q(reporting_primary=models.F("id"))), name="event_reporting_exclusion"),
        ]


class HelperAssignment(models.Model):
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="helper_assignments")
    profile = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT)
    access_generation = models.PositiveIntegerField()
    assigned_by = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT, related_name="+")
    assigned_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "profile"], condition=Q(ended_at__isnull=True), name="event_one_current_helper")]


class FormRevision(models.Model):
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="forms")
    revision = models.PositiveBigIntegerField()
    schema = models.JSONField()
    created_by = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    published_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "revision"], name="event_form_revision_unique")]


class RegistrationWindow(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="registration_windows")
    opened_at = models.DateTimeField(default=timezone.now)
    deadline_at = models.DateTimeField()
    opened_by = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT)


class Application(models.Model):
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="applications")
    owner = models.ForeignKey("participants.Person", on_delete=models.PROTECT)
    form = models.ForeignKey(FormRevision, on_delete=models.PROTECT)
    participation_format = models.CharField(max_length=16)
    status = models.CharField(max_length=24, default="draft")
    revision = models.PositiveBigIntegerField(default=1)
    data_revision = models.PositiveBigIntegerField(default=1)
    values = models.JSONField(default=dict)
    validation_digest = models.CharField(max_length=64, blank=True)
    first_submitted_at = models.DateTimeField(null=True)
    submission_window = models.ForeignKey(RegistrationWindow, null=True, on_delete=models.PROTECT)
    participant_explanation = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "owner"], condition=Q(first_submitted_at__isnull=False), name="event_one_submitted_owner"),
            models.UniqueConstraint(fields=["event", "owner", "participation_format"], condition=Q(status="draft"), name="event_one_draft_format"),
            models.CheckConstraint(condition=Q(status__in=["draft", "submitted", "needs_changes", "admitted", "rejected", "withdrawn", "admission_revoked"]), name="event_application_status"),
            models.CheckConstraint(condition=Q(status="draft", first_submitted_at__isnull=True, submission_window__isnull=True)
                                   | (~Q(status="draft") & Q(first_submitted_at__isnull=False, submission_window__isnull=False)), name="event_application_submission"),
        ]


class ApplicationPlayer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.PROTECT, related_name="players")
    person = models.ForeignKey("participants.Person", null=True, on_delete=models.PROTECT)
    position = models.PositiveIntegerField()
    values = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["application", "position"], condition=Q(is_active=True), name="event_application_player_position")]


class EventCommand(models.Model):
    actor = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT)
    owner = models.ForeignKey("participants.Person", null=True, on_delete=models.PROTECT)
    client_operation_id = models.UUIDField()
    action = models.CharField(max_length=40)
    fingerprint = models.CharField(max_length=64)
    event = models.ForeignKey(Event, null=True, on_delete=models.PROTECT)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["actor", "client_operation_id"], condition=Q(owner__isnull=True), name="event_actor_command_unique"),
            models.UniqueConstraint(fields=["owner", "client_operation_id"], condition=Q(owner__isnull=False), name="event_owner_command_unique"),
        ]


class EventHistory(models.Model):
    command = models.OneToOneField(EventCommand, on_delete=models.PROTECT)
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="history")
    application = models.ForeignKey(Application, null=True, on_delete=models.PROTECT)
    actor = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)


class StartSnapshot(models.Model):
    event = models.OneToOneField(Event, on_delete=models.PROTECT, related_name="start_snapshot")
    command = models.OneToOneField(EventCommand, on_delete=models.PROTECT)
    entries = models.JSONField()
    pending_applications = models.JSONField(default=list)
    created_at = models.DateTimeField(default=timezone.now)


class Participation(models.Model):
    snapshot = models.ForeignKey(StartSnapshot, on_delete=models.PROTECT, related_name="participation_marks")
    application = models.ForeignKey(Application, on_delete=models.PROTECT)
    mark = models.CharField(max_length=16, default="unmarked")
    revision = models.PositiveBigIntegerField(default=1)
    changed_by = models.ForeignKey("accounts.StaffProfile", null=True, on_delete=models.PROTECT)
    changed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["snapshot", "application"], name="event_participation_entry_unique"),
            models.CheckConstraint(condition=Q(mark__in=["unmarked", "participated", "not_participated"]), name="event_participation_mark"),
        ]


class Venue(models.Model):
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="venues")
    name = models.CharField(max_length=200)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "name"], name="event_venue_name_unique")]


class DeletionIntent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    command = models.OneToOneField(EventCommand, null=True, on_delete=models.PROTECT)
    event = models.OneToOneField(Event, null=True, on_delete=models.PROTECT)
    environment = models.CharField(max_length=128)
    event_uuid = models.UUIDField()
    actor_uuid = models.UUIDField()
    client_operation_id = models.UUIDField()
    request_fingerprint = models.CharField(max_length=64)
    notice_mode = models.CharField(max_length=24)
    status = models.CharField(max_length=24, default="awaiting_ledger")
    notification_snapshot = models.JSONField(default=dict)
    notification_verification_required = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    ledger_recorded_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    cleaned_at = models.DateTimeField(null=True)
    task = models.OneToOneField("jobs.BackgroundTask", null=True, on_delete=models.PROTECT)
    cleanup_task = models.OneToOneField("jobs.BackgroundTask", null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["environment", "event_uuid"], name="event_deletion_key_unique"),
            models.CheckConstraint(condition=Q(notice_mode__in=["none", "notify", "already_notified"]), name="event_deletion_notice_mode"),
            models.CheckConstraint(condition=Q(status__in=["awaiting_ledger", "needs_review"], finished_at__isnull=True)
                                   | Q(status="deleted", finished_at__isnull=False, ledger_recorded_at__isnull=False), name="event_deletion_result"),
        ]
