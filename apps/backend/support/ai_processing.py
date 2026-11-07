import logging
from dataclasses import asdict
from datetime import timedelta
from time import monotonic
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from accounts.policies import quarantine_incident
from jobs.models import BackgroundTask as Task
from jobs.services import ClockTimestamp, LeaseLost, claim, enqueue, finish, renew
from knowledge.search import hybrid_search, sources_are_current
from storage.services import database_time
from vk_channel.models import LongPollCursor

from .ai_delivery import cancel_for_handoff
from .ai_generation import ModelUnavailable, generate_quote, save_answer
from .models import (
    AIProcessingInput, AIProcessingInputMessage, AIProcessingRun, AIRequest, AIRequestMessage,
    Dialog, InboundRoute, Message,
)

logger = logging.getLogger(__name__)

TASK_TYPE = "support.ai_lookup"
LOOKUP_BUDGET = timedelta(seconds=30)
QUESTION_LIMIT = 800


def begin_lookup(cursor, dialog, route, message):
    """Called with the cursor, dialog and route locked."""
    if (not settings.SUPPORT_AI_LOOKUP_ENABLED or cursor.automation_paused
            or route.automation_epoch != cursor.automation_epoch or route.mode_version != dialog.mode_version
            or dialog.mode != "ai_auto" or message.sender_vk_id != dialog.vk_peer_id
            or message.direction != "inbound" or message.attachments.exists()):
        return False
    text = " ".join(message.text.split())
    if not text or len(text) > QUESTION_LIMIT:
        return False
    now = database_time()
    request = None
    if route.reply_to_cmid is not None:
        request = AIRequest.objects.filter(
            Q(status="pending") | Q(status="answered_full", aianswerinput__answer__parts__aipartdelivery__status__in=("waiting", "pending")),
            dialog=dialog, mode_version=dialog.mode_version, automation_epoch=cursor.automation_epoch, message_links__message__conversation_message_id=route.reply_to_cmid,
            message_links__message__sender_vk_id=message.sender_vk_id,
            message_links__message__inboundroute__sequence__lt=route.sequence,
        ).first()
        if request is None or len(request.normalized_query) + 1 + len(text) > QUESTION_LIMIT:
            return False
    if request is None:
        request = AIRequest.objects.create(
            dialog=dialog, mode_version=dialog.mode_version, automation_epoch=cursor.automation_epoch,
            request_version_started_at=now, created_at=now, normalized_query=text,
        )
    else:
        running = AIProcessingRun.objects.filter(inputs__request=request, status="running").first()
        if running is not None:
            Task.objects.filter(pk=running.task_id).update(
                status=Task.Status.FAILED, result={"reason": "superseded"},
                lease_token=None, locked_until=None, updated_at=now,
            )
            record_no_answer(running, "superseded", now)
        request.request_version += 1
        request.status, request.finished_at = "pending", None
        request.request_version_started_at = now
        request.normalized_query += "\n" + text
        request.save(update_fields=["request_version", "request_version_started_at", "normalized_query", "status", "finished_at"])
    AIRequestMessage.objects.create(request=request, message=message)

    operation = uuid4()
    task = enqueue(TASK_TYPE, operation, {"dialog_id": dialog.pk}, priority=10)
    run = AIProcessingRun.objects.create(
        operation_id=operation, task=task, dialog=dialog, mode_version=dialog.mode_version,
        automation_epoch=cursor.automation_epoch, created_at=now, processing_deadline_at=now + LOOKUP_BUDGET,
    )
    snapshot = AIProcessingInput.objects.create(
        run=run, request=request, request_version=request.request_version, question_text=request.normalized_query,
    )
    AIProcessingInputMessage.objects.bulk_create([
        AIProcessingInputMessage(processing_input=snapshot, message_id=message_id)
        for message_id in request.message_links.values_list("message_id", flat=True)
    ])
    route.status, route.reason = "ai_processing", ""
    return True


def record_no_answer(run, reason, now, hits=()):
    run.status, run.reason, run.finished_at = "no_answer", reason, now
    run.sources_used = [
        {key: value for key, value in asdict(hit).items() if key not in {"content", "heading"}} for hit in hits
    ]
    run.save(update_fields=["status", "reason", "finished_at", "sources_used"])


def lock_run(run_id, hits=()):
    candidate = AIProcessingRun.objects.values("dialog_id", "dialog__cursor_id").get(pk=run_id)
    cursor = LongPollCursor.objects.select_for_update().get(pk=candidate["dialog__cursor_id"])
    sources_current = not hits or sources_are_current(hits)
    dialog = Dialog.objects.select_for_update().get(pk=candidate["dialog_id"])
    run = AIProcessingRun.objects.select_for_update().get(pk=run_id)
    return cursor, dialog, run, sources_current


def return_for_review(run, status, now):
    snapshot = run.inputs.get()
    updated = AIRequest.objects.filter(pk=snapshot.request_id, request_version=snapshot.request_version).update(
        status=status, finished_at=now,
    )
    if updated:
        InboundRoute.objects.filter(
            message_id__in=snapshot.messages.values_list("pk", flat=True),
            status="ai_processing", message__ticket__isnull=True,
        ).update(status="needs_review", reason="manual_support")


def finish_lookup(run, reason, now, hits=()):
    record_no_answer(run, reason, now, hits)
    return_for_review(run, "failed" if reason != "superseded" else "stale_cancelled", now)


@transaction.atomic
def recover_expired_lookup():
    candidate = AIProcessingRun.objects.filter(
        status="running", task__status=Task.Status.PROCESSING, task__locked_until__lte=ClockTimestamp(),
    ).order_by("task__locked_until").first()
    if candidate is None:
        return False
    _, _, run, _ = lock_run(candidate.pk)
    task = Task.objects.select_for_update().get(pk=run.task_id)
    now = database_time()
    if run.status != "running" or task.status != Task.Status.PROCESSING or task.locked_until > now:
        return False
    exhausted = task.attempt_count >= task.max_attempts or now >= run.processing_deadline_at
    if exhausted:
        finish_lookup(run, "processing_budget_exhausted", now)
    Task.objects.filter(pk=task.pk).update(
        status=Task.Status.DEAD if exhausted else Task.Status.RETRY,
        result={"reason": "processing_budget_exhausted"} if exhausted else None,
        lease_token=None, locked_until=None, scheduled_at=now, updated_at=now,
    )
    return True


def run_lookup_once(search=hybrid_search, generate=generate_quote):
    while recover_expired_lookup():
        pass
    task = claim([TASK_TYPE])
    if task is None:
        return False
    run = AIProcessingRun.objects.get(task=task)
    question = run.inputs.get().question_text
    try:
        with transaction.atomic():
            cursor, dialog, locked, _ = lock_run(run.pk)
            renew(task)
            reason = lookup_block_reason(cursor, dialog, locked, database_time())
        hits, quote = [], None
        lookup_ms = None
        if not reason:
            started = monotonic()
            try:
                hits = search(question)
            except Exception:
                logger.warning("Knowledge lookup failed for AI processing run %s", run.pk)
                reason = "model_unavailable"
            lookup_ms = round((monotonic() - started) * 1000)
            if not reason:
                if not hits:
                    reason = "no_reliable_basis"
                elif not settings.SUPPORT_AI_GENERATION_ENABLED:
                    reason = "generation_not_enabled"
                else:
                    remaining = (run.processing_deadline_at - database_time()).total_seconds()
                    if remaining <= 0:
                        reason = "processing_budget_exhausted"
                    else:
                        try:
                            quote = generate(question, hits, remaining)
                        except ModelUnavailable:
                            reason = "model_unavailable"
                        else:
                            if quote is None:
                                reason = "no_reliable_basis"
        with transaction.atomic():
            cursor, dialog, locked, sources_current = lock_run(run.pk, hits)
            now = database_time()
            blocked = lookup_block_reason(cursor, dialog, locked, now)
            if blocked:
                reason, hits, quote = blocked, [], None
            elif not sources_current:
                reason, hits, quote = "sources_changed", [], None
            if quote is None:
                finish(task, {"reason": reason})
                finish_lookup(locked, reason, now, hits)
            else:
                answer = save_answer(locked, quote, hits, now, lookup_ms)
                finish(task, {"answer_id": answer.pk})
                # Preview remains available to staff while the delivery worker is separate.
                return_for_review(locked, "answered_full", now)
    except LeaseLost:
        pass
    return True


def lookup_block_reason(cursor, dialog, run, now):
    if run.status != "running":
        raise LeaseLost(run.pk)
    if quarantine_incident():
        return "recovered_from_backup"
    if (cursor.automation_paused or cursor.automation_epoch != run.automation_epoch
            or dialog.mode != "ai_auto" or dialog.mode_version != run.mode_version):
        return "superseded"
    if now >= run.processing_deadline_at:
        return "processing_budget_exhausted"
    return ""


def handoff_active_requests(cursor, dialog, ticket):
    """Link active questions to the card while the caller still holds the mode locks."""
    now = database_time()
    active = AIRequest.objects.filter(
        Q(status="pending") | Q(status="answered_full", aianswerinput__answer__parts__aipartdelivery__status__in=(
            "waiting", "pending", "unknown_outcome",
        )),
        dialog=dialog, mode_version=dialog.mode_version, automation_epoch=cursor.automation_epoch,
    )
    request_ids = list(active.values_list("pk", flat=True).distinct())
    if not request_ids:
        return
    messages = Message.objects.filter(
        airequestmessage__request_id__in=request_ids, dialog=dialog, ticket__isnull=True,
    )
    message_ids = list(messages.values_list("pk", flat=True))
    messages.update(ticket=ticket)
    InboundRoute.objects.filter(message_id__in=message_ids, status__in=("ai_processing", "needs_review")).update(
        status="attached", reason="student_requested_manager",
    )
    for run in AIProcessingRun.objects.filter(inputs__request_id__in=request_ids, status="running").distinct():
        Task.objects.filter(pk=run.task_id).update(
            status=Task.Status.FAILED, result={"reason": "superseded"},
            lease_token=None, locked_until=None, updated_at=now,
        )
        run.handoff_ticket = ticket
        run.save(update_fields=["handoff_ticket"])
        record_no_answer(run, "superseded", now)
    saved = AIProcessingRun.objects.filter(
        inputs__request_id__in=request_ids, status="answer_saved", handoff_ticket__isnull=True,
        mode_version=dialog.mode_version, automation_epoch=cursor.automation_epoch,
    ).distinct().order_by("answer_id")
    for run in saved:
        run.handoff_ticket = ticket
        run.save(update_fields=["handoff_ticket"])
        cancel_for_handoff(run.answer_id, now)
    AIRequest.objects.filter(pk__in=request_ids).update(status="escalated_to_human", finished_at=now)
