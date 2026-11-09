"""Student decisions over an automatic question, called with its channel and dialog locked."""

from jobs.models import BackgroundTask as Task

from .models import AIProcessingRun, InboundRoute, Ticket


def resolve_request(cursor, dialog, request, user_id, now):
    from .ai_delivery import cancel_unstarted_parts
    from .ai_processing import record_no_answer
    from .handoff_plans import refresh_dialog_plans

    request.resolved_at, request.resolved_by_vk_id = now, user_id
    request.save(update_fields=["resolved_at", "resolved_by_vk_id"])
    # Remove this question's handoff cause before cancellation callbacks revisit the plan.
    refresh_dialog_plans(cursor, dialog)
    runs = AIProcessingRun.objects.filter(
        inputs__request=request, inputs__request_version=request.request_version,
    ).distinct().order_by("pk")
    for run in runs:
        if run.status == "running":
            Task.objects.filter(pk=run.task_id).update(
                status=Task.Status.FAILED, result={"reason": "student_resolved"},
                lease_token=None, locked_until=None, updated_at=now,
            )
            record_no_answer(run, "student_resolved", now)
        if run.answer_id:
            cancel_unstarted_parts(run.answer_id, now, "question_resolved")
    if request.status == "pending":
        request.status, request.finished_at = "stale_cancelled", now
        request.save(update_fields=["status", "finished_at"])
    InboundRoute.objects.filter(
        message__airequestmessage__request=request, message__ticket__isnull=True,
        status__in=("pending", "ai_processing", "needs_review"),
    ).update(status="ignored", reason="student_resolved")


def request_manager(cursor, dialog, request, now):
    from .handoff_plans import finish_explicit_handoff

    ticket = Ticket.objects.create(dialog=dialog, handoff_reason="student_requested_manager", created_at=now)
    finish_explicit_handoff(cursor, dialog, ticket, planned_request_ids=(request.pk,))
    return ticket
