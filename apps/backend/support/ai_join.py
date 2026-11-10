"""Explicit staff intervention in one selected automatic processing cycle (§16.23)."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.urls import reverse

from accounts.services import AccessError
from storage.services import database_time
from vk_channel.models import LongPollCursor
from workspace.services import notify_assignment

from .models import AIProcessingRun, Dialog, Ticket, TicketEvent
from .queries import allowed_communities, ticket_data
from .services import reserve_command, support_actor

ACTION = "join_ai_dialog"
INACTIVE_REASONS = {"superseded", "recovered_from_backup", "student_resolved"}


def completion_deadline(now, reason=""):
    return None if reason in INACTIVE_REASONS else now + timedelta(seconds=settings.SUPPORT_AI_JOIN_WINDOW_SECONDS)


def block_reason(run, dialog, cursor, now):
    if (dialog.mode != "ai_auto" or dialog.mode_version != run.mode_version
            or cursor.automation_epoch != run.automation_epoch):
        return "ai_dialog_stale"
    inputs = list(run.inputs.all())
    if not inputs or any(
        item.request_version != item.request.request_version or item.request.mode_version != run.mode_version
        or item.request.automation_epoch != run.automation_epoch for item in inputs
    ):
        return "ai_dialog_stale"
    deadline = run.processing_deadline_at if run.status == "running" else run.join_until
    if (cursor.automation_paused or run.reason in INACTIVE_REASONS or not deadline or now >= deadline
            or not any(item.request.resolved_at is None for item in inputs)):
        return "ai_dialog_inactive"
    return ""


def state(run, *, now=None):
    # Rendering is read-only. POST repeats the checks under the same mode locks as delivery.
    can_join = not run.handoff_ticket_id and not block_reason(
        run, run.dialog, run.dialog.cursor, now or database_time(),
    ) and not Ticket.objects.filter(dialog=run.dialog, status=Ticket.Status.OPEN).exists()
    return {"can_join": bool(can_join), **({"run_id": str(run.pk), "expected_mode_version": run.mode_version}
                                         if can_join else {})}


def latest_state(actor, dialog_id):
    now = database_time()
    runs = AIProcessingRun.objects.filter(
        Q(status="running", processing_deadline_at__gt=now)
        | Q(status__in=["answer_saved", "no_answer"], join_until__gt=now),
        dialog_id=dialog_id, dialog__cursor_id__in=allowed_communities(actor), dialog__mode="ai_auto",
        mode_version=F("dialog__mode_version"), automation_epoch=F("dialog__cursor__automation_epoch"),
        handoff_ticket__isnull=True, dialog__cursor__automation_paused=False,
    ).select_related("dialog__cursor").prefetch_related("inputs__request").order_by("-created_at", "-pk")
    for run in runs:
        result = state(run, now=now)
        if result["can_join"]:
            return result
    return {"can_join": False}


@transaction.atomic
def join(session, run_id, operation_id, expected_version):
    actor = support_actor(session, lock=True)
    if not 0 < run_id < 2**63:
        raise AccessError("ai_processing_run_not_found", 404)
    original = AIProcessingRun.objects.filter(
        pk=run_id, dialog__cursor_id__in=allowed_communities(actor),
    ).values("dialog_id", "dialog__cursor_id").first()
    if original is None:
        raise AccessError("ai_processing_run_not_found", 404)
    cursor = LongPollCursor.objects.select_for_update().get(pk=original["dialog__cursor_id"])
    dialog = Dialog.objects.select_for_update().get(pk=original["dialog_id"])
    run = AIProcessingRun.objects.select_for_update().prefetch_related("inputs__request").get(pk=run_id, dialog=dialog)
    command, created = reserve_command(actor, operation_id, ACTION, "ai_processing_run", run.pk,
                                      {"expected_mode_version": expected_version})
    if not created:
        event = TicketEvent.objects.select_related("ticket").get(command=command)
        ticket = Ticket.objects.select_for_update().get(pk=event.ticket_id)
        if (ticket.status != Ticket.Status.OPEN or ticket.current_owner_id != actor.pk
                or ticket.assignment_version != event.assignment_version):
            raise AccessError("operation_superseded", 409, ticket=ticket_data(ticket))
        return ticket, event.payload["mode_version"], True
    # A stale caller cannot use an unrelated open card to bypass version checks.
    if expected_version != run.mode_version:
        raise AccessError("ai_dialog_stale", 409)
    if run.handoff_ticket_id:
        ticket = Ticket.objects.select_for_update().filter(
            pk=run.handoff_ticket_id, dialog=dialog, status=Ticket.Status.OPEN,
        ).first()
        if ticket:
            raise AccessError("human_ticket_exists", 409, ticket=ticket_data(ticket),
                              ticket_url=reverse("support-ticket", args=[ticket.pk]))
    reason = block_reason(run, dialog, cursor, database_time())
    if reason:
        raise AccessError(reason, 409)
    if Ticket.objects.filter(dialog=dialog, status=Ticket.Status.OPEN).exists():
        raise AccessError("ai_dialog_stale", 409)

    from .handoff_plans import finish_explicit_handoff, work_for_ticket

    request_ids = [item.request_id for item in run.inputs.select_related("request")
                   if item.request.resolved_at is None]
    work = work_for_ticket(cursor, dialog, [], planned_request_ids=request_ids, reason="manager_joined")
    ticket = Ticket.objects.create(dialog=dialog, current_owner=actor, handoff_reason="manager_joined",
                                   created_at=database_time())
    finish_explicit_handoff(cursor, dialog, ticket, planned_request_ids=request_ids, force=True)
    # Includes a completed no-answer result, and running results cancelled by the handoff.
    run.handoff_ticket = ticket
    run.save(update_fields=["handoff_ticket"])
    event = TicketEvent.objects.create(
        ticket=ticket, command=command, assignment_version=ticket.assignment_version,
        previous_owner_id=None, new_owner_id=actor.pk,
        payload={"run_id": str(run.pk), "previous_mode_version": run.mode_version,
                 "mode_version": dialog.mode_version, "work_snapshot": work},
    )
    notify_assignment(event)
    return ticket, dialog.mode_version, False
