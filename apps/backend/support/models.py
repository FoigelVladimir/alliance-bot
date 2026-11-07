from django.db import models
from django.db.models import Q
from django.utils import timezone


class Dialog(models.Model):
    cursor = models.ForeignKey("vk_channel.LongPollCursor", on_delete=models.PROTECT)
    vk_peer_id = models.BigIntegerField()
    mode = models.CharField(max_length=20, default="ai_auto")
    mode_version = models.PositiveIntegerField(default=1)
    last_inbound_sequence = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["cursor", "vk_peer_id"], name="support_unique_community_dialog"),
        ]


class Ticket(models.Model):
    class Status(models.TextChoices):
        OPEN = "open"
        CLOSED = "closed"

    dialog = models.ForeignKey(Dialog, on_delete=models.PROTECT)
    status = models.CharField(max_length=6, choices=Status, default=Status.OPEN)
    current_owner = models.ForeignKey("accounts.StaffProfile", null=True, on_delete=models.PROTECT, related_name="+")
    assignment_version = models.PositiveBigIntegerField(default=1)
    importance = models.CharField(max_length=8, default="normal")
    created_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True)
    closed_by = models.ForeignKey("accounts.StaffProfile", null=True, on_delete=models.PROTECT, related_name="+")
    closed_warning_ack = models.BooleanField(default=False)
    parent_ticket = models.ForeignKey("self", null=True, on_delete=models.SET_NULL, related_name="continuations")
    closed_by_vk_id = models.BigIntegerField(null=True)
    closed_reason = models.CharField(max_length=32, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dialog"], condition=Q(status="open"), name="support_one_open_ticket"),
            models.CheckConstraint(
                condition=Q(status="open", closed_at__isnull=True, closed_by__isnull=True, closed_warning_ack=False)
                | Q(status="closed", closed_at__isnull=False),
                name="support_ticket_closed_attrs",
            ),
        ]


class Message(models.Model):
    dialog = models.ForeignKey(Dialog, on_delete=models.PROTECT)
    ticket = models.ForeignKey(Ticket, null=True, on_delete=models.PROTECT)
    inbound_event = models.OneToOneField("vk_channel.InboundEvent", null=True, on_delete=models.PROTECT)
    direction = models.CharField(max_length=20, default="inbound")
    conversation_message_id = models.BigIntegerField(null=True)
    sender_vk_id = models.BigIntegerField(null=True)
    text = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["ticket", "created_at", "id"], name="support_message_ticket_time")]
        constraints = [
            models.UniqueConstraint(
                fields=["dialog", "conversation_message_id"], name="support_unique_dialog_message",
            ),
            models.CheckConstraint(
                condition=~Q(direction="outbound_manager") | Q(ticket__isnull=False),
                name="support_manager_message_ticket",
            ),
        ]


class Attachment(models.Model):
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="attachments")
    attachment_type = models.CharField(max_length=30)
    source_position = models.PositiveIntegerField()
    vk_attach_id = models.CharField(max_length=100, null=True, blank=True)
    source_ref = models.JSONField(null=True)
    file_object = models.ForeignKey("storage.FileObject", null=True, on_delete=models.PROTECT)
    reason_code = models.CharField(max_length=50, null=True)
    verified_content_type = models.CharField(max_length=100, null=True)
    prepared_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField(null=True, db_index=True)
    state = models.CharField(max_length=20)
    meta_info = models.JSONField(default=dict)

    class Meta:
        db_table = "support_message_attachment"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(state="ready", file_object__isnull=False, verified_content_type__isnull=False,
                      prepared_at__isnull=False, expires_at__isnull=False)
                    | Q(state__in=["metadata_only", "preparing", "unsupported", "unavailable"],
                        file_object__isnull=True, verified_content_type__isnull=True,
                        prepared_at__isnull=True, expires_at__isnull=True)
                ),
                name="support_attachment_ready_fields",
            ),
            models.UniqueConstraint(fields=["message", "source_position"], name="support_unique_attachment_position"),
        ]


class AttachmentPreparation(models.Model):
    attachment = models.ForeignKey(Attachment, on_delete=models.PROTECT)
    initiator = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT)
    access_generation = models.PositiveBigIntegerField()
    command = models.OneToOneField("SupportCommand", on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True)
    state = models.CharField(max_length=12, default="active")
    reason_code = models.CharField(max_length=50, null=True)

    class Meta:
        db_table = "support_attachment_preparation"
        constraints = [
            models.UniqueConstraint(fields=["attachment"], condition=Q(state="active"),
                                    name="support_one_active_attachment_fetch"),
        ]


class InboundRoute(models.Model):
    message = models.OneToOneField(Message, on_delete=models.PROTECT)
    dialog = models.ForeignKey(Dialog, on_delete=models.PROTECT)
    sequence = models.PositiveBigIntegerField()
    mode_version = models.PositiveIntegerField()
    automation_epoch = models.PositiveBigIntegerField()
    historical = models.BooleanField(default=False)
    automation_paused = models.BooleanField(default=False)
    status = models.CharField(max_length=16, default="pending")
    reason = models.CharField(max_length=32, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dialog", "sequence"], name="support_unique_inbound_sequence"),
        ]



class SupportMembership(models.Model):
    profile = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT)
    cursor = models.ForeignKey("vk_channel.LongPollCursor", on_delete=models.PROTECT)
    access_generation = models.PositiveIntegerField()
    granted_at = models.DateTimeField(default=timezone.now)
    coordinates_support = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["profile", "cursor"], name="support_unique_membership"),
        ]


class SupportCommand(models.Model):
    client_operation_id = models.UUIDField(unique=True)
    actor = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    target_type = models.CharField(max_length=20)
    target_id = models.PositiveBigIntegerField()
    request_fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)


class TicketEvent(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT)
    command = models.OneToOneField(SupportCommand, on_delete=models.PROTECT)
    assignment_version = models.PositiveBigIntegerField()
    changed = models.BooleanField(default=True)
    previous_owner_id = models.PositiveBigIntegerField(null=True)
    new_owner_id = models.PositiveBigIntegerField(null=True)
    payload = models.JSONField(default=dict)


class ReviewDecision(models.Model):
    route = models.OneToOneField(InboundRoute, on_delete=models.PROTECT)
    command = models.OneToOneField(SupportCommand, on_delete=models.PROTECT)
    ticket = models.ForeignKey(Ticket, null=True, on_delete=models.PROTECT)
    reason = models.TextField(blank=True)


class Draft(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT)
    author = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT)
    text = models.TextField(blank=True)
    revision = models.PositiveBigIntegerField(default=1)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["ticket", "author"], name="support_unique_personal_draft"),
        ]


class OutgoingResponse(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        SENT = "sent"
        FAILED = "failed"
        UNKNOWN = "unknown_outcome"
        CANCELLED = "cancelled_by_takeover"

    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT)
    command = models.OneToOneField(SupportCommand, on_delete=models.PROTECT)
    assignment_version = models.PositiveBigIntegerField()
    text = models.TextField()
    vk_random_id = models.PositiveIntegerField(unique=True)
    vk_keyboard = models.JSONField(null=True)
    status = models.CharField(max_length=24, choices=Status, default=Status.PENDING)
    message = models.OneToOneField(Message, null=True, on_delete=models.PROTECT)
    provider_message_id = models.PositiveBigIntegerField(null=True)
    source_draft_id_snapshot = models.PositiveBigIntegerField(null=True)
    source_draft_revision = models.PositiveBigIntegerField(null=True)
    recovery_verification_required = models.BooleanField(default=False)
    failure_reason = models.CharField(max_length=48, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["pending", "sent", "failed", "unknown_outcome", "cancelled_by_takeover"]),
                name="support_response_valid_status",
            ),
            models.CheckConstraint(
                condition=Q(source_draft_id_snapshot__isnull=True, source_draft_revision__isnull=True)
                | Q(source_draft_id_snapshot__isnull=False, source_draft_revision__isnull=False),
                name="support_response_draft_pair",
            ),
        ]


class OutgoingAttempt(models.Model):
    class Status(models.TextChoices):
        IN_FLIGHT = "in_flight"
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        UNKNOWN = "unknown_outcome"

    response = models.ForeignKey(OutgoingResponse, on_delete=models.PROTECT, related_name="attempts")
    command = models.OneToOneField(SupportCommand, on_delete=models.PROTECT)
    task = models.OneToOneField("jobs.BackgroundTask", on_delete=models.PROTECT)
    attempt_type = models.CharField(max_length=16)
    status = models.CharField(max_length=16, choices=Status, default=Status.IN_FLIGHT)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    failure_reason = models.CharField(max_length=48, blank=True)
    vk_error_code = models.PositiveIntegerField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["response"], condition=Q(status__in=["in_flight", "unknown_outcome"]),
                name="support_response_one_active_attempt",
            ),
            models.UniqueConstraint(
                fields=["response"], condition=Q(attempt_type="initial"),
                name="support_response_one_initial_attempt",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["in_flight", "succeeded", "failed", "unknown_outcome"]),
                name="support_attempt_valid_status",
            ),
            models.CheckConstraint(
                condition=Q(attempt_type__in=["initial", "manual_retry"]),
                name="support_attempt_valid_type",
            ),
        ]


class OutgoingReconciliation(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        FINISHED = "finished"

    class Outcome(models.TextChoices):
        ACCEPTED = "accepted_by_vk"
        UNRESOLVED = "unresolved"

    response = models.ForeignKey(OutgoingResponse, on_delete=models.PROTECT, related_name="reconciliations")
    command = models.OneToOneField(SupportCommand, on_delete=models.PROTECT)
    target_attempt = models.ForeignKey(OutgoingAttempt, on_delete=models.PROTECT)
    task = models.OneToOneField("jobs.BackgroundTask", on_delete=models.PROTECT)
    status = models.CharField(max_length=8, choices=Status, default=Status.QUEUED)
    outcome = models.CharField(max_length=24, choices=Outcome, blank=True)
    reason = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    lookup_deadline_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["response"], condition=Q(status__in=["queued", "running"]),
                name="support_one_active_reconciliation",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["queued", "running"], outcome="", finished_at__isnull=True)
                | Q(status="finished", outcome__in=["accepted_by_vk", "unresolved"], finished_at__isnull=False),
                name="support_reconciliation_outcome",
            ),
        ]


class ReconciliationEvidence(models.Model):
    reconciliation = models.OneToOneField(OutgoingReconciliation, on_delete=models.PROTECT)
    source_kind = models.CharField(max_length=32)
    rule_version = models.PositiveIntegerField()
    observed_at = models.DateTimeField()
    facts = models.JSONField()
