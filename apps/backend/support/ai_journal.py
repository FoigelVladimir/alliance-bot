import re
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from django.core.paginator import Paginator
from django.db.models import Case, DateTimeField, F, PositiveBigIntegerField, When
from django.http import JsonResponse, QueryDict
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_safe

from accounts.services import AccessError
from accounts.views import json_object, private

from .models import AIAnswerReview, AIAnswerReviewEvent, AIProcessingRun, Ticket
from .queries import allowed_communities
from .services import support_actor

REASONS = {
    "superseded": "Вопрос или режим диалога изменился",
    "recovered_from_backup": "Попытка прервана при восстановлении",
    "processing_budget_exhausted": "Истекло время обработки",
    "model_unavailable": "Поиск временно недоступен",
    "generation_not_enabled": "Найдены источники, генерация ещё не подключена",
    "no_reliable_basis": "Подходящие источники не найдены",
    "sources_changed": "Публикации изменились во время поиска",
    "handoff_not_enabled": "Частичный ответ требует включённой передачи менеджеру",
    "student_resolved": "Студент подтвердил решение вопроса",
}
RESULTS = ("all", "answer", "no_answer", "problem")
DELIVERIES = ("all", "waiting", "pending", "sent", "failed", "cancelled", "unknown_outcome")


def day(value):
    if not value:
        return None
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Invalid calendar date")
    parsed = date.fromisoformat(value)
    if not date.min < parsed < date.max:
        raise ValueError("Date outside filter bounds")
    return parsed


def visible_runs(actor):
    return AIProcessingRun.objects.select_related("answer", "answer__quality_review").filter(
        status__in=("no_answer", "answer_saved"), dialog__cursor_id__in=allowed_communities(actor),
    )


def run_label(run):
    if run.status == "answer_saved":
        from .attachment_responses import PROFILE

        if run.answer.retrieval_snapshot.get("profile") == PROFILE:
            return "Подготовлена просьба описать вложение текстом"
        if run.handoff_plan_id:
            return "Частичный ответ подготовлен"
        return "Ответ подготовлен"
    return REASONS.get(run.reason, "Ответ не подготовлен")


@private
@require_safe
def journal(request, fragment=False):
    actor = support_actor(request.session)
    runs = visible_runs(actor).annotate(
        row_at=Case(When(status="answer_saved", then=F("answer__created_at")),
                    default=F("finished_at"), output_field=DateTimeField()),
        row_id=Case(When(status="answer_saved", then=F("answer_id")),
                    default=F("pk"), output_field=PositiveBigIntegerField()),
    ).order_by("-row_at", "status", "-row_id")
    raw_start, raw_end = request.GET.get("from", ""), request.GET.get("to", "")
    date_error = False
    try:
        start, end = day(raw_start), day(raw_end)
        if start and end and start > end:
            raise ValueError("Reversed date range")
    except ValueError:
        start = end = None
        raw_start = raw_end = ""
        date_error = True
    if start:
        runs = runs.filter(row_at__gte=timezone.make_aware(datetime.combine(start, time.min)))
    if end:
        runs = runs.filter(row_at__lt=timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min)))

    result, delivery = request.GET.get("result", "all"), request.GET.get("delivery", "all")
    result = result if result in RESULTS else "all"
    delivery = delivery if delivery in DELIVERIES else "all"
    if result == "answer":
        runs = runs.filter(status="answer_saved")
    elif result == "no_answer":
        runs = runs.filter(status="no_answer")
    elif result == "problem":
        runs = runs.filter(status="answer_saved", answer__quality_review__problem_flag=True)
    if delivery != "all":
        runs = runs.filter(status="answer_saved", answer__parts__aipartdelivery__status=delivery).distinct()
    page_number = request.GET.get("page", "1")
    if not page_number.isascii() or not page_number.isdecimal() or len(page_number) > 10 or int(page_number) < 1:
        page_number = 1
    page = Paginator(runs.prefetch_related("inputs"), 50).get_page(page_number)
    filters = {"from": raw_start, "to": raw_end, "result": result, "delivery": delivery}
    pagination = QueryDict(mutable=True)
    pagination.update(filters)
    return render(request, "support/ai_journal_list.html" if fragment else "support/ai_journal.html", {
        "page": page, "entries": [(run, run_label(run)) for run in page],
        "filters": filters, "pagination": pagination, "date_error": date_error,
    })


@private
@require_safe
def processing_run(request, run_id):
    actor = support_actor(request.session)
    if not 0 < run_id < 2**63:
        raise AccessError("not_found", 404)
    run = visible_runs(actor).prefetch_related("inputs").filter(pk=run_id, status="no_answer").first()
    if run is None:
        raise AccessError("not_found", 404)
    ticket = Ticket.objects.filter(
        pk=run.handoff_ticket_id, dialog__cursor_id__in=allowed_communities(actor),
    ).first() if run.handoff_ticket_id else None
    return render(request, "support/ai_processing_run.html", {"run": run, "reason": run_label(run), "ticket": ticket})


@private
@require_safe
def answer_detail(request, answer_id):
    from .ai_reconciliation import delivery_state

    actor = support_actor(request.session)
    if not 0 < answer_id < 2**63:
        raise AccessError("not_found", 404)
    run = visible_runs(actor).filter(answer_id=answer_id, status="answer_saved").select_related(
        "answer",
    ).prefetch_related("inputs", "answer__inputs", "answer__parts__aipartdelivery").first()
    if run is None:
        raise AccessError("not_found", 404)
    parts = [{"part": part, "state": delivery_state(actor, part.aipartdelivery), "operation_id": uuid4()}
             for part in run.answer.parts.all()]
    review = AIAnswerReview.objects.filter(answer_id=answer_id).first()
    history = Paginator(AIAnswerReviewEvent.objects.filter(review__answer_id=answer_id).order_by(
        "-resulting_revision",
    ), 50).get_page(request.GET.get("review_page", "1"))
    return render(request, "support/ai_processing_run.html", {"run": run, "reason": run_label(run),
        "part_states": parts, "return_url": reverse("support-ai-answer", args=[answer_id]),
        "quality_review": review, "review_history": history})


@private
@require_http_methods(["GET", "HEAD", "POST"])
def answer_review(request, answer_id):
    from .answer_reviews import parameters, read_review, save_review

    if request.method in ("GET", "HEAD"):
        return JsonResponse(read_review(request.session, answer_id))
    support_actor(request.session)
    if request.content_type != "application/json":
        raise AccessError("invalid_request", 400)
    data = json_object(request, ("client_operation_id", "expected_review_revision", "problem_flag", "comment"))
    return JsonResponse(save_review(request.session, answer_id, data["client_operation_id"], parameters(data)))
