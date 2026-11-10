"""Accept locally, durably mark independently, then finalize and clean locally."""

from uuid import NAMESPACE_OID, uuid5

from django.conf import settings
from django.db import transaction
from django.db.models import CharField, Subquery
from django.db.models.expressions import RawSQL
from django.db.models.functions import Cast

from accounts.services import AccessError
from jobs.services import LeaseLost, enqueue
from notifications.event_messages import cancellation_snapshot, create_cancellation, delivery_footprint
from notifications.models import CancellationBatch, ChannelMessage
from participants.models import PersonOrigin
from vk_channel.services import database_now

from . import deletion_ledger as ledger
from .forms import digest
from .models import Application, ApplicationPlayer, DeletionIntent, Event, EventCommand, EventHistory, FormRevision
from .services import number, organiser, record, reserve_command, revision, staff_actor

COMMIT_TASK = "events.commit_deletion"
CLEANUP_TASK = "events.clean_deleted_data"


def intent_data(intent):
    batch = CancellationBatch.objects.filter(deletion=intent).first()
    return {"id": str(intent.pk), "event_uuid": str(intent.event_uuid), "status": intent.status,
        "created_at": intent.created_at, "ledger_recorded_at": intent.ledger_recorded_at,
        "finished_at": intent.finished_at, "cleaned_at": intent.cleaned_at, "notice_mode": intent.notice_mode,
        "notification_verification_required": intent.notification_verification_required or bool(batch and batch.verification_required),
        "notification_batch_id": str(batch.pk) if batch else None}


def preview_data(event):
    if event.started_at or event.state not in {"planned", "cancelled"}:
        raise AccessError("event_state_conflict", 409)
    footprint = delivery_footprint(event)
    applications = list(event.applications.order_by("pk").values("id", "status", "revision"))
    version = digest({"event_uuid": str(event.stable_id), "event_revision": event.revision,
                      "applications": applications, "deliveries": footprint})
    return {"deletion_preview_version": version, "event_uuid": str(event.stable_id), "event_revision": event.revision,
        "title": event.title, "planned_start_at": event.planned_start_at,
        "submitted_count": sum(item["status"] != "draft" for item in applications),
        "notification_choice_required": footprint["observed"],
        "notification_eligible_count": sum(item["eligible"] for item in footprint["recipients"]),
        "notification_excluded_count": sum(not item["eligible"] for item in footprint["recipients"]),
        "journal_configured": bool(settings.APP_ENVIRONMENT_ID and settings.EVENT_DELETION_LEDGER_DSN),
        "notification_snapshot": cancellation_snapshot(event, footprint)}


@transaction.atomic
def deletion_preview(session, event_id):
    actor, _ = staff_actor(session)
    event = Event.objects.select_for_update().filter(pk=event_id).first()
    if event is None or event.deleted_at:
        raise AccessError("event_not_found", 404)
    if event.deletion_pending or event.restoration_verification_required:
        raise AccessError("event_temporarily_unavailable", 503)
    if not organiser(event, actor):
        raise AccessError("event_organiser_required", 403)
    return preview_data(event)


def parameters(event_uuid, expected_revision, preview_version, notice_mode):
    number(expected_revision)
    if (not isinstance(preview_version, str) or len(preview_version) != 64
            or not isinstance(notice_mode, str) or notice_mode not in {"none", "notify", "already_notified"}):
        raise AccessError("invalid_request", 400)
    return {"event_uuid": str(event_uuid), "revision": expected_revision, "preview": preview_version, "notice_mode": notice_mode}


def request_fingerprint(event_id, params):
    return digest({"action": "delete_event", "event_id": event_id, "parameters": params})


def missing_result(session, event_id, event_uuid, operation_id, fingerprint):
    # No application transaction or row lock is held across this independent read.
    try:
        mark = ledger.read_mark(settings.APP_ENVIRONMENT_ID, event_uuid)
    except (ledger.LedgerUnavailable, ledger.LedgerMismatch):
        raise AccessError("deletion_journal_unavailable", 503) from None
    with transaction.atomic():
        actor, _ = staff_actor(session)
        if mark is None or str(mark["actor_uuid"]) != str(actor.stable_id) or mark["operation_id"] != operation_id:
            raise AccessError("event_not_found", 404)
        if mark["request_fingerprint"] != fingerprint:
            raise AccessError("operation_mismatch", 409)
        event = Event.objects.select_for_update().filter(stable_id=event_uuid).first()
        intent = restore_mark(mark, event)
        return intent, True


def request_deletion(session, event_id, event_uuid, operation_id, expected_revision, preview_version, notice_mode):
    params = parameters(event_uuid, expected_revision, preview_version, notice_mode)
    fingerprint = request_fingerprint(event_id, params)
    with transaction.atomic():
        actor, _ = staff_actor(session)
        event = Event.objects.select_for_update().filter(pk=event_id, stable_id=event_uuid).first()
        if event is not None:
            if event.restoration_verification_required:
                raise AccessError("event_temporarily_unavailable", 503)
            existing_command = EventCommand.objects.filter(actor=actor, owner__isnull=True, client_operation_id=operation_id).first()
            if existing_command:
                if existing_command.action != "delete_event" or existing_command.fingerprint != fingerprint:
                    raise AccessError("operation_mismatch", 409)
                intent = DeletionIntent.objects.filter(pk=existing_command.result.get("deletion_id")).first()
                if intent:
                    return intent, True
            current = DeletionIntent.objects.filter(event=event).first()
            if not organiser(event, actor):
                raise AccessError("event_organiser_required", 403)
            command, created = reserve_command(actor, operation_id, "delete_event", event_id, params)
            if current:
                command.result = {"deletion_id": str(current.pk)}
                command.save(update_fields=["result"])
                return current, not created
            if not settings.APP_ENVIRONMENT_ID or not settings.EVENT_DELETION_LEDGER_DSN:
                raise AccessError("deletion_journal_unavailable", 503)
            revision(event, expected_revision)
            preview = preview_data(event)
            if preview_version != preview["deletion_preview_version"]:
                raise AccessError("event_deletion_preview_changed", 409)
            if preview["notification_choice_required"] and notice_mode == "none":
                raise AccessError("deletion_notification_choice_required", 409)
            intent = DeletionIntent.objects.create(command=command, event=event, environment=settings.APP_ENVIRONMENT_ID,
                event_uuid=event.stable_id, actor_uuid=actor.stable_id, client_operation_id=operation_id,
                request_fingerprint=command.fingerprint, notice_mode=notice_mode,
                notification_snapshot=preview["notification_snapshot"] if notice_mode == "notify" else {})
            event.deletion_pending = True
            record(command, event, "deletion_requested", {"deletion_id": str(intent.pk), "notice_mode": notice_mode},
                   result={"deletion_id": str(intent.pk)})
            intent.task = enqueue(COMMIT_TASK, intent.pk, {"deletion_id": str(intent.pk)}, priority=20, max_attempts=1_000_000)
            intent.save(update_fields=["task"])
            return intent, False
    return missing_result(session, event_id, event_uuid, operation_id, fingerprint)


def finalize(intent, mark):
    now = database_now()
    if intent.event_id:
        event = Event.objects.select_for_update().get(pk=intent.event_id)
        if event.stable_id != intent.event_uuid or event.started_at:
            intent.status = "needs_review"
            intent.save(update_fields=["status"])
            return
        event.state, event.deleted_at, event.deletion_pending = "deleted", now, False
        event.revision += 1
        event.save(update_fields=["state", "deleted_at", "deletion_pending", "revision"])
    intent.status, intent.finished_at, intent.ledger_recorded_at = "deleted", now, mark["recorded_at"]
    intent.save(update_fields=["status", "finished_at", "ledger_recorded_at"])
    if intent.notice_mode == "notify":
        create_cancellation(intent)
    if intent.event_id:
        intent.cleanup_task = enqueue(CLEANUP_TASK, intent.pk, {"deletion_id": str(intent.pk)}, priority=20)
        intent.save(update_fields=["cleanup_task"])


def commit_deletion_task(task):
    intent = DeletionIntent.objects.get(pk=task.payload["deletion_id"])
    if intent.status != "awaiting_ledger":
        return lambda: {"status": intent.status}
    try:
        mark = ledger.append_mark(intent)
        mismatch = False
    except ledger.LedgerUnavailable:
        raise OSError("Deletion journal is temporarily unavailable") from None
    except ledger.LedgerMismatch:
        mark, mismatch = None, True

    def save():
        if intent.event_id:
            Event.objects.select_for_update().get(pk=intent.event_id)
        current = DeletionIntent.objects.select_for_update().get(pk=intent.pk)
        if current.task_id != task.pk or current.status != "awaiting_ledger":
            raise LeaseLost(task.pk)
        if mismatch:
            current.status = "needs_review"
            current.save(update_fields=["status"])
            return {"status": "needs_review", "reason": "deletion_journal_mismatch"}
        finalize(current, mark)
        return {"deletion_id": str(current.pk), "status": current.status}

    return save


def cleanup_task(task):
    def save():
        head = DeletionIntent.objects.get(pk=task.payload["deletion_id"])
        if head.event_id:
            event = Event.objects.select_for_update().get(pk=head.event_id)
        intent = DeletionIntent.objects.select_for_update().get(pk=head.pk)
        if intent.status != "deleted" or not intent.ledger_recorded_at or intent.cleanup_task_id != task.pk:
            raise LeaseLost(task.pk)
        if intent.cleaned_at or intent.event_id is None:
            return {"status": "already_cleaned"}
        if event.stable_id != intent.event_uuid or event.state != "deleted":
            raise LeaseLost(task.pk)
        player_keys = ApplicationPlayer.objects.filter(application__event=event).annotate(source_key=Cast("pk", CharField())).values("source_key")
        PersonOrigin.objects.filter(kind="application_player", source_id__in=Subquery(player_keys)).update(values={})
        ApplicationPlayer.objects.filter(application__event=event).update(values={}, person=None, is_active=False)
        Application.objects.filter(event=event).update(values={}, participant_explanation="", validation_digest="")
        EventHistory.objects.filter(event=event).update(payload={"data_cleared_for_deletion": True})
        EventCommand.objects.filter(event=event).exclude(action="delete_event").update(result={"event_deleted": True, "deletion_id": str(intent.pk)})
        FormRevision.objects.filter(event=event).update(schema={})
        ChannelMessage.objects.filter(event=event).exclude(kind="event_cancellation").exclude(text="").update(
            text_sha256=RawSQL("encode(sha256(convert_to(text, 'UTF8')), 'hex')", []), text="", keyboard=None)
        event.title, event.description, event.location, event.sport = "", "", "", ""
        event.save(update_fields=["title", "description", "location", "sport"])
        intent.cleaned_at = database_now()
        intent.save(update_fields=["cleaned_at"])
        return {"status": "cleaned", "deletion_id": str(intent.pk)}

    return save


def restore_mark(mark, event=None):
    intent, created = DeletionIntent.objects.get_or_create(environment=mark["environment"], event_uuid=mark["event_uuid"], defaults={
        "pk": mark["deletion_id"], "event": event, "actor_uuid": mark["actor_uuid"], "client_operation_id": mark["operation_id"],
        "request_fingerprint": mark["request_fingerprint"], "notice_mode": mark["notice_mode"],
        "notification_verification_required": True,
    })
    if not ledger.same_mark(mark, ledger.mark_values(intent)):
        if event:
            event.deletion_pending = True
            event.save(update_fields=["deletion_pending"])
        intent.status, intent.finished_at = "needs_review", None
        intent.save(update_fields=["status", "finished_at"])
        return intent
    if event and intent.event_id is None:
        intent.event = event
        intent.save(update_fields=["event"])
    if intent.status != "deleted":
        finalize(intent, mark)
    elif event and event.state != "deleted":
        finalize(intent, mark)
    return intent


def read_status(session, deletion_id):
    with transaction.atomic():
        actor, _ = staff_actor(session)
        intent = DeletionIntent.objects.filter(pk=deletion_id, environment=settings.APP_ENVIRONMENT_ID).first()
        if intent:
            if intent.event_id is None and intent.actor_uuid != actor.stable_id:
                raise AccessError("deletion_not_found", 404)
            return intent
    try:
        mark = ledger.read_deletion(settings.APP_ENVIRONMENT_ID, deletion_id)
    except (ledger.LedgerUnavailable, ledger.LedgerMismatch):
        raise AccessError("deletion_journal_unavailable", 503) from None
    with transaction.atomic():
        actor, _ = staff_actor(session)
        if mark is None or mark["actor_uuid"] != actor.stable_id:
            raise AccessError("deletion_not_found", 404)
        event = Event.objects.select_for_update().filter(stable_id=mark["event_uuid"]).first()
        return restore_mark(mark, event)
