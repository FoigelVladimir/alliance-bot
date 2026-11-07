"""Cold recovery only: old writers must be stopped and quarantine must remain active."""
from django.db import transaction

from accounts.policies import quarantine_incident
from jobs.models import BackgroundTask as Task
from vk_channel.services import database_now

from .delivery import TASK_TYPE as SEND_TASK
from .models import (
    DeliveryRecoveryRun, Dialog, OutgoingAttempt as Attempt, OutgoingReconciliation as Check,
    OutgoingResponse as Response, ResponseRecoveryGuard as Guard, Ticket,
)

UNFINISHED = (Task.Status.PENDING, Task.Status.PROCESSING, Task.Status.RETRY, Task.Status.UNKNOWN)


class RecoveryConflict(Exception):
    pass


@transaction.atomic
def register_recovery(run_id):
    incident = quarantine_incident()
    if not incident:
        raise RecoveryConflict("Recovery requires an active staff quarantine")
    run, _ = DeliveryRecoveryRun.objects.get_or_create(pk=run_id, defaults={"incident": incident})
    if run.incident != incident:
        raise RecoveryConflict("Recovery UUID belongs to another quarantine incident")
    return run


@transaction.atomic
def recover_support_delivery(run_id):
    run = DeliveryRecoveryRun.objects.select_for_update().get(pk=run_id)
    if not quarantine_incident() or run.incident != quarantine_incident():
        raise RecoveryConflict("Keep the matching quarantine active during recovery")
    if run.completed_at:
        return False
    now = database_now()
    # Whole-pass rollback preserves quarantine on malformed or contradictory groups.
    for response_id in Response.objects.order_by("pk").values_list("pk", flat=True):
        original = Response.objects.get(pk=response_id)
        Dialog.objects.select_for_update().get(pk=original.ticket.dialog_id)
        Ticket.objects.select_for_update().get(pk=original.ticket_id)
        response = Response.objects.select_for_update().get(pk=response_id)
        attempts = list(Attempt.objects.select_for_update().filter(response=response).order_by("pk"))
        guards = list(Guard.objects.select_for_update().filter(response=response, resolved_at__isnull=True))
        checks = list(Check.objects.select_for_update().filter(response=response).exclude(status=Check.Status.FINISHED))
        task_ids = [attempt.task_id for attempt in attempts] + [check.task_id for check in checks]
        tasks = {task.pk: task for task in Task.objects.select_for_update().filter(pk__in=task_ids).order_by("pk")}
        if (response.status != Response.Status.SENT and (response.message_id or response.provider_message_id)
                or response.status == Response.Status.SENT and not (response.message_id and response.provider_message_id)):
            raise RecoveryConflict(f"Inconsistent confirmed response: {response.pk}")
        unfinished = []
        for attempt in attempts:
            task = tasks[attempt.task_id]
            # Old versions stored only response_id. The FK and UUID establish the missing ID.
            valid_payload = task.payload in (
                {"response_id": response.pk}, {"response_id": response.pk, "attempt_id": attempt.pk},
            )
            if (task.task_type != SEND_TASK or not valid_payload
                    or task.idempotency_key != attempt.command.client_operation_id):
                raise RecoveryConflict(f"Invalid delivery binding: {attempt.pk}")
            if attempt.status == Attempt.Status.SUCCEEDED and (
                response.status != Response.Status.SENT or task.status != Task.Status.DONE
            ):
                raise RecoveryConflict(f"Inconsistent confirmed attempt: {attempt.pk}")
            active = task.status in UNFINISHED or attempt.status in (Attempt.Status.IN_FLIGHT, Attempt.Status.UNKNOWN)
            if active:
                if response.status == Response.Status.SENT or response.message_id or response.provider_message_id:
                    raise RecoveryConflict(f"Confirmed response has unfinished delivery: {response.pk}")
                if attempt.status not in (Attempt.Status.IN_FLIGHT, Attempt.Status.UNKNOWN) or task.status not in UNFINISHED:
                    raise RecoveryConflict(f"Inconsistent delivery outcome: {attempt.pk}")
                unfinished.append(attempt)
        if len(unfinished) > 1 or (unfinished and guards):
            raise RecoveryConflict(f"Inconsistent active targets: {response.pk}")
        if response.status in (Response.Status.PENDING, Response.Status.UNKNOWN) and not unfinished:
            raise RecoveryConflict(f"Missing unfinished attempt: {response.pk}")
        for attempt in unfinished:
            task = tasks[attempt.task_id]
            attempt.status = Attempt.Status.UNKNOWN
            attempt.finished_at = None
            attempt.failure_reason = "recovered_from_backup"
            attempt.save(update_fields=["status", "finished_at", "failure_reason"])
            response.status = Response.Status.UNKNOWN
            response.failure_reason = "recovered_from_backup"
            task.status = Task.Status.UNKNOWN
            task.payload = {"response_id": response.pk, "attempt_id": attempt.pk}
            task.save(update_fields=["payload"])
            task.result = {"reason": "recovered_from_backup", "recovery_run_id": str(run.pk)}
            revoke_task(task, now)
        for check in checks:
            task = tasks[check.task_id]
            payload = ({"attempt_id": check.target_attempt_id} if check.target_attempt_id
                       else {"recovery_guard_id": check.target_recovery_guard_id})
            target = check.target_attempt if check.target_attempt_id else check.target_recovery_guard
            if (task.task_type != "support.reconcile_response" or task.payload != payload
                    or task.idempotency_key != check.command.client_operation_id or target.response_id != response.pk):
                raise RecoveryConflict(f"Invalid reconciliation binding: {check.pk}")
            check.status, check.outcome = Check.Status.FINISHED, Check.Outcome.UNRESOLVED
            check.reason, check.finished_at = "recovered_from_backup", now
            check.save(update_fields=["status", "outcome", "reason", "finished_at"])
            task.status, task.result = Task.Status.FAILED, {"reason": "recovered_from_backup"}
            revoke_task(task, now)
        if response.status == Response.Status.FAILED:
            existing = Guard.objects.filter(response=response, recovery_run=run).first()
            if existing is None:
                for guard in guards:
                    guard.resolved_at, guard.resolution = now, "superseded"
                    guard.save(update_fields=["resolved_at", "resolution"])
                Guard.objects.create(response=response, recovery_run=run, opened_at=now)
            response.recovery_verification_required = Guard.objects.filter(response=response, resolved_at__isnull=True).exists()
        response.save(update_fields=["status", "failure_reason", "recovery_verification_required"])
    # Also reject orphan jobs: there is no safe domain group to repair speculatively.
    if Task.objects.filter(task_type=SEND_TASK, status__in=UNFINISHED, outgoingattempt__isnull=True).exists():
        raise RecoveryConflict("Orphan outgoing task")
    if Response.objects.filter(status=Response.Status.FAILED, recovery_verification_required=False).exists():
        raise RecoveryConflict("Incomplete recovery guard coverage")
    run.completed_at = now
    run.save(update_fields=["completed_at"])
    return True


def revoke_task(task, now):
    # Terminal jobs cannot carry a lease; clearing it also fences the old worker.
    task.lease_token, task.locked_until, task.updated_at = None, None, now
    task.save(update_fields=["status", "result", "lease_token", "locked_until", "updated_at"])
