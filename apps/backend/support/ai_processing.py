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

from .ai_delivery import cancel_for_handoff, cancel_unstarted_parts
from .ai_generation import GeneratedAnswer, ModelUnavailable, generate_quote, save_answer
from .attachment_responses import save_prompt
from .models import (
    AIAnswer, AIPartDelivery, AIProcessingInput, AIProcessingInputMessage, AIProcessingRun, AIRequest, AIRequestMessage,
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
            or message.direction != "inbound"):
        return False
    text = " ".join(message.text.split())
    if (not text and not message.attachments.exists()) or len(text) > QUESTION_LIMIT:
        return False
    if settings.SUPPORT_AI_CONTEXT_ENABLED and text:
        from .ai_context import enqueue_context

        return enqueue_context(cursor, dialog, route, message)
    now = database_time()
    request = None
    if route.reply_to_cmid is not None:
        request = AIRequest.objects.filter(
            Q(status="pending") | Q(status__in=("answered_full", "answered_partial"), aianswerinput__answer__parts__aipartdelivery__status__in=("waiting", "pending")),
            dialog=dialog, mode_version=dialog.mode_version, automation_epoch=cursor.automation_epoch, message_links__message__conversation_message_id=route.reply_to_cmid,
            resolved_at__isnull=True,
            message_links__message__sender_vk_id=message.sender_vk_id,
            message_links__message__inboundroute__sequence__lt=route.sequence,
        ).first()
        if request is None or len(request.normalized_query) + 1 + len(text) > QUESTION_LIMIT:
            return False
    create_question_run(cursor, dialog, route, message, request=request, text=text)
    return True


def create_question_run(cursor, dialog, route, message, *, request=None, text=None, query=None,
                        started_at=None, task=None, deadline=None, refresh_plans=True):
    """The caller holds the channel/dialog locks; original message rows stay intact."""
    now = database_time()
    started_at = started_at or now
    text = " ".join(message.text.split()) if text is None else text
    old_version = request.request_version if request else None
    if request is None:
        request = AIRequest.objects.create(
            dialog=dialog, mode_version=dialog.mode_version, automation_epoch=cursor.automation_epoch,
            request_version_started_at=started_at, created_at=now, normalized_query=query if query is not None else text,
        )
    else:
        for running in AIProcessingRun.objects.filter(inputs__request=request, status="running").distinct().order_by("pk"):
            Task.objects.filter(pk=running.task_id).update(
                status=Task.Status.FAILED, result={"reason": "superseded"},
                lease_token=None, locked_until=None, updated_at=now,
            )
            record_no_answer(running, "superseded", now)
        request.request_version += 1
        request.status, request.finished_at = "pending", None
        request.request_version_started_at = started_at
        request.normalized_query = query if query is not None else "\n".join(part for part in (request.normalized_query, text) if part)
        request.save(update_fields=["request_version", "request_version_started_at", "normalized_query", "status", "finished_at"])
    AIRequestMessage.objects.create(request=request, message=message)

    operation = task.idempotency_key if task else uuid4()
    task = task or enqueue(TASK_TYPE, operation, {"dialog_id": dialog.pk}, priority=10)
    run = AIProcessingRun.objects.create(
        operation_id=operation, task=task, dialog=dialog, mode_version=dialog.mode_version,
        automation_epoch=cursor.automation_epoch, created_at=started_at,
        processing_deadline_at=deadline or started_at + LOOKUP_BUDGET,
    )
    snapshot = AIProcessingInput.objects.create(
        run=run, request=request, request_version=request.request_version, question_text=request.normalized_query,
    )
    AIProcessingInputMessage.objects.bulk_create([
        AIProcessingInputMessage(processing_input=snapshot, message_id=message_id)
        for message_id in request.message_links.values_list("message_id", flat=True)
    ])
    route.status, route.reason = "ai_processing", ""
    from .handoff_plans import refresh_dialog_plans

    if refresh_plans:
        refresh_dialog_plans(cursor, dialog)
    if old_version is not None:
        for answer in AIAnswer.objects.filter(inputs__request=request,
                inputs__request_version_snapshot__lte=old_version).distinct().order_by("pk"):
            cancel_unstarted_parts(answer.pk, now, "question_changed")
    if dialog.mode != "ai_auto":
        route.refresh_from_db(fields=["status", "reason"])
    return run, request


def record_no_answer(run, reason, now, hits=()):
    from .ai_join import completion_deadline

    run.status, run.reason, run.finished_at = "no_answer", reason, now
    run.join_until = completion_deadline(now, reason)
    run.sources_used = [
        {key: value for key, value in asdict(hit).items() if key not in {"content", "heading"}} for hit in hits
    ]
    run.save(update_fields=["status", "reason", "finished_at", "join_until", "sources_used"])


def lock_run(run_id, hits=()):
    candidate = AIProcessingRun.objects.values("dialog_id", "dialog__cursor_id").get(pk=run_id)
    cursor = LongPollCursor.objects.select_for_update().get(pk=candidate["dialog__cursor_id"])
    sources_current = not hits or sources_are_current(hits)
    dialog = Dialog.objects.select_for_update().get(pk=candidate["dialog_id"])
    run = AIProcessingRun.objects.select_for_update().get(pk=run_id)
    return cursor, dialog, run, sources_current


def return_for_review(run, status, now):
    snapshot = run.inputs.get()
    updated = AIRequest.objects.filter(pk=snapshot.request_id, request_version=snapshot.request_version).exclude(
        status="escalated_to_human",
    ).update(
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
    from .handoff_plans import ensure_handoff, evaluate_plan

    if reason == "no_reliable_basis" and settings.SUPPORT_AI_HANDOFF_ENABLED:
        ensure_handoff(run)
    elif run.handoff_plan_id:
        evaluate_plan(run.handoff_plan_id)


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
    from .generation_slot import generation_slot

    with generation_slot() as acquired:
        if not acquired:
            return False
        from .ai_context import run_context_once

        if run_context_once():
            return True
        return _run_lookup_once(search, generate)


def _run_lookup_once(search, generate):
    while recover_expired_lookup():
        pass
    task = claim([TASK_TYPE])
    if task is None:
        return False
    run = AIProcessingRun.objects.get(task=task)
    snapshot = run.inputs.get()
    question = snapshot.question_text
    has_attachments = snapshot.messages.filter(attachments__isnull=False).exists()
    try:
        with transaction.atomic():
            cursor, dialog, locked, _ = lock_run(run.pk)
            renew(task)
            reason = lookup_block_reason(cursor, dialog, locked, database_time())
        hits, quote = [], None
        lookup_ms = None
        prompt = has_attachments and not question.strip()
        if not reason and not prompt:
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
                            if has_attachments:
                                quote = generate(question, hits, remaining, has_unread_attachments=True)
                            else:
                                quote = generate(question, hits, remaining)
                        except ModelUnavailable:
                            reason = "model_unavailable"
                        else:
                            if quote is None:
                                reason = "no_reliable_basis"
        if has_attachments and reason == "no_reliable_basis":
            # Missing attachment contents are not grounds for an automatic human ticket.
            prompt, reason = True, ""
        with transaction.atomic():
            cursor, dialog, locked, sources_current = lock_run(run.pk, () if prompt else hits)
            now = database_time()
            blocked = lookup_block_reason(cursor, dialog, locked, now)
            partial = isinstance(quote, GeneratedAnswer) and bool(quote.remaining_work)
            if partial and not settings.SUPPORT_AI_HANDOFF_ENABLED:
                reason, quote = "handoff_not_enabled", None
            established_handoff = (settings.SUPPORT_AI_HANDOFF_ENABLED and (reason == "no_reliable_basis" or partial)
                                   and blocked == "processing_budget_exhausted")
            if blocked and not established_handoff:
                reason, hits, quote = blocked, [], None
                prompt = False
            elif not sources_current:
                reason, hits, quote = "sources_changed", [], None
            if prompt:
                answer = save_prompt(locked, now, lookup_ms)
                finish(task, {"answer_id": answer.pk})
                # The question remains pending until text clarification or an explicit handoff.
            elif quote is None:
                finish(task, {"reason": reason})
                finish_lookup(locked, reason, now, hits)
            else:
                answer = save_answer(locked, quote, hits, now, lookup_ms)
                finish(task, {"answer_id": answer.pk})
                # Preview remains available to staff while the delivery worker is separate.
                return_for_review(locked, "answered_partial" if partial else "answered_full", now)
    except LeaseLost:
        pass
    return True


def lookup_block_reason(cursor, dialog, run, now):
    if run.status != "running":
        raise LeaseLost(run.pk)
    if quarantine_incident():
        return "recovered_from_backup"
    if run.inputs.filter(request__resolved_at__isnull=False).exists():
        return "student_resolved"
    if (cursor.automation_paused or cursor.automation_epoch != run.automation_epoch
            or dialog.mode != "ai_auto" or dialog.mode_version != run.mode_version):
        return "superseded"
    if now >= run.processing_deadline_at:
        return "processing_budget_exhausted"
    return ""


def handoff_active_requests(cursor, dialog, ticket, *, reason="student_requested_manager", planned_request_ids=()):
    """Link active questions to the card while the caller still holds the mode locks."""
    now = database_time()
    active = AIRequest.objects.filter(
        Q(pk__in=planned_request_ids) | Q(status__in=("pending", "failed", "answered_partial", "awaiting_clarification")) | Q(status="answered_full", aianswerinput__answer__parts__aipartdelivery__status__in=(
            "waiting", "pending", "unknown_outcome", "failed", "cancelled",
        )),
        dialog=dialog, mode_version=dialog.mode_version, automation_epoch=cursor.automation_epoch,
        resolved_at__isnull=True,
    )
    request_ids = list(active.values_list("pk", flat=True).distinct())
    if not request_ids:
        return
    messages = Message.objects.filter(
        airequestmessage__request_id__in=request_ids, dialog=dialog, ticket__isnull=True,
    )
    message_ids = list(messages.values_list("pk", flat=True))
    messages.update(ticket=ticket)
    InboundRoute.objects.filter(message_id__in=message_ids, status__in=("pending", "ai_processing", "needs_review")).update(
        status="attached", reason=reason,
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
    AIProcessingRun.objects.filter(
        inputs__request_id__in=request_ids, status="no_answer", handoff_ticket__isnull=True,
        mode_version=dialog.mode_version, automation_epoch=cursor.automation_epoch,
    ).update(handoff_ticket=ticket)
    for run in saved:
        run.handoff_ticket = ticket
        run.save(update_fields=["handoff_ticket"])
        AIPartDelivery.objects.filter(part__answer_id=run.answer_id).update(handoff_ticket=ticket)
        Message.objects.filter(ai_answer_part__answer_id=run.answer_id, ticket__isnull=True).update(ticket=ticket)
        cancel_for_handoff(run.answer_id, now)
    AIRequest.objects.filter(pk__in=request_ids).update(status="escalated_to_human", finished_at=now)
