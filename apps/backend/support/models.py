from uuid import NAMESPACE_URL, uuid5

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
    handoff_reason = models.CharField(max_length=32, blank=True)
    handoff_route = models.OneToOneField(
        "InboundRoute", null=True, on_delete=models.PROTECT, related_name="handoff_ticket",
    )
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
    reply_to_cmid = models.BigIntegerField(null=True)
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
    confirmed_sent_at = models.DateTimeField(null=True)
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


class DeliveryRecoveryRun(models.Model):
    id = models.UUIDField(primary_key=True)
    incident = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True)


class ResponseRecoveryGuard(models.Model):
    response = models.ForeignKey(OutgoingResponse, on_delete=models.PROTECT, related_name="recovery_guards")
    recovery_run = models.ForeignKey(DeliveryRecoveryRun, on_delete=models.PROTECT)
    opened_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True)
    resolution = models.CharField(max_length=24, null=True)
    evidence_ref = models.ForeignKey("ReconciliationEvidence", null=True, on_delete=models.PROTECT)
    resolved_by = models.ForeignKey("accounts.StaffProfile", null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["response", "recovery_run"], name="support_guard_response_run"),
            models.UniqueConstraint(
                fields=["response"], condition=Q(resolved_at__isnull=True), name="support_one_active_recovery_guard",
            ),
            models.CheckConstraint(
                condition=Q(resolved_at__isnull=True, resolution__isnull=True, evidence_ref__isnull=True)
                | Q(resolved_at__isnull=False, resolution__isnull=False, resolution="superseded", evidence_ref__isnull=True)
                | Q(resolved_at__isnull=False, resolution__isnull=False, resolution__in=["accepted_by_vk", "not_sent_verified"],
                    evidence_ref__isnull=False),
                name="support_guard_resolution",
            ),
        ]


class OutgoingReconciliation(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        FINISHED = "finished"

    class Outcome(models.TextChoices):
        ACCEPTED = "accepted_by_vk"
        NOT_SENT = "not_sent_verified"
        UNRESOLVED = "unresolved"

    response = models.ForeignKey(OutgoingResponse, on_delete=models.PROTECT, related_name="reconciliations")
    command = models.OneToOneField(SupportCommand, on_delete=models.PROTECT)
    target_attempt = models.ForeignKey(OutgoingAttempt, null=True, on_delete=models.PROTECT)
    target_recovery_guard = models.ForeignKey(ResponseRecoveryGuard, null=True, on_delete=models.PROTECT)
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
                | Q(status="finished", outcome__in=["accepted_by_vk", "not_sent_verified", "unresolved"], finished_at__isnull=False),
                name="support_reconciliation_outcome",
            ),
            models.CheckConstraint(
                condition=Q(target_attempt__isnull=False, target_recovery_guard__isnull=True)
                | Q(target_attempt__isnull=True, target_recovery_guard__isnull=False),
                name="support_reconciliation_one_target",
            ),
        ]


class ReconciliationEvidence(models.Model):
    reconciliation = models.OneToOneField(OutgoingReconciliation, on_delete=models.PROTECT)
    source_kind = models.CharField(max_length=32)
    rule_version = models.PositiveIntegerField()
    observed_at = models.DateTimeField()
    facts = models.JSONField()


class AIRequest(models.Model):
    dialog = models.ForeignKey(Dialog, on_delete=models.PROTECT)
    mode_version = models.PositiveIntegerField()
    automation_epoch = models.PositiveBigIntegerField()
    request_version = models.PositiveIntegerField(default=1)
    request_version_started_at = models.DateTimeField()
    normalized_query = models.TextField()
    status = models.CharField(max_length=30, default="pending")
    created_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "support_ai_request"


class AIRequestMessage(models.Model):
    request = models.ForeignKey(AIRequest, on_delete=models.PROTECT, related_name="message_links")
    message = models.OneToOneField(Message, on_delete=models.PROTECT)

    class Meta:
        db_table = "support_ai_request_message"


class AIProcessingRun(models.Model):
    operation_id = models.UUIDField(unique=True)
    dialog = models.ForeignKey(Dialog, on_delete=models.PROTECT)
    task = models.OneToOneField("jobs.BackgroundTask", on_delete=models.PROTECT)
    mode_version = models.PositiveIntegerField()
    automation_epoch = models.PositiveBigIntegerField()
    answer = models.OneToOneField("AIAnswer", null=True, on_delete=models.PROTECT)
    handoff_ticket = models.ForeignKey(Ticket, null=True, on_delete=models.PROTECT)
    handoff_plan = models.ForeignKey("HandoffPlan", null=True, on_delete=models.PROTECT, related_name="runs")
    status = models.CharField(max_length=16, default="running")
    reason = models.CharField(max_length=40, blank=True)
    sources_used = models.JSONField(default=list)
    created_at = models.DateTimeField()
    processing_deadline_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "support_ai_processing_run"
        constraints = [
            models.CheckConstraint(
                condition=Q(status="running", reason="", finished_at__isnull=True, answer__isnull=True)
                | (Q(status="no_answer", finished_at__isnull=False, answer__isnull=True) & ~Q(reason=""))
                | Q(status="answer_saved", reason="", finished_at__isnull=False, answer__isnull=False),
                name="support_ai_run_outcome",
            ),
        ]


class AIProcessingInput(models.Model):
    run = models.ForeignKey(AIProcessingRun, on_delete=models.PROTECT, related_name="inputs")
    request = models.ForeignKey(AIRequest, on_delete=models.PROTECT)
    request_version = models.PositiveIntegerField()
    question_text = models.TextField()
    messages = models.ManyToManyField(Message, through="AIProcessingInputMessage")

    class Meta:
        db_table = "support_ai_processing_input"
        constraints = [
            models.UniqueConstraint(fields=["run", "request"], name="support_ai_unique_run_request"),
        ]


class AIProcessingInputMessage(models.Model):
    processing_input = models.ForeignKey(AIProcessingInput, on_delete=models.PROTECT)
    message = models.ForeignKey(Message, on_delete=models.PROTECT)

    class Meta:
        db_table = "support_ai_processing_input_message"
        constraints = [
            models.UniqueConstraint(fields=["processing_input", "message"], name="support_ai_unique_input_message"),
        ]


class AIAnswer(models.Model):
    dialog = models.ForeignKey(Dialog, on_delete=models.PROTECT)
    generation_operation_id = models.UUIDField(unique=True)
    mode_version_snapshot = models.PositiveIntegerField()
    model_snapshot = models.JSONField()
    retrieval_snapshot = models.JSONField()
    timings_snapshot = models.JSONField()
    snapshot_schema_version = models.PositiveIntegerField(default=1)
    content_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "support_ai_answer"


class AIAnswerInput(models.Model):
    answer = models.ForeignKey(AIAnswer, on_delete=models.PROTECT, related_name="inputs")
    request = models.ForeignKey(AIRequest, on_delete=models.PROTECT)
    request_version_snapshot = models.PositiveIntegerField()
    query_snapshot = models.TextField()
    sources_snapshot = models.JSONField()

    class Meta:
        db_table = "support_ai_answer_input"
        constraints = [models.UniqueConstraint(fields=["answer", "request"], name="support_ai_answer_input_unique")]


class AIAnswerInputMessage(models.Model):
    input = models.ForeignKey(AIAnswerInput, on_delete=models.PROTECT)
    message = models.ForeignKey(Message, on_delete=models.PROTECT)

    class Meta:
        db_table = "support_ai_answer_input_message"
        constraints = [models.UniqueConstraint(fields=["input", "message"], name="support_ai_answer_msg_unique")]


class AIAnswerPart(models.Model):
    answer = models.ForeignKey(AIAnswer, on_delete=models.PROTECT, related_name="parts")
    position = models.PositiveIntegerField()
    text_snapshot = models.TextField()
    message = models.OneToOneField(Message, null=True, on_delete=models.PROTECT, related_name="ai_answer_part")

    class Meta:
        db_table = "support_ai_answer_part"
        constraints = [
            models.UniqueConstraint(fields=["answer", "position"], name="support_ai_answer_part_unique"),
            models.CheckConstraint(condition=Q(position__gte=1), name="support_ai_answer_part_position"),
        ]


class AIAnswerPartInput(models.Model):
    part = models.ForeignKey(AIAnswerPart, on_delete=models.PROTECT)
    input = models.ForeignKey(AIAnswerInput, on_delete=models.PROTECT)

    class Meta:
        db_table = "support_ai_answer_part_input"
        constraints = [models.UniqueConstraint(fields=["part", "input"], name="support_ai_part_input_unique")]


class BotDeliveryRecoveryRun(models.Model):
    id = models.UUIDField(primary_key=True)
    incident = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True)


class AIPartDelivery(models.Model):
    part = models.OneToOneField(AIAnswerPart, on_delete=models.PROTECT)
    handoff_ticket = models.ForeignKey(Ticket, null=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, default="waiting")
    vk_random_id = models.PositiveIntegerField(null=True, unique=True)
    provider_message_id = models.PositiveBigIntegerField(null=True)
    reason_code = models.CharField(max_length=48, blank=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    uncertainty_revision = models.UUIDField(null=True)
    uncertainty_since = models.DateTimeField(null=True)
    recovery_run = models.ForeignKey(BotDeliveryRecoveryRun, null=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "support_ai_part_delivery"
        indexes = [models.Index(fields=["uncertainty_since", "id"], condition=Q(status="unknown_outcome"), name="ai_unknown_scan")]
        constraints = [
            models.CheckConstraint(
                condition=Q(uncertainty_revision__isnull=False, uncertainty_since__isnull=False)
                | (~Q(status="unknown_outcome") & Q(uncertainty_revision__isnull=True, uncertainty_since__isnull=True)),
                name="support_ai_uncertainty_pair",
            ),
        ]


class AIPartAttempt(models.Model):
    delivery = models.OneToOneField(AIPartDelivery, on_delete=models.PROTECT)
    task = models.OneToOneField("jobs.BackgroundTask", on_delete=models.PROTECT)
    operation_id = models.UUIDField(unique=True)
    status = models.CharField(max_length=20, default="in_flight")
    created_at = models.DateTimeField()
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    reason_code = models.CharField(max_length=48, blank=True)
    vk_error_code = models.PositiveIntegerField(null=True)

    class Meta:
        db_table = "support_ai_part_attempt"
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["in_flight", "unknown_outcome"], finished_at__isnull=True)
                | Q(status__in=["succeeded", "failed"], finished_at__isnull=False),
                name="support_ai_attempt_finished",
            ),
        ]


class VKRandomReservation(models.Model):
    random_id = models.PositiveIntegerField(primary_key=True)

    class Meta:
        db_table = "support_vk_random_reservation"


class AIPartReconciliation(models.Model):
    delivery = models.ForeignKey(AIPartDelivery, related_name="reconciliations", on_delete=models.PROTECT)
    target_revision = models.UUIDField()
    target_attempt = models.ForeignKey(AIPartAttempt, null=True, on_delete=models.PROTECT)
    trigger = models.CharField(max_length=12)
    actor = models.ForeignKey("accounts.StaffProfile", null=True, on_delete=models.PROTECT)
    client_operation_id = models.UUIDField(null=True)
    command = models.OneToOneField(SupportCommand, null=True, on_delete=models.PROTECT)
    task = models.OneToOneField("jobs.BackgroundTask", on_delete=models.PROTECT)
    status = models.CharField(max_length=8, default="queued")
    outcome = models.CharField(max_length=24, blank=True)
    reason = models.CharField(max_length=48, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    lookup_deadline_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    evidence = models.OneToOneField("AIPartReconciliationEvidence", null=True, related_name="+", on_delete=models.PROTECT)

    @property
    def task_key(self):
        if self.trigger == "manual":
            return self.client_operation_id
        return uuid5(NAMESPACE_URL, f"alliance:bot-reconciliation:{self.delivery_id}:{self.target_revision}")

    class Meta:
        db_table = "support_ai_part_reconciliation"
        constraints = [
            models.UniqueConstraint(fields=["delivery"], condition=Q(status__in=["queued", "running"]), name="ai_part_one_active_check"),
            models.UniqueConstraint(fields=["delivery", "target_revision"], condition=Q(trigger="automatic"), name="ai_part_one_auto_revision"),
            models.CheckConstraint(condition=Q(trigger="manual", actor__isnull=False, client_operation_id__isnull=False, command__isnull=False)
                | Q(trigger="automatic", actor__isnull=True, client_operation_id__isnull=True, command__isnull=True), name="ai_part_check_trigger"),
            models.CheckConstraint(condition=Q(status__in=["queued", "running"], outcome="", finished_at__isnull=True, evidence__isnull=True)
                | Q(status="finished", outcome="unresolved", finished_at__isnull=False, evidence__isnull=True)
                | Q(status="finished", outcome__in=["accepted_by_vk", "not_sent_verified"], finished_at__isnull=False, evidence__isnull=False),
                name="ai_part_check_outcome"),
        ]


class AIPartReconciliationEvidence(models.Model):
    reconciliation = models.OneToOneField(AIPartReconciliation, on_delete=models.PROTECT)
    source_kind = models.CharField(max_length=32)
    rule_version = models.PositiveIntegerField(default=1)
    observed_at = models.DateTimeField()
    facts = models.JSONField()

    class Meta:
        db_table = "support_ai_part_reconciliation_evidence"


class HandoffPlan(models.Model):
    dialog = models.ForeignKey(Dialog, on_delete=models.PROTECT)
    mode_version = models.PositiveIntegerField()
    automation_epoch = models.PositiveBigIntegerField()
    fingerprint = models.CharField(max_length=64, unique=True)
    inputs_snapshot = models.JSONField()
    budget = models.DurationField()
    initial_deadline_at = models.DateTimeField()
    handoff_deadline_at = models.DateTimeField()
    timer_revision = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=12, default="pending")
    ticket = models.ForeignKey(Ticket, null=True, on_delete=models.PROTECT, related_name="handoff_plans")
    work_snapshot = models.JSONField(default=list)
    created_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "support_handoff_plan"
        indexes = [models.Index(fields=["handoff_deadline_at", "id"], condition=Q(status="pending"), name="support_handoff_due")]
        constraints = [
            models.CheckConstraint(condition=Q(timer_revision__gte=1), name="support_handoff_revision"),
            models.CheckConstraint(
                condition=Q(status="pending", ticket__isnull=True, finished_at__isnull=True)
                | Q(status="transferred", ticket__isnull=False, finished_at__isnull=False)
                | Q(status="superseded", ticket__isnull=True, finished_at__isnull=False),
                name="support_handoff_outcome",
            ),
        ]


class HandoffPlanAnswer(models.Model):
    plan = models.ForeignKey(HandoffPlan, on_delete=models.PROTECT, related_name="answers")
    answer = models.OneToOneField(AIAnswer, on_delete=models.PROTECT)
    required_part_ids = models.JSONField()

    class Meta:
        db_table = "support_handoff_plan_answer"
