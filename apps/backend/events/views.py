from uuid import uuid4
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.paginator import Paginator
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from accounts.models import StaffProfile
from accounts.services import AccessError
from accounts.views import private
from workspace.services import workspace_actor

from . import http, lifecycle, queries, services
from .forms import schema_for
from .models import Event, FormRevision

ERRORS = {
    "event_revision_conflict": "Мероприятие изменилось. Сравните текущие данные с вашим вводом.",
    "application_revision_conflict": "Заявка изменилась. Проверьте сохранённые данные перед повтором.",
    "event_state_conflict": "Действие недоступно в текущем состоянии мероприятия.",
    "application_state_conflict": "Состояние заявки изменилось. Проверьте доступные действия.",
    "registration_closed": "Приём новых заявок закрыт. Сохранённый черновик остаётся доступным.",
    "registration_window_changed": "Открыт новый период регистрации. Проверьте актуальные условия и подтвердите подачу заново.",
    "registration_form_changed": "Форма регистрации изменилась. Проверьте поля и сохраните правки заново.",
    "registration_form_incompatible": "После первой подачи нельзя менять формат и обязательные правила. Можно добавить необязательное поле или уточнить подсказку.",
    "registration_deadline_invalid": "Срок регистрации должен быть в будущем.",
    "registration_state_conflict": "Проверьте текущий срок. Первое открытие, изменение срока и повторное открытие выполняются отдельно.",
    "event_start_preview_changed": "Состав или заявки изменились. Откройте новый предпросмотр старта.",
    "pending_applications_confirmation_required": "Подтвердите список заявок, которые останутся без допуска.",
    "event_finish_preview_changed": "Отметки изменились. Откройте новый предпросмотр завершения.",
    "event_interruption_preview_changed": "Факты изменились. Откройте новый предпросмотр остановки.",
    "verified_person_data_conflict": "Правка расходится с подтверждёнными сведениями человека. Передайте её администратору данных.",
    "application_validation_failed": "Проверьте обязательные поля заявки.",
    "invalid_review_reason": "Укажите обязательное основание или пояснение для выбранного действия.",
    "responsible_not_eligible": "Выбранный сотрудник больше не может быть ответственным. Выберите действующего сотрудника.",
    "helper_not_eligible": "Проверьте допуск помощника. Ответственный не дублируется в помощниках.",
    "operation_mismatch": "Этот идентификатор команды уже использован с другими данными. Проверьте результат и отправьте новую команду.",
    "invalid_event_datetime": "Укажите дату, время и часовой пояс. Для неоднозначного местного времени нужен явный часовой сдвиг.",
    "invalid_content": "Проверьте заполненные поля и их длину.",
    "reporting_preview_changed": "Данные отчётности изменились. Откройте новый предпросмотр.",
    "reporting_primary_in_use": "На мероприятие ссылаются исключённые дубли. Сначала разберите эти связи.",
}
CARD_FIELDS = ["title", "sport", "participation_format", "planned_start_at", "timezone_name", "location"]


def page(request, template, context, *, status=200):
    response = render(request, "events/" + template + ".html", context, status=status)
    response["Referrer-Policy"] = "strict-origin"
    return response


def candidates():
    return StaffProfile.objects.filter(access_state="granted", blocked_at__isnull=True, disabled=False,
        role__in=["manager", "administrator", "leader"], bindings__active=True).order_by("display_name", "pk")


def candidate_data():
    return [{"id": str(profile.pk), "name": profile.display_name or f"Сотрудник {profile.pk}"} for profile in candidates()]


@private
@require_http_methods(["GET"])
def responsible_candidates(request):
    workspace_actor(request.session)
    return JsonResponse({"candidates": candidate_data()})


def card_input(request, data):
    values = {key: data[key] for key in CARD_FIELDS}
    values["description"] = data.get("description", "")
    values["planned_start_at"] = http.instant(data["planned_start_at"],
        timezone_name=data["timezone_name"] if request.content_type != "application/json" else None)
    return values


@private
@require_http_methods(["GET", "POST"])
def catalog(request):
    actor = workspace_actor(request.session)
    error = None
    if request.method == "POST":
        try:
            data = http.command(request, ["responsible_profile_id", *CARD_FIELDS], ["description"])
            command, replayed = services.create_event(request.session, data["client_operation_id"],
                http.integer(request, data, "responsible_profile_id"), card_input(request, data))
        except AccessError as caught:
            if http.as_json(request) or caught.status not in (400, 409, 422):
                raise
            error = caught
        else:
            return command_response(request, command, replayed, created=True)
    events = Event.objects.filter(deleted_at__isnull=True, deletion_pending=False, restoration_verification_required=False)
    state = request.GET.get("state", "")
    if state:
        if state not in queries.EVENT_LABELS or state == "deleted":
            raise AccessError("invalid_request", 400)
        events = events.filter(state=state)
    rows = Paginator(events.select_related("current_window").order_by("-planned_start_at", "-pk"), 20).get_page(request.GET.get("page", "1"))
    if http.as_json(request):
        return JsonResponse({"events": [queries.event_data(event) for event in rows], "page": rows.number, "total_count": rows.paginator.count})
    for event in rows:
        event.status_label = queries.EVENT_LABELS[event.state]
    return page(request, "catalog", {"events": rows, "candidates": candidate_data(), "operation_id": uuid4(),
        "form_values": request.POST if error else {"timezone_name": "Europe/Moscow", "responsible_profile_id": str(actor.pk)},
        "error_message": ERRORS.get(error.code, "Не удалось создать мероприятие. Проверьте данные.") if error else None},
        status=error.status if error else 200)


def detail_page(request, event, *, error=None):
    actor = workspace_actor(request.session)
    reviewer = services.organiser(event, actor)
    current = event.current_form or event.forms.order_by("-revision").first()
    local_start = event.planned_start_at.astimezone(ZoneInfo(event.timezone_name)).strftime("%Y-%m-%dT%H:%M")
    applications = event.applications.filter(first_submitted_at__isnull=False).order_by("first_submitted_at", "pk")
    for application in applications:
        application.status_label = queries.APPLICATION_LABELS[application.status]
    participation = queries.participation_rows(event)
    for item in participation:
        item["operation_id"] = uuid4()
    return page(request, "detail", {"event": event, "event_data": queries.event_data(event), "actor": actor,
        "is_organiser": reviewer, "is_responsible": event.responsible_id == actor.pk and event.responsible_generation == actor.access_generation,
        "candidates": candidate_data(), "applications": applications, "participation": participation,
        "operation_id": uuid4(), "helper_operation_id": uuid4(), "responsible_operation_id": uuid4(), "window_operation_id": uuid4(),
        "form_operation_id": uuid4(), "publish_operation_id": uuid4(), "card_operation_id": uuid4(),
        "prepared_forms": event.forms.order_by("-revision"), "form_schema": current.schema if current else schema_for(event.participation_format),
        "planned_local": local_start, "card_values": request.POST if error and "title" in request.POST else {**queries.event_data(event), "planned_start_at": local_start},
        "error_message": ERRORS.get(error.code, "Действие недоступно. Проверьте текущие данные.") if error else None},
        status=error.status if error else 200)


@private
@require_http_methods(["GET"])
def detail(request, event_id):
    workspace_actor(request.session)
    event = services.get_event(event_id)
    if http.as_json(request):
        return JsonResponse({"event": queries.event_data(event), "participation": queries.participation_rows(event)})
    return detail_page(request, event)


@private
@require_http_methods(["GET"])
def public(request, event_id):
    try:
        event = services.get_event(event_id)
    except AccessError as error:
        if http.as_json(request) or error.status not in (404, 503):
            raise
        return page(request, "unavailable", {}, status=error.status)
    data = queries.event_data(event, public=True)
    if http.as_json(request):
        return JsonResponse({"event": data})
    return page(request, "public", {"event": event, "event_data": data,
        "bot_url": "https://vk.me/club" + settings.VK_GROUP_ID if settings.VK_GROUP_ID.isascii() and settings.VK_GROUP_ID.isdecimal() and len(settings.VK_GROUP_ID) <= 19 else None})


def command_response(request, command, replayed, *, created=False):
    if http.as_json(request):
        return JsonResponse({"operation": {"client_operation_id": str(command.client_operation_id), "action": command.action,
            "replayed": replayed, "result": command.result}, "event": queries.event_data(services.get_event(command.event_id))},
            status=201 if created and not replayed else 200)
    return HttpResponseRedirect(reverse("event-detail", args=[command.event_id]), status=303)


@private
@require_http_methods(["POST"])
def card_command(request, event_id, action):
    workspace_actor(request.session)
    try:
        if action == "save":
            data = http.command(request, ["expected_event_revision", *CARD_FIELDS], ["description"])
            result = services.save_card(request.session, event_id, data["client_operation_id"],
                http.integer(request, data, "expected_event_revision"), card_input(request, data))
        elif action == "helpers":
            data = http.command(request, ["expected_event_revision", "helper_profile_id", "action"])
            result = services.change_helper(request.session, event_id, data["client_operation_id"],
                http.integer(request, data, "expected_event_revision"), http.integer(request, data, "helper_profile_id"), data["action"])
        elif action == "responsible":
            data = http.command(request, ["expected_event_revision", "responsible_profile_id"], ["replacement_reason"])
            result = services.change_responsible(request.session, event_id, data["client_operation_id"],
                http.integer(request, data, "expected_event_revision"), http.integer(request, data, "responsible_profile_id"), data.get("replacement_reason"))
        elif action in {"open", "reopen", "deadline"}:
            data = http.command(request, ["expected_event_revision", "registration_deadline_at", *(["form_revision_id"] if action == "open" else [])])
            event = services.get_event(event_id)
            deadline = http.instant(data["registration_deadline_at"], timezone_name=event.timezone_name if request.content_type != "application/json" else None)
            result = services.registration_window(request.session, event_id, data["client_operation_id"],
                http.integer(request, data, "expected_event_revision"), action, deadline,
                form_id=http.integer(request, data, "form_revision_id") if action == "open" else None)
        elif action == "publish_form":
            data = http.command(request, ["expected_event_revision", "form_revision_id"])
            result = services.publish_form(request.session, event_id, data["client_operation_id"],
                http.integer(request, data, "expected_event_revision"), http.integer(request, data, "form_revision_id"))
        else:
            raise AccessError("not_found", 404)
    except AccessError as error:
        if http.as_json(request) or error.status not in (400, 409, 422):
            raise
        return detail_page(request, services.get_event(event_id), error=error)
    return command_response(request, *result)


@private
@require_http_methods(["POST"])
def prepare_form(request, event_id):
    workspace_actor(request.session)
    if request.content_type == "application/json":
        data = http.command(request, ["expected_event_revision", "schema"])
        schema = data["schema"]
    else:
        data = http.command(request, ["expected_event_revision", "participation_format"],
            ["categories", "min_players", "max_players", "full_name", "study_group", "comment"])
        bounds = {}
        for key in ("min_players", "max_players"):
            bounds[key] = http.integer(request, data, key) if data.get(key) else None
        schema = schema_for(data["participation_format"], categories=[item.strip() for item in data.get("categories", "").splitlines() if item.strip()],
            optional_fields=[key for key in ("full_name", "study_group", "comment") if data.get(key) == "on"], **bounds)
    try:
        result = services.prepare_form(request.session, event_id, data["client_operation_id"], http.integer(request, data, "expected_event_revision"), schema)
    except AccessError as error:
        if http.as_json(request) or error.status not in (400, 409, 422):
            raise
        return detail_page(request, services.get_event(event_id), error=error)
    return command_response(request, *result)
