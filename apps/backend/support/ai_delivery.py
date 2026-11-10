from uuid import NAMESPACE_URL, uuid4, uuid5

from django.conf import settings
from django.db import connection, transaction

from accounts.policies import quarantine_incident
from jobs.models import BackgroundTask as Task
from jobs.services import CHANNEL, ClockTimestamp, LeaseLost, admit_external_call, claim, defer, enqueue, finish
from knowledge.search import SearchHit, sources_are_current
from storage.services import database_time
from vk_channel.client import DeliveryRejected, DeliveryUnknown
from vk_channel.models import LongPollCursor

from .models import AIAnswer, AIAnswerInput, AIPartAttempt, AIPartDelivery, AIPartReconciliation, BotDeliveryRecoveryRun, Dialog, Message
from .vk_keys import reserve_random_id
from .attachment_responses import PROFILE as ATTACHMENT_PROMPT_PROFILE, valid_prompt
from .context_responses import PROFILE as CONTEXT_PROMPT_PROFILE, valid_prompt as valid_context_prompt

TASK_TYPE = "send_vk_bot_part"


def answer_hits(answer):
    return [
        SearchHit(item["fragment_id"], item["material_id"], item["publication_version"],
                  item["prepared_snapshot_id"], "", "", 0)
        for sources in answer.inputs.values_list("sources_snapshot", flat=True) for item in sources
    ]


def lock_chain(answer_id, *, verify_sources=False):
    head = AIAnswer.objects.values("dialog_id", "dialog__cursor_id").get(pk=answer_id)
    cursor = LongPollCursor.objects.select_for_update().get(pk=head["dialog__cursor_id"])
    sources_ok = True
    if verify_sources:
        snapshot = AIAnswer.objects.get(pk=answer_id)
        if snapshot.retrieval_snapshot.get("profile") == ATTACHMENT_PROMPT_PROFILE:
            sources_ok = valid_prompt(snapshot)
        elif snapshot.retrieval_snapshot.get("profile") == CONTEXT_PROMPT_PROFILE:
            sources_ok = valid_context_prompt(snapshot)
        else:
            hits = answer_hits(snapshot)
            sources_ok = bool(hits) and sources_are_current(hits)
    dialog = Dialog.objects.select_for_update().get(pk=head["dialog_id"])
    answer = AIAnswer.objects.select_for_update().get(pk=answer_id)
    deliveries = list(AIPartDelivery.objects.filter(part__answer=answer).select_related("part")
                      .select_for_update(of=("self",)).order_by("pk"))
    return cursor, dialog, answer, deliveries, sources_ok


def block_reason(cursor, dialog, answer, sources_ok, community_id, peer_id):
    from .handoff_plans import answer_dispatch_blocked

    run = answer.aiprocessingrun
    if quarantine_incident() or cursor.community_id != community_id or dialog.vk_peer_id != peer_id:
        return "channel_unavailable"
    if cursor.automation_paused or cursor.automation_epoch != run.automation_epoch:
        return "channel_paused"
    if dialog.mode != "ai_auto" or dialog.mode_version != answer.mode_version_snapshot:
        return "dialog_changed"
    if answer_dispatch_blocked(answer.pk):
        return "handoff_deadline"
    if any(item.request.request_version != item.request_version_snapshot
           for item in answer.inputs.select_related("request")):
        return "question_changed"
    if answer.inputs.filter(request__resolved_at__isnull=False).exists():
        return "question_resolved"
    if not sources_ok:
        return "sources_changed"
    from .ai_context import dispatch_waits_for_context

    if dispatch_waits_for_context(run):
        return "context_pending" if database_time() < run.processing_deadline_at else "context_budget_exhausted"
    return ""


def cancel_waiting(answer, reason, now):
    AIPartDelivery.objects.filter(part__answer=answer, status="waiting").update(
        status="cancelled", reason_code=reason, updated_at=now,
    )


def reserved_elsewhere(answer):
    for request_id, version in answer.inputs.values_list("request_id", "request_version_snapshot"):
        if AIAnswerInput.objects.filter(
            request_id=request_id, request_version_snapshot=version,
            answer__parts__aipartdelivery__aipartattempt__isnull=False,
        ).exclude(answer=answer).exists():
            return True
    return False


@transaction.atomic
def schedule_part(answer_id, community_id, peer_id):
    cursor, dialog, answer, deliveries, sources_ok = lock_chain(answer_id, verify_sources=True)
    next_part = next((item for item in sorted(deliveries, key=lambda item: item.part.position) if item.status != "sent"), None)
    if next_part is None or next_part.status != "waiting":
        return False
    reason = block_reason(cursor, dialog, answer, sources_ok, community_id, peer_id)
    if reason == "context_pending":
        return False
    if not reason and reserved_elsewhere(answer):
        reason = "request_version_already_reserved"
    now = database_time()
    if reason:
        cancel_waiting(answer, reason, now)
        from .handoff_plans import answer_outcome_changed

        answer_outcome_changed(answer.pk)
        return True
    # Allocate the row ID before building the task that references it.
    with connection.cursor() as db:
        db.execute("SELECT nextval(pg_get_serial_sequence('support_ai_part_attempt', 'id'))")
        attempt_id = db.fetchone()[0]
    operation_id = uuid4()
    next_part.vk_random_id = reserve_random_id()
    next_part.status, next_part.updated_at = "pending", now
    next_part.save(update_fields=["vk_random_id", "status", "updated_at"])
    attempt = AIPartAttempt.objects.create(
        pk=attempt_id, delivery=next_part, operation_id=operation_id, created_at=now,
        task=enqueue(TASK_TYPE, operation_id, {"delivery_id": next_part.pk, "attempt_id": attempt_id}),
    )
    return bool(attempt.pk)


def lock_attempt(task, *, verify_sources=False):
    reference = AIPartAttempt.objects.filter(task_id=task.pk).values_list("delivery__part__answer_id", flat=True).first()
    if reference is None:
        raise LeaseLost(task.pk)
    cursor, dialog, answer, deliveries, sources_ok = lock_chain(reference, verify_sources=verify_sources)
    attempt = AIPartAttempt.objects.select_for_update().get(task_id=task.pk)
    delivery = next(item for item in deliveries if item.pk == attempt.delivery_id)
    if (task.payload != {"delivery_id": delivery.pk, "attempt_id": attempt.pk}
            or task.idempotency_key != attempt.operation_id):
        raise LeaseLost(task.pk)
    return cursor, dialog, answer, delivery, attempt, sources_ok


def mark_uncertainty(delivery, now, recovery_run=None):
    if (delivery.status != "unknown_outcome" or delivery.uncertainty_revision is None
            or recovery_run is not None and delivery.recovery_run_id != recovery_run.pk):
        delivery.uncertainty_revision = uuid4()
        delivery.uncertainty_since = now
        delivery.recovery_run = recovery_run
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, '')", [CHANNEL])


def set_outcome(answer, delivery, attempt, status, now, *, reason="", provider_id=None, error_code=None):
    if status == "unknown_outcome":
        mark_uncertainty(delivery, now)
    attempt.status = {"sent": "succeeded", "unknown_outcome": "unknown_outcome"}.get(status, "failed")
    attempt.finished_at = None if status == "unknown_outcome" else now
    attempt.reason_code, attempt.vk_error_code = reason, error_code
    attempt.save(update_fields=["status", "finished_at", "reason_code", "vk_error_code"])
    if status == "sent":
        part = delivery.part
        part.message = Message.objects.create(
            dialog_id=answer.dialog_id, ticket_id=delivery.handoff_ticket_id or answer.aiprocessingrun.handoff_ticket_id,
            direction="outbound_bot", text=part.text_snapshot, created_at=now,
        )
        part.save(update_fields=["message"])
        delivery.provider_message_id = provider_id
    else:
        cancel_waiting(answer, "previous_part_unconfirmed", now)
    delivery.status, delivery.reason_code, delivery.updated_at = status, reason, now
    delivery.save(update_fields=["status", "reason_code", "updated_at", "provider_message_id",
                                 "uncertainty_revision", "uncertainty_since", "recovery_run"])
    from .handoff_plans import answer_outcome_changed

    answer_outcome_changed(answer.pk)


@transaction.atomic
def admit(task, community_id, peer_id):
    cursor, dialog, answer, delivery, attempt, sources_ok = lock_attempt(task, verify_sources=True)
    if delivery.status != "pending" or attempt.status != "in_flight" or attempt.started_at is not None:
        raise LeaseLost(task.pk)
    reason = block_reason(cursor, dialog, answer, sources_ok, community_id, peer_id)
    if reason == "context_pending":
        defer(task)
        return None
    if reason:
        finish(task, {"reason": reason}, status=Task.Status.FAILED)
        set_outcome(answer, delivery, attempt, "cancelled", database_time(), reason=reason)
        return None
    admit_external_call(task)
    attempt.started_at = database_time()
    attempt.save(update_fields=["started_at"])
    if settings.VK_CLICK_ANSWERS_VERIFIED and delivery.vk_keyboard is None:
        from vk_channel.student_actions import bot_keyboard

        delivery.vk_keyboard = bot_keyboard(answer, delivery, attempt.started_at)
        delivery.save(update_fields=["vk_keyboard"])
    parameters = (dialog.vk_peer_id, delivery.vk_random_id, delivery.part.text_snapshot)
    return (*parameters, delivery.vk_keyboard) if delivery.vk_keyboard else parameters


@transaction.atomic
def complete(task, status, **outcome):
    _, _, answer, delivery, attempt, _ = lock_attempt(task)
    if delivery.status != "pending" or attempt.status != "in_flight" or attempt.started_at is None:
        raise LeaseLost(task.pk)
    task_status = {"sent": Task.Status.DONE, "unknown_outcome": Task.Status.UNKNOWN}.get(status, Task.Status.FAILED)
    finish(task, {"attempt_id": str(attempt.operation_id)}, status=task_status)
    set_outcome(answer, delivery, attempt, status, database_time(), **outcome)


@transaction.atomic
def recover_expired():
    candidate = Task.objects.filter(task_type=TASK_TYPE, status=Task.Status.PROCESSING,
                                    locked_until__lte=ClockTimestamp()).order_by("locked_until", "pk").first()
    if candidate is None:
        return False
    _, _, answer, delivery, attempt, _ = lock_attempt(candidate)
    task = Task.objects.select_for_update().get(pk=candidate.pk)
    now = database_time()
    if task.status != Task.Status.PROCESSING or task.locked_until > now:
        return False
    if attempt.started_at is not None or task.admitted_at is not None:
        reason = "worker_lease_expired"
        set_outcome(answer, delivery, attempt, "unknown_outcome", now, reason=reason)
        status = Task.Status.UNKNOWN
    elif task.attempt_count >= task.max_attempts:
        reason = "queue_exhausted_before_dispatch"
        set_outcome(answer, delivery, attempt, "failed", now, reason=reason)
        status = Task.Status.DEAD
    else:
        reason, status = "", Task.Status.RETRY
    Task.objects.filter(pk=task.pk).update(
        status=status, result={"reason": reason} if reason else None, scheduled_at=now,
        lease_token=None, locked_until=None, updated_at=now,
    )
    return True


def run_bot_sender_once(community_id, peer_id, send):
    while recover_expired():
        pass
    candidate = AIPartDelivery.objects.filter(
        status="waiting", part__answer__dialog__cursor__community_id=community_id,
        part__answer__dialog__vk_peer_id=peer_id,
    ).exclude(part__answer__parts__aipartdelivery__status="pending").order_by("pk").first()
    scheduled = candidate is not None and schedule_part(candidate.part.answer_id, community_id, peer_id)
    task = claim([TASK_TYPE])
    if task is None:
        return scheduled
    try:
        parameters = admit(task, community_id, peer_id)
        if parameters is None:
            return True
        try:
            provider_id = send(*parameters)
        except DeliveryRejected as exc:
            complete(task, "failed", reason="vk_rejected", error_code=exc.code)
        except (DeliveryUnknown, OSError):
            complete(task, "unknown_outcome", reason="vk_confirmation_lost")
        else:
            complete(task, "sent", provider_id=provider_id)
    except LeaseLost:
        pass
    return True


def cancel_for_handoff(answer_id, now):
    """The caller holds the dialog lock and has already linked the handoff card."""
    cancel_unstarted_parts(answer_id, now, "dialog_changed")


def cancel_unstarted_parts(answer_id, now, reason):
    """The caller holds the cursor and dialog locks; admitted calls retain their outcomes."""
    answer = AIAnswer.objects.select_for_update().get(pk=answer_id)
    deliveries = AIPartDelivery.objects.filter(part__answer=answer, status="pending").select_for_update().order_by("pk")
    cancel_waiting(answer, reason, now)
    for delivery in deliveries:
        attempt = AIPartAttempt.objects.select_for_update().get(delivery=delivery)
        if attempt.started_at is None and attempt.status == "in_flight":
            Task.objects.filter(pk=attempt.task_id, admitted_at__isnull=True).update(
                status=Task.Status.FAILED, result={"reason": reason},
                lease_token=None, locked_until=None, updated_at=now,
            )
            set_outcome(answer, delivery, attempt, "cancelled", now, reason=reason)


def isolate_restored_bot_deliveries(recovery_run_id=None):
    incident = quarantine_incident()
    if not incident:
        raise ValueError("Запустите команду в карантине восстановления.")
    # Legacy invocation remains repeatable within its quarantine incident.
    run_id = recovery_run_id or uuid5(NAMESPACE_URL, f"alliance:bot-recovery:{incident}")
    with transaction.atomic():
        run, _ = BotDeliveryRecoveryRun.objects.get_or_create(pk=run_id, defaults={"incident": incident})
        if run.incident != incident:
            raise ValueError("UUID восстановления принадлежит другому инциденту.")
    return isolate_recovery_run(run.pk)


@transaction.atomic
def isolate_recovery_run(run_id):
    run = BotDeliveryRecoveryRun.objects.select_for_update().get(pk=run_id)
    if not quarantine_incident() or quarantine_incident() != run.incident:
        raise ValueError("Сохраняйте карантин исходного инцидента до окончания обработки.")
    if run.completed_at is not None:
        return False
    answer_ids = list(AIPartDelivery.objects.filter(
        status__in=("waiting", "pending", "unknown_outcome"),
    ).order_by("part__answer_id").values_list("part__answer_id", flat=True).distinct())
    for answer_id in answer_ids:
        _, _, _, deliveries, _ = lock_chain(answer_id)
        now = database_time()
        for delivery in deliveries:
            if delivery.status not in {"waiting", "pending", "unknown_outcome"}:
                continue
            if delivery.provider_message_id is not None or delivery.part.message_id is not None:
                raise ValueError("Незавершённая доставка содержит подтверждённое сообщение.")
            attempt = AIPartAttempt.objects.select_for_update().filter(delivery=delivery).first()
            checks = list(AIPartReconciliation.objects.select_for_update().filter(
                delivery=delivery, status__in=["queued", "running"],
            ).order_by("pk"))
            task_ids = [check.task_id for check in checks]
            if attempt:
                task_ids.append(attempt.task_id)
            list(Task.objects.select_for_update().filter(pk__in=task_ids).order_by("pk"))
            if attempt is None:
                if delivery.status == "pending" or delivery.vk_random_id is not None:
                    raise ValueError("Нарушена связь восстановленной части и попытки.")
            else:
                task = Task.objects.select_for_update().get(pk=attempt.task_id)
                if (delivery.status == "waiting" or delivery.vk_random_id is None or task.task_type != TASK_TYPE
                        or task.idempotency_key != attempt.operation_id
                        or attempt.status not in {"in_flight", "unknown_outcome"}
                        or task.status not in {Task.Status.PENDING, Task.Status.PROCESSING, Task.Status.RETRY, Task.Status.UNKNOWN}
                        or task.payload != {"delivery_id": delivery.pk, "attempt_id": attempt.pk}):
                    raise ValueError("Нарушена связь восстановленной попытки и задания.")
                attempt.status, attempt.reason_code, attempt.finished_at = "unknown_outcome", "recovered_from_backup", None
                attempt.save(update_fields=["status", "reason_code", "finished_at"])
                Task.objects.filter(pk=task.pk).update(
                    status=Task.Status.UNKNOWN, result={"reason": "recovered_from_backup"},
                    lease_token=None, locked_until=None, updated_at=now,
                )
            for check in checks:
                reading = Task.objects.get(pk=check.task_id)
                if (reading.task_type != "reconcile_vk_bot_part"
                        or reading.payload != {"reconciliation_id": check.pk, "delivery_id": delivery.pk}
                        or reading.idempotency_key != check.task_key):
                    raise ValueError("Нарушена связь восстановленной сверки и задания.")
                check.status, check.outcome, check.reason = "finished", "unresolved", "recovered_from_backup"
                check.finished_at = now
                check.save(update_fields=["status", "outcome", "reason", "finished_at"])
                Task.objects.filter(pk=check.task_id).update(status=Task.Status.FAILED,
                    result={"reason": "recovered_from_backup"}, lease_token=None, locked_until=None, updated_at=now)
            mark_uncertainty(delivery, now, run)
            delivery.status, delivery.reason_code, delivery.updated_at = "unknown_outcome", "recovered_from_backup", now
            delivery.save(update_fields=["status", "reason_code", "updated_at", "uncertainty_revision",
                                         "uncertainty_since", "recovery_run"])
    run.completed_at = database_time()
    run.save(update_fields=["completed_at"])
    return True
