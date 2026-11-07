from datetime import timedelta

from django.db import transaction

from accounts.policies import quarantine_incident
from accounts.services import AccessError
from jobs.models import BackgroundTask as Task
from jobs.services import ClockTimestamp, LeaseLost, claim, enqueue, finish, renew
from vk_channel.backfill import HistoryQuotaExceeded, use_history_quota
from vk_channel.client import DeliveryUnknown
from vk_channel.services import database_now

from .delivery import lock_delivery, record_outcome
from .models import (
    OutgoingAttempt as Attempt, OutgoingReconciliation as Check, OutgoingResponse as Response,
    ReconciliationEvidence, Ticket,
)
from .queries import allowed_communities, may_coordinate
from .services import reserve_command, support_actor

TASK_TYPE = "support.reconcile_response"
LOOKUP_WINDOW = timedelta(minutes=2)


def may_reconcile(actor, ticket):
    return (
        (ticket.status == Ticket.Status.OPEN and ticket.current_owner_id == actor.pk)
        or may_coordinate(actor, ticket.dialog.cursor_id)
    )


@transaction.atomic
def request_reconciliation(session, response_id, operation_id):
    actor = support_actor(session, lock=True)
    ticket_id = Response.objects.filter(
        pk=response_id, ticket__dialog__cursor_id__in=allowed_communities(actor),
    ).values_list("ticket_id", flat=True).first()
    if ticket_id is None:
        raise AccessError("response_not_found", 404)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    if not may_reconcile(actor, ticket):
        raise AccessError("permission_denied", 403)
    response = Response.objects.select_for_update().get(pk=response_id)
    command, created = reserve_command(actor, operation_id, "reconcile_response", "response", response.pk, {})
    if not created:
        return Check.objects.get(command=command), True
    if response.recovery_verification_required:
        code = "reconciliation_state_inconsistent" if response.status == Response.Status.UNKNOWN else "recovery_verification_required"
        raise AccessError(code, 409)
    if response.status != Response.Status.UNKNOWN:
        raise AccessError("reconciliation_not_required", 409)
    attempt = Attempt.objects.select_for_update().filter(response=response, status=Attempt.Status.UNKNOWN).first()
    if attempt is None:
        raise AccessError("reconciliation_state_inconsistent", 409)
    if Check.objects.filter(response=response).exclude(status=Check.Status.FINISHED).exists():
        raise AccessError("reconciliation_in_progress", 409)
    task = enqueue(TASK_TYPE, operation_id, {"attempt_id": attempt.pk})
    return Check.objects.create(
        response=response, command=command, target_attempt=attempt, task=task,
        lookup_deadline_at=database_now() + LOOKUP_WINDOW,
    ), False


def lock_check(task_id):
    outbound_id = Check.objects.filter(task_id=task_id).values_list("target_attempt__task_id", flat=True).get()
    ticket, response, attempt = lock_delivery(outbound_id)
    check = Check.objects.select_for_update().get(task_id=task_id)
    return ticket, response, attempt, check


def target_is_current(response, attempt, check):
    return (
        attempt.response_id == response.pk == check.response_id
        and attempt.pk == check.target_attempt_id
        and attempt.status == Attempt.Status.UNKNOWN
        and response.status == Response.Status.UNKNOWN
        and not response.recovery_verification_required
    )


def finish_check(check, outcome, reason=""):
    check.status = Check.Status.FINISHED
    check.outcome = outcome
    check.reason = reason
    check.finished_at = database_now()
    check.save(update_fields=["status", "outcome", "reason", "finished_at"])


@transaction.atomic
def begin_lookup(task, community_id):
    ticket, response, attempt, check = lock_check(task.pk)
    if check.status != Check.Status.QUEUED:
        raise LeaseLost(task.pk)
    dialog = ticket.dialog
    if not target_is_current(response, attempt, check):
        reason = "target_changed"
    elif check.lookup_deadline_at <= database_now():
        reason = "lookup_budget_exhausted"
    elif quarantine_incident() or dialog.cursor.community_id != community_id:
        reason = "history_unavailable"
    else:
        renew(task)
        check.status = Check.Status.RUNNING
        check.save(update_fields=["status"])
        return dialog.vk_peer_id, response.vk_random_id, response.text, attempt.started_at
    finish_check(check, Check.Outcome.UNRESOLVED, reason)
    finish(task, {"reason": reason})
    return None


def matching_message(items, community_id, peer_id, random_id, text, started_at):
    if started_at is None or not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return None
    candidates = [item for item in items if type(item.get("random_id")) is int and item["random_id"] == random_id]
    if len(candidates) != 1:
        return None
    item = candidates[0]
    identifier, date = item.get("id"), item.get("date")
    if (
        type(identifier) is not int or not 0 < identifier < 2**63
        or item.get("out") != 1 or item.get("from_id") != -community_id or item.get("peer_id") != peer_id
        or type(date) is not int or date < int(started_at.timestamp()) or item.get("text") != text
    ):
        return None
    return {"community_id": community_id, "peer_id": peer_id, "message_id": identifier,
            "direction": "outbound", "random_id": random_id, "message_date": date}


@transaction.atomic
def complete_lookup(task, evidence, reason):
    ticket, response, attempt, check = lock_check(task.pk)
    if check.status != Check.Status.RUNNING:
        raise LeaseLost(task.pk)
    jobs = {
        job.pk: job for job in Task.objects.select_for_update().filter(pk__in=[task.pk, attempt.task_id]).order_by("pk")
    }
    outgoing = jobs[attempt.task_id]
    if not target_is_current(response, attempt, check) or outgoing.status != Task.Status.UNKNOWN:
        evidence, reason = None, "target_changed"
    elif quarantine_incident():
        evidence, reason = None, "history_unavailable"
    elif check.lookup_deadline_at <= database_now():
        evidence, reason = None, "lookup_budget_exhausted"
    elif evidence and Response.objects.filter(
        ticket__dialog_id=ticket.dialog_id, provider_message_id=evidence["message_id"],
    ).exclude(pk=response.pk).exists():
        evidence, reason = None, "no_unique_match"

    outcome = Check.Outcome.UNRESOLVED
    if evidence:
        ReconciliationEvidence.objects.create(
            reconciliation=check, source_kind="vk_history", rule_version=1,
            observed_at=database_now(), facts=evidence,
        )
        record_outcome(ticket, response, attempt, Attempt.Status.SUCCEEDED, provider_id=evidence["message_id"])
        outgoing.status = Task.Status.DONE
        outgoing.result = {"attempt_id": attempt.pk, "reconciliation_id": check.pk}
        outgoing.updated_at = database_now()
        outgoing.save(update_fields=["status", "result", "updated_at"])
        outcome = Check.Outcome.ACCEPTED
    finish_check(check, outcome, reason)
    finish(task, {"reconciliation_id": check.pk, "outcome": outcome})


@transaction.atomic
def recover_expired_lookup():
    task_id = Task.objects.filter(
        task_type=TASK_TYPE, status=Task.Status.PROCESSING, locked_until__lte=ClockTimestamp(),
    ).order_by("locked_until", "pk").values_list("pk", flat=True).first()
    if task_id is None:
        return False
    _, response, attempt, check = lock_check(task_id)
    task = Task.objects.select_for_update().get(pk=task_id)
    now = database_now()
    if task.status != Task.Status.PROCESSING or task.locked_until > now:
        return True
    if not target_is_current(response, attempt, check):
        task.status = Task.Status.DONE
        finish_check(check, Check.Outcome.UNRESOLVED, "target_changed")
    elif task.attempt_count >= task.max_attempts or check.lookup_deadline_at <= now:
        task.status = Task.Status.DEAD
        finish_check(check, Check.Outcome.UNRESOLVED, "lookup_budget_exhausted")
    else:
        task.status = Task.Status.RETRY
        task.scheduled_at = now
        check.status = Check.Status.QUEUED
        check.save(update_fields=["status"])
    task.lease_token = task.locked_until = None
    task.updated_at = now
    task.save(update_fields=["status", "scheduled_at", "lease_token", "locked_until", "updated_at"])
    return True


def run_reconciliation_once(community_id, read_history):
    while recover_expired_lookup():
        pass
    task = claim([TASK_TYPE])
    if task is None:
        return False
    try:
        parameters = begin_lookup(task, community_id)
        if parameters is not None:
            peer_id, random_id, text, started_at = parameters
            try:
                use_history_quota(community_id)
                items = read_history(peer_id)
            except HistoryQuotaExceeded:
                complete_lookup(task, None, "history_quota")
            except (DeliveryUnknown, OSError):
                complete_lookup(task, None, "history_unavailable")
            else:
                evidence = matching_message(items, community_id, peer_id, random_id, text, started_at)
                complete_lookup(task, evidence, "" if evidence else "no_unique_match")
    except LeaseLost:
        pass
    return True
