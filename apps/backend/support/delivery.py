from datetime import timedelta
from secrets import randbelow

from django.conf import settings
from django.db import transaction
from django.db.models import F

from accounts.policies import has_work_access, quarantine_incident
from accounts.services import AccessError, lock_profiles
from jobs.models import BackgroundTask as Task
from jobs.services import ClockTimestamp, LeaseLost, admit_external_call, claim, enqueue, finish
from vk_channel.client import DeliveryRejected, DeliveryUnknown
from vk_channel.services import database_now
from vk_channel.student_actions import issue_ticket_action

from .models import Dialog, Draft, Message, OutgoingAttempt as Attempt, OutgoingResponse as Response, Ticket
from .queries import allowed_communities, ticket_data
from .services import reserve_command, support_actor, visible_dialog

TASK_TYPE = "support.send_response"


def assert_assignment(ticket, actor, version, *, replay=False):
    if ticket.status != Ticket.Status.OPEN or ticket.current_owner_id != actor.pk or ticket.assignment_version != version:
        code = "operation_state_changed" if replay else "assignment_version_conflict"
        raise AccessError(code, 409, ticket=ticket_data(ticket))


def create_attempt(response, command, kind):
    task = enqueue(TASK_TYPE, command.client_operation_id, {"response_id": response.pk})
    return Attempt.objects.create(response=response, command=command, task=task, attempt_type=kind)


@transaction.atomic
def send_response(session, ticket_id, operation_id, version, text, source_draft):
    actor = support_actor(session, lock=True)
    dialog_id = Ticket.objects.filter(pk=ticket_id).values_list("dialog_id", flat=True).first()
    visible_dialog(actor, dialog_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    command, created = reserve_command(actor, operation_id, "send", "ticket", ticket.pk, {
        "version": version, "text": text, "source_draft": source_draft,
    })
    assert_assignment(ticket, actor, version, replay=not created)
    if not created:
        return Attempt.objects.select_related("response").get(command=command), True
    draft_id = revision = None
    if source_draft:
        draft_id, revision = source_draft
        draft = Draft.objects.select_for_update().filter(pk=draft_id, ticket=ticket, author=actor).first()
        if draft is None or draft.revision != revision or draft.text != text:
            raise AccessError("draft_revision_conflict", 409)
    for _ in range(8):
        response, inserted = Response.objects.get_or_create(vk_random_id=randbelow(2**31 - 1) + 1, defaults={
            "ticket": ticket, "command": command, "assignment_version": version, "text": text,
            "source_draft_id_snapshot": draft_id, "source_draft_revision": revision,
        })
        if inserted:
            return create_attempt(response, command, "initial"), False
    raise AccessError("send_unavailable", 503)


@transaction.atomic
def retry_response(session, response_id, operation_id, version):
    actor = support_actor(session, lock=True)
    original = Response.objects.filter(pk=response_id, ticket__dialog__cursor_id__in=allowed_communities(actor)).first()
    if original is None:
        raise AccessError("response_not_found", 404)
    ticket = Ticket.objects.select_for_update().get(pk=original.ticket_id)
    response = Response.objects.select_for_update().get(pk=response_id)
    command, created = reserve_command(actor, operation_id, "manual_retry", "response", response.pk, {"version": version})
    assert_assignment(ticket, actor, version, replay=not created)
    if response.assignment_version != version:
        raise AccessError("assignment_version_conflict", 409, ticket=ticket_data(ticket))
    if not created:
        return Attempt.objects.select_related("response").get(command=command), True
    if response.recovery_verification_required:
        raise AccessError("recovery_verification_required", 409)
    errors = {
        Response.Status.UNKNOWN: "outcome_unknown", Response.Status.PENDING: "response_in_progress",
        Response.Status.SENT: "response_already_sent", Response.Status.CANCELLED: "assignment_version_conflict",
    }
    if response.status != Response.Status.FAILED:
        raise AccessError(errors[response.status], 409)
    response.status = Response.Status.PENDING
    response.save(update_fields=["status"])
    return create_attempt(response, command, "manual_retry"), False


def lock_delivery(task_id):
    dialog_id, ticket_id, response_id, attempt_id = Attempt.objects.filter(task_id=task_id).values_list(
        "response__ticket__dialog_id", "response__ticket_id", "response_id", "pk",
    ).get()
    Dialog.objects.select_for_update().get(pk=dialog_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    response = Response.objects.select_for_update().get(pk=response_id)
    attempt = Attempt.objects.select_for_update().get(pk=attempt_id)
    return ticket, response, attempt


def record_outcome(ticket, response, attempt, status, *, provider_id=None, reason="", error_code=None):
    attempt.status = status
    attempt.finished_at = None if status == Attempt.Status.UNKNOWN else database_now()
    attempt.failure_reason = reason
    attempt.vk_error_code = error_code
    if status == Attempt.Status.SUCCEEDED:
        response.status = Response.Status.SENT
        response.provider_message_id = provider_id
        response.message = Message.objects.create(
            ticket=ticket, dialog_id=ticket.dialog_id, direction="outbound_manager", text=response.text,
        )
        if ticket.status == Ticket.Status.OPEN and response.source_draft_id_snapshot is not None:
            Draft.objects.filter(
                pk=response.source_draft_id_snapshot, revision=response.source_draft_revision,
                author_id=response.command.actor_id, ticket=ticket,
            ).update(text="", revision=F("revision") + 1, updated_at=attempt.finished_at)
    elif status == Attempt.Status.UNKNOWN:
        response.status = Response.Status.UNKNOWN
    elif response.status != Response.Status.CANCELLED:
        response.status = Response.Status.FAILED
    attempt.save(update_fields=["status", "finished_at", "failure_reason", "vk_error_code"])
    response.failure_reason = reason
    response.save(update_fields=["status", "message", "provider_message_id", "failure_reason"])


@transaction.atomic
def admit(task, community_id):
    author_id = Attempt.objects.filter(task_id=task.pk).values_list("response__command__actor_id", flat=True).get()
    actor = lock_profiles(author_id).get(author_id)
    ticket, response, attempt = lock_delivery(task.pk)
    dialog = ticket.dialog
    if attempt.status != Attempt.Status.IN_FLIGHT or attempt.started_at is not None:
        raise LeaseLost(task.pk)
    if (
        quarantine_incident() or response.recovery_verification_required or dialog.cursor.community_id != community_id
        or not has_work_access(actor) or actor.disabled or not actor.bindings.filter(active=True).exists()
        or dialog.cursor_id not in allowed_communities(actor)
        or ticket.status != Ticket.Status.OPEN or ticket.current_owner_id != author_id
        or ticket.assignment_version != response.assignment_version or response.status != Response.Status.PENDING
    ):
        record_outcome(ticket, response, attempt, Attempt.Status.FAILED, reason="blocked_before_send")
        finish(task, {"reason": "blocked_before_send"}, status=Task.Status.FAILED)
        return None
    admit_external_call(task)
    attempt.started_at = database_now()
    attempt.save(update_fields=["started_at"])
    if settings.VK_CLICK_ANSWERS_VERIFIED and attempt.attempt_type == "initial":
        button = issue_ticket_action(
            ticket.pk, dialog.vk_peer_id, "resolve", attempt.started_at + timedelta(days=7),
        )
        response.vk_keyboard = {"inline": True, "buttons": [[{"action": button, "color": "secondary"}]]}
        response.save(update_fields=["vk_keyboard"])
    parameters = (dialog.vk_peer_id, response.vk_random_id, response.text)
    return (*parameters, response.vk_keyboard) if response.vk_keyboard else parameters


@transaction.atomic
def finish_delivery(task, status, **outcome):
    ticket, response, attempt = lock_delivery(task.pk)
    if attempt.status != Attempt.Status.IN_FLIGHT or attempt.started_at is None:
        raise LeaseLost(task.pk)
    record_outcome(ticket, response, attempt, status, **outcome)
    task_status = {
        Attempt.Status.SUCCEEDED: Task.Status.DONE, Attempt.Status.UNKNOWN: Task.Status.UNKNOWN,
        Attempt.Status.FAILED: Task.Status.FAILED,
    }[status]
    finish(task, {"attempt_id": attempt.pk}, status=task_status)


@transaction.atomic
def recover_expired_delivery():
    task_id = Task.objects.filter(
        task_type=TASK_TYPE, status=Task.Status.PROCESSING, locked_until__lte=ClockTimestamp(),
    ).order_by("locked_until", "pk").values_list("pk", flat=True).first()
    if task_id is None:
        return False
    ticket, response, attempt = lock_delivery(task_id)
    task = Task.objects.select_for_update().get(pk=task_id)
    now = database_now()
    if task.status != Task.Status.PROCESSING or task.locked_until > now:
        return True
    if attempt.started_at is not None or task.admitted_at is not None:
        record_outcome(ticket, response, attempt, Attempt.Status.UNKNOWN, reason="worker_lease_expired")
        task.status = Task.Status.UNKNOWN
    elif task.attempt_count >= task.max_attempts:
        record_outcome(ticket, response, attempt, Attempt.Status.FAILED, reason="worker_budget_exhausted")
        task.status = Task.Status.DEAD
    else:
        task.status = Task.Status.RETRY
        task.scheduled_at = now
    task.lease_token = task.locked_until = None
    task.updated_at = now
    task.save(update_fields=["status", "scheduled_at", "lease_token", "locked_until", "updated_at"])
    return True


def run_sender_once(community_id, send):
    while recover_expired_delivery():
        pass
    task = claim([TASK_TYPE])
    if task is None:
        return False
    try:
        parameters = admit(task, community_id)
        if parameters is not None:
            try:
                provider_id = send(*parameters)
            except DeliveryRejected as error:
                finish_delivery(task, Attempt.Status.FAILED, reason="vk_rejected", error_code=error.code)
            except (DeliveryUnknown, OSError):
                finish_delivery(task, Attempt.Status.UNKNOWN, reason="vk_confirmation_lost")
            else:
                finish_delivery(task, Attempt.Status.SUCCEEDED, provider_id=provider_id)
    except LeaseLost:
        pass
    return True
