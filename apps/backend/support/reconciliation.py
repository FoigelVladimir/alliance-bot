from datetime import timedelta

from django.db import transaction

from accounts.policies import quarantine_incident
from accounts.services import AccessError
from jobs.models import BackgroundTask as Task
from jobs.services import ClockTimestamp, LeaseLost, claim, enqueue, finish, renew
from vk_channel.history import HistoryQuotaExceeded, read_history as guarded_history_read
from vk_channel.client import DeliveryUnknown
from vk_channel.services import database_now

from .delivery import record_outcome
from .models import (
    Dialog, Message,
    OutgoingAttempt as Attempt, OutgoingReconciliation as Check, OutgoingResponse as Response,
    ReconciliationEvidence, ResponseRecoveryGuard as Guard, Ticket,
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
    guard = Guard.objects.select_for_update().filter(response=response, resolved_at__isnull=True).first()
    attempt = Attempt.objects.select_for_update().filter(response=response, status=Attempt.Status.UNKNOWN).first()
    if (bool(guard) != response.recovery_verification_required
            or (guard and (attempt or response.status != Response.Status.FAILED))
            or (not guard and response.status == Response.Status.UNKNOWN and attempt is None)):
        raise AccessError("reconciliation_state_inconsistent", 409)
    if guard is None and (response.status != Response.Status.UNKNOWN or attempt is None):
        raise AccessError("reconciliation_not_required", 409)
    if Check.objects.filter(response=response).exclude(status=Check.Status.FINISHED).exists():
        raise AccessError("reconciliation_in_progress", 409)
    task = enqueue(TASK_TYPE, operation_id, {"recovery_guard_id": guard.pk} if guard else {"attempt_id": attempt.pk})
    return Check.objects.create(
        response=response, command=command, target_attempt=None if guard else attempt, target_recovery_guard=guard, task=task,
        lookup_deadline_at=database_now() + LOOKUP_WINDOW,
    ), False


def lock_check(task_id):
    response_id, ticket_id, dialog_id = Check.objects.filter(task_id=task_id).values_list(
        "response_id", "response__ticket_id", "response__ticket__dialog_id",
    ).get()
    Dialog.objects.select_for_update().get(pk=dialog_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    response = Response.objects.select_for_update().get(pk=response_id)
    target_attempt_id, guard_id = Check.objects.filter(task_id=task_id).values_list(
        "target_attempt_id", "target_recovery_guard_id",
    ).get()
    target = (Guard.objects.select_for_update().get(pk=guard_id) if guard_id
              else Attempt.objects.select_for_update().get(pk=target_attempt_id))
    check = Check.objects.select_for_update().get(task_id=task_id)
    return ticket, response, target, check


def target_is_current(response, attempt, check):
    if check.target_recovery_guard_id:
        return (
            attempt.pk == check.target_recovery_guard_id and attempt.response_id == response.pk == check.response_id
            and attempt.resolved_at is None and response.recovery_verification_required
            and response.status == Response.Status.FAILED
            and not response.attempts.filter(status__in=[Attempt.Status.IN_FLIGHT, Attempt.Status.UNKNOWN]).exists()
        )
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
        since = (response.command.created_at
                 if check.target_recovery_guard_id or attempt.failure_reason == "recovered_from_backup"
                 else attempt.started_at)
        return dialog.vk_peer_id, response.vk_random_id, response.text, since
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
    recovery = bool(check.target_recovery_guard_id)
    job_ids = [task.pk] if recovery else [task.pk, attempt.task_id]
    jobs = {job.pk: job for job in Task.objects.select_for_update().filter(pk__in=job_ids).order_by("pk")}
    outgoing = None if recovery else jobs[attempt.task_id]
    if not target_is_current(response, attempt, check) or (outgoing and (
        outgoing.status != Task.Status.UNKNOWN or outgoing.task_type != "support.send_response"
        or outgoing.payload != {"response_id": response.pk, "attempt_id": attempt.pk}
    )):
        evidence, reason = None, "target_changed"
    elif quarantine_incident():
        evidence, reason = None, "history_unavailable"
    elif check.lookup_deadline_at <= database_now():
        evidence, reason = None, "lookup_budget_exhausted"
    elif evidence and evidence.get("message_id") and Response.objects.filter(
        ticket__dialog_id=ticket.dialog_id, provider_message_id=evidence["message_id"],
    ).exclude(pk=response.pk).exists():
        evidence, reason = None, "no_unique_match"

    outcome = Check.Outcome.UNRESOLVED
    if evidence:
        outcome = Check.Outcome.NOT_SENT if evidence.get("not_sent") else Check.Outcome.ACCEPTED
        if outcome == Check.Outcome.NOT_SENT:
            evidence = None if recovery else local_non_admission(response, attempt)
            if not evidence:
                reason, outcome = "no_unique_match", Check.Outcome.UNRESOLVED
        elif recovery:
            evidence = {**evidence, "recovery_run_id": str(attempt.recovery_run_id),
                        "interval_start": response.command.created_at.isoformat(),
                        "interval_end": database_now().isoformat(), "coverage_basis": "unique_correlated_message"}
    if evidence:
        proof = ReconciliationEvidence.objects.create(
            reconciliation=check, source_kind="local_non_admission" if outcome == Check.Outcome.NOT_SENT else "vk_history", rule_version=1,
            observed_at=database_now(), facts=evidence,
        )
        if recovery:
            response.message = Message.objects.create(
                ticket=ticket, dialog_id=ticket.dialog_id, direction="outbound_manager", text=response.text,
            )
            response.status, response.provider_message_id = Response.Status.SENT, evidence["message_id"]
            response.confirmed_sent_at, response.recovery_verification_required = database_now(), False
            response.failure_reason = ""
            response.save(update_fields=["message", "status", "provider_message_id", "confirmed_sent_at", "recovery_verification_required", "failure_reason"])
            attempt.resolved_at, attempt.resolution, attempt.evidence_ref = database_now(), outcome, proof
            attempt.save(update_fields=["resolved_at", "resolution", "evidence_ref"])
        else:
            status = Attempt.Status.FAILED if outcome == Check.Outcome.NOT_SENT else Attempt.Status.SUCCEEDED
            record_outcome(ticket, response, attempt, status, provider_id=evidence.get("message_id"),
                           reason="not_sent_verified" if outcome == Check.Outcome.NOT_SENT else "")
            outgoing.status = Task.Status.FAILED if outcome == Check.Outcome.NOT_SENT else Task.Status.DONE
            outgoing.result = {"attempt_id": attempt.pk, "reconciliation_id": check.pk}
            outgoing.updated_at = database_now()
            outgoing.save(update_fields=["status", "result", "updated_at"])
    finish_check(check, outcome, reason)
    finish(task, {"reconciliation_id": check.pk, "outcome": outcome})


def local_non_admission(response, attempt):
    """Negative proof is unavailable for any logical response touched by restoration."""
    if response.recovery_guards.exists() or response.attempts.filter(failure_reason="recovered_from_backup").exists():
        return None
    calls = list(response.attempts.select_related("task").order_by("pk"))
    if not calls or any(item.started_at is not None or item.task.admitted_at is not None for item in calls):
        return None
    if any(item.pk != attempt.pk and (
        item.status != Attempt.Status.FAILED or item.task.status not in (Task.Status.FAILED, Task.Status.DEAD)
    ) for item in calls):
        return None
    return {"not_sent": True, "response_id": response.pk, "attempt_ids": [item.pk for item in calls],
            "task_ids": [item.task_id for item in calls], "no_external_admission": True}


@transaction.atomic
def complete_local_lookup(task):
    _, response, target, check = lock_check(task.pk)
    if check.target_recovery_guard_id or not target_is_current(response, target, check):
        return False
    list(Task.objects.select_for_update().filter(pk__in=[task.pk, target.task_id]).order_by("pk"))
    proof = local_non_admission(response, target)
    if not proof:
        return False
    complete_lookup(task, proof, "")
    return True


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


def defer_read_task(task, check, retry_at):
    """The caller holds the domain and check locks for a still-current target."""
    renew(task)
    job = Task.objects.select_for_update().get(pk=task.pk)
    now = database_now()
    if job.attempt_count >= job.max_attempts or retry_at >= check.lookup_deadline_at or now >= check.lookup_deadline_at:
        finish_check(check, "unresolved", "lookup_budget_exhausted")
        job.status = Task.Status.DEAD if job.attempt_count >= job.max_attempts else Task.Status.DONE
    else:
        check.status = "queued"
        check.save(update_fields=["status"])
        job.status, job.scheduled_at = Task.Status.RETRY, retry_at
    job.lease_token = job.locked_until = None
    job.updated_at = now
    job.save(update_fields=["status", "scheduled_at", "lease_token", "locked_until", "updated_at"])


@transaction.atomic
def defer_for_quota(task, retry_at):
    _, response, attempt, check = lock_check(task.pk)
    if not target_is_current(response, attempt, check):
        finish_check(check, Check.Outcome.UNRESOLVED, "target_changed")
        finish(task, {"reason": "target_changed"})
        return
    defer_read_task(task, check, retry_at or database_now() + timedelta(seconds=1))


def run_reconciliation_once(community_id, read_history=None):
    while recover_expired_lookup():
        pass
    own_tasks = Check.objects.filter(response__ticket__dialog__cursor__community_id=community_id).values_list("task_id", flat=True)
    task = claim([TASK_TYPE], task_ids=own_tasks)
    if task is None:
        return False
    try:
        parameters = begin_lookup(task, community_id)
        if parameters is not None and not complete_local_lookup(task):
            peer_id, random_id, text, started_at = parameters
            if read_history is None:
                complete_lookup(task, None, "history_unavailable")
                return True
            try:
                items = guarded_history_read(community_id, read_history, peer_id)
            except HistoryQuotaExceeded as error:
                defer_for_quota(task, error.retry_at)
            except (DeliveryUnknown, OSError):
                complete_lookup(task, None, "history_unavailable")
            else:
                evidence = matching_message(items, community_id, peer_id, random_id, text, started_at)
                complete_lookup(task, evidence, "" if evidence else "no_unique_match")
    except LeaseLost:
        pass
    return True
