from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q

from accounts.policies import quarantine_incident
from accounts.services import AccessError
from jobs.models import BackgroundTask as Task
from jobs.services import ClockTimestamp, LeaseLost, claim, enqueue, finish, renew
from storage.services import database_time
from vk_channel.history import HistoryQuotaExceeded, read_history as guarded_history_read
from vk_channel.client import DeliveryUnknown
from vk_channel.models import LongPollCursor

from .ai_delivery import TASK_TYPE as SEND_TASK, set_outcome
from .models import (
    AIAnswer, AIPartAttempt, AIPartDelivery, AIPartReconciliation as Check,
    AIPartReconciliationEvidence as Evidence, AIProcessingRun, Dialog, OutgoingResponse, Ticket,
)
from .queries import allowed_communities
from .reconciliation import defer_read_task, matching_message, may_reconcile
from .services import reserve_command, support_actor

TASK_TYPE = "reconcile_vk_bot_part"
LOOKUP_WINDOW = timedelta(minutes=2)
SCAN_BATCH_SIZE = 25


def visible_delivery(actor, part_id):
    delivery = AIPartDelivery.objects.filter(
        part_id=part_id, part__answer__dialog__cursor_id__in=allowed_communities(actor),
        part__answer__aiprocessingrun__status="answer_saved",
    ).first()
    if delivery is None:
        raise AccessError("not_found", 404)
    return delivery


def lock_delivery(delivery_id):
    dialog_id, answer_id = AIPartDelivery.objects.filter(pk=delivery_id).values_list(
        "part__answer__dialog_id", "part__answer_id",
    ).get()
    cursor_id = Dialog.objects.values_list("cursor_id", flat=True).get(pk=dialog_id)
    LongPollCursor.objects.select_for_update().get(pk=cursor_id)
    dialog = Dialog.objects.select_for_update().get(pk=dialog_id)
    run = AIProcessingRun.objects.get(answer_id=answer_id)
    ticket_id = (AIPartDelivery.objects.values_list("handoff_ticket_id", flat=True).get(pk=delivery_id)
                 or run.handoff_ticket_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id) if ticket_id else None
    if run.dialog_id != dialog.pk or ticket is not None and ticket.dialog_id != dialog.pk:
        raise AccessError("reconciliation_state_inconsistent", 409)
    answer = AIAnswer.objects.select_for_update().get(pk=answer_id)
    delivery = AIPartDelivery.objects.select_related("part").select_for_update(of=("self",)).get(pk=delivery_id)
    attempt = AIPartAttempt.objects.select_for_update().filter(delivery=delivery).first()
    return dialog, ticket, answer, delivery, attempt


def can_reconcile(actor, ticket):
    return ticket is None or may_reconcile(actor, ticket)


@transaction.atomic
def request_reconciliation(session, part_id, operation_id):
    actor = support_actor(session, lock=True)
    original = visible_delivery(actor, part_id)
    _, ticket, _, delivery, attempt = lock_delivery(original.pk)
    if not can_reconcile(actor, ticket):
        raise AccessError("permission_denied", 403)
    command, created = reserve_command(actor, operation_id, "reconcile_ai_part", "ai_part", part_id, {})
    if not created:
        return Check.objects.get(command=command), True
    if delivery.status != "unknown_outcome" or delivery.uncertainty_revision is None:
        raise AccessError("reconciliation_not_required", 409)
    if Check.objects.filter(delivery=delivery, status__in=["queued", "running"]).exists():
        raise AccessError("reconciliation_in_progress", 409)
    return enqueue_check(delivery, attempt, actor=actor, command=command), False


def enqueue_check(delivery, attempt, *, actor=None, command=None):
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(pg_get_serial_sequence('support_ai_part_reconciliation', 'id'))")
        check_id = cursor.fetchone()[0]
    check = Check(
        pk=check_id, delivery=delivery, target_revision=delivery.uncertainty_revision,
        target_attempt=attempt, trigger="manual" if command else "automatic", actor=actor,
        client_operation_id=command.client_operation_id if command else None,
        command=command, lookup_deadline_at=database_time() + LOOKUP_WINDOW,
    )
    check.task = enqueue(TASK_TYPE, check.task_key, {"reconciliation_id": check_id, "delivery_id": delivery.pk},
                         priority=0 if command else -10)
    check.save(force_insert=True)
    return check


@transaction.atomic
def schedule_automatic(community_id, peer_id, *, history_available=False):
    if not settings.VK_BOT_RECONCILIATION_AUTO_ENABLED or quarantine_incident():
        return 0
    cursor = LongPollCursor.objects.select_for_update().filter(community_id=community_id).first()
    if cursor is None:
        return 0
    candidates = AIPartDelivery.objects.filter(
        status="unknown_outcome", part__answer__dialog__cursor=cursor,
        part__answer__dialog__vk_peer_id=peer_id, part__answer__aiprocessingrun__status="answer_saved",
    )
    if cursor.reconciliation_scan_since is not None:
        candidates = candidates.filter(Q(uncertainty_since__gt=cursor.reconciliation_scan_since)
            | Q(uncertainty_since=cursor.reconciliation_scan_since, pk__gt=cursor.reconciliation_scan_id))
    rows = list(candidates.order_by("uncertainty_since", "pk").values_list("pk", "uncertainty_since")[:SCAN_BATCH_SIZE])
    scheduled = 0
    for delivery_id, _ in rows:
        _, _, _, delivery, attempt = lock_delivery(delivery_id)
        if delivery.status != "unknown_outcome" or delivery.uncertainty_revision is None:
            continue
        if (Check.objects.filter(delivery=delivery, target_revision=delivery.uncertainty_revision).exists()
                or Check.objects.filter(delivery=delivery, status__in=["queued", "running"]).exists()):
            continue
        outgoing = Task.objects.select_for_update().get(pk=attempt.task_id) if attempt else None
        if attempt is None or not outgoing_matches(outgoing, delivery, attempt):
            continue
        local = non_admission(delivery, attempt, outgoing)
        if local or history_available and delivery.vk_random_id is not None:
            enqueue_check(delivery, attempt)
            scheduled += 1
    if len(rows) == SCAN_BATCH_SIZE:
        cursor.reconciliation_scan_id, cursor.reconciliation_scan_since = rows[-1]
    else:
        cursor.reconciliation_scan_since = cursor.reconciliation_scan_id = None
    cursor.save(update_fields=["reconciliation_scan_since", "reconciliation_scan_id"])
    return scheduled


def check_data(check):
    return {"id": str(check.pk), "part_id": str(check.delivery.part_id), "status": check.status,
            "trigger": check.trigger,
            "outcome": check.outcome or None, "reason": check.reason or None,
            "target_revision": str(check.target_revision)}


def delivery_state(actor, delivery):
    run = AIProcessingRun.objects.get(answer_id=delivery.part.answer_id)
    ticket_id = delivery.handoff_ticket_id or run.handoff_ticket_id
    ticket = Ticket.objects.filter(pk=ticket_id).first() if ticket_id else None
    check = delivery.reconciliations.filter(target_revision=delivery.uncertainty_revision).order_by("-pk").first()
    return {"part_id": str(delivery.part_id), "status": delivery.status,
            "uncertainty_revision": str(delivery.uncertainty_revision) if delivery.uncertainty_revision else None,
            "last_reconciliation": check_data(check) if check else None,
            "can_reconcile": delivery.status == "unknown_outcome" and can_reconcile(actor, ticket),
            "checking": bool(check and check.status != "finished")}


def lock_check(task):
    check_id, delivery_id = Check.objects.filter(task_id=task.pk).values_list("pk", "delivery_id").get()
    if task.task_type != TASK_TYPE or task.payload != {"reconciliation_id": check_id, "delivery_id": delivery_id}:
        raise LeaseLost(task.pk)
    context = lock_delivery(delivery_id)
    check = Check.objects.select_for_update().get(pk=check_id)
    if task.idempotency_key != check.task_key:
        raise LeaseLost(task.pk)
    return (*context, check)


def target_is_current(delivery, attempt, check):
    return (delivery.status == "unknown_outcome" and delivery.uncertainty_revision == check.target_revision
            and delivery.part.message_id is None
            and check.target_attempt_id == (attempt.pk if attempt else None)
            and (attempt is None or attempt.status == "unknown_outcome"))


def finish_check(check, outcome, reason):
    check.status, check.outcome, check.reason = "finished", outcome, reason
    check.finished_at = database_time()
    check.save(update_fields=["status", "outcome", "reason", "finished_at", "evidence"])


@transaction.atomic
def begin_lookup(task, community_id, peer_id):
    dialog, _, _, delivery, attempt, check = lock_check(task)
    if check.status != "queued":
        raise LeaseLost(task.pk)
    reason = ""
    if not target_is_current(delivery, attempt, check):
        reason = "target_changed"
    elif check.trigger == "automatic" and not settings.VK_BOT_RECONCILIATION_AUTO_ENABLED:
        reason = "automation_disabled"
    elif database_time() >= check.lookup_deadline_at:
        reason = "lookup_budget_exhausted"
    elif quarantine_incident() or dialog.cursor.community_id != community_id or dialog.vk_peer_id != peer_id:
        reason = "history_unavailable"
    if reason:
        finish_check(check, "unresolved", reason)
        finish(task, {"reason": reason})
        return False
    renew(task)
    check.status = "running"
    check.save(update_fields=["status"])
    return True


def outgoing_matches(task, delivery, attempt):
    return (task.status == Task.Status.UNKNOWN and task.task_type == SEND_TASK
            and task.idempotency_key == attempt.operation_id
            and task.payload == {"delivery_id": delivery.pk, "attempt_id": attempt.pk})


def non_admission(delivery, attempt, outgoing):
    if (attempt is None or delivery.recovery_run_id or delivery.reason_code == "recovered_from_backup"
            or attempt.reason_code == "recovered_from_backup" or attempt.started_at is not None
            or outgoing.admitted_at is not None):
        return None
    return {"delivery_id": delivery.pk, "attempt_id": attempt.pk, "task_id": outgoing.pk,
            "no_external_admission": True}


@transaction.atomic
def complete_lookup(task, community_id, items=None, reason=""):
    dialog, _, answer, delivery, attempt, check = lock_check(task)
    if check.status != "running":
        raise LeaseLost(task.pk)
    task_ids = [task.pk, attempt.task_id] if attempt else [task.pk]
    jobs = {job.pk: job for job in Task.objects.select_for_update().filter(pk__in=task_ids).order_by("pk")}
    outgoing = jobs[attempt.task_id] if attempt else None
    proof = None
    outcome = "unresolved"
    if not target_is_current(delivery, attempt, check) or (attempt and not outgoing_matches(outgoing, delivery, attempt)):
        reason = "target_changed"
    elif quarantine_incident() or dialog.cursor.community_id != community_id:
        reason = "history_unavailable"
    elif database_time() >= check.lookup_deadline_at:
        reason = "lookup_budget_exhausted"
    else:
        proof = non_admission(delivery, attempt, outgoing)
        if proof:
            outcome = "not_sent_verified"
        elif items is not None and delivery.vk_random_id is not None and attempt is not None:
            proof = matching_message(items, community_id, dialog.vk_peer_id, delivery.vk_random_id,
                                     delivery.part.text_snapshot, answer.created_at)
            if proof:
                duplicate = AIPartDelivery.objects.filter(part__answer__dialog=dialog, provider_message_id=proof["message_id"]).exclude(pk=delivery.pk).exists()
                duplicate |= OutgoingResponse.objects.filter(ticket__dialog=dialog, provider_message_id=proof["message_id"]).exists()
                if duplicate:
                    proof = None
            if proof:
                outcome = "accepted_by_vk"
            else:
                reason = "no_unique_match"
        else:
            reason = reason or "history_unavailable"
    if proof:
        reason = ""
        proof = {**proof, "uncertainty_revision": str(check.target_revision),
                 "recovery_run_id": str(delivery.recovery_run_id) if delivery.recovery_run_id else None}
        check.evidence = Evidence.objects.create(reconciliation=check, observed_at=database_time(), facts=proof,
            source_kind="local_non_admission" if outcome == "not_sent_verified" else "vk_history")
        if attempt:
            status = "sent" if outcome == "accepted_by_vk" else "failed"
            set_outcome(answer, delivery, attempt, status, database_time(), provider_id=proof.get("message_id"),
                        reason="not_sent_verified" if outcome == "not_sent_verified" else "")
            outgoing.status = Task.Status.DONE if outcome == "accepted_by_vk" else Task.Status.FAILED
            outgoing.result = {"reconciliation_id": check.pk}
            outgoing.updated_at = database_time()
            outgoing.save(update_fields=["status", "result", "updated_at"])
        else:
            # A key without its attempt is not a supported correlation source.
            raise ValueError("Missing attempt for correlated delivery")
    finish_check(check, outcome, reason)
    finish(task, {"reconciliation_id": check.pk, "outcome": outcome})


@transaction.atomic
def local_parameters(task):
    dialog, _, _, delivery, attempt, check = lock_check(task)
    outgoing = Task.objects.get(pk=attempt.task_id) if attempt else None
    local = non_admission(delivery, attempt, outgoing)
    return local is not None, dialog.vk_peer_id, delivery.vk_random_id is not None and attempt is not None, check.trigger == "automatic"


@transaction.atomic
def recover_expired():
    task = Task.objects.filter(task_type=TASK_TYPE, status=Task.Status.PROCESSING,
                               locked_until__lte=ClockTimestamp()).order_by("locked_until", "pk").first()
    if task is None:
        return False
    _, _, _, delivery, attempt, check = lock_check(task)
    job = Task.objects.select_for_update().get(pk=task.pk)
    now = database_time()
    if job.status != Task.Status.PROCESSING or job.locked_until > now:
        return True
    if not target_is_current(delivery, attempt, check):
        job.status = Task.Status.DONE
        finish_check(check, "unresolved", "target_changed")
    elif job.attempt_count >= job.max_attempts or now >= check.lookup_deadline_at:
        job.status = Task.Status.DEAD if job.attempt_count >= job.max_attempts else Task.Status.DONE
        finish_check(check, "unresolved", "lookup_budget_exhausted")
    else:
        job.status, job.scheduled_at = Task.Status.RETRY, now
        check.status = "queued"
        check.save(update_fields=["status"])
    job.lease_token = job.locked_until = None
    job.updated_at = now
    job.save(update_fields=["status", "scheduled_at", "lease_token", "locked_until", "updated_at"])
    return True


@transaction.atomic
def defer_for_quota(task, retry_at):
    _, _, _, delivery, attempt, check = lock_check(task)
    if not target_is_current(delivery, attempt, check):
        finish_check(check, "unresolved", "target_changed")
        finish(task, {"reason": "target_changed"})
    else:
        defer_read_task(task, check, retry_at or database_time() + timedelta(seconds=1))


def run_reconciliation_once(community_id, peer_id, read_history=None):
    while recover_expired():
        pass
    scheduled = schedule_automatic(community_id, peer_id, history_available=read_history is not None)
    own_tasks = Check.objects.filter(delivery__part__answer__dialog__cursor__community_id=community_id,
                                    delivery__part__answer__dialog__vk_peer_id=peer_id).values_list("task_id", flat=True)
    task = claim([TASK_TYPE], task_ids=own_tasks)
    if task is None:
        return bool(scheduled)
    try:
        if not begin_lookup(task, community_id, peer_id):
            return True
        local, target_peer, correlated, automatic = local_parameters(task)
        if local or read_history is None or not correlated:
            complete_lookup(task, community_id, reason="history_unavailable")
            return True
        try:
            items = guarded_history_read(community_id, read_history, target_peer, automatic=automatic)
        except HistoryQuotaExceeded as error:
            defer_for_quota(task, error.retry_at)
        except (DeliveryUnknown, OSError):
            complete_lookup(task, community_id, reason="history_unavailable")
        else:
            complete_lookup(task, community_id, items=items)
    except LeaseLost:
        pass
    return True
