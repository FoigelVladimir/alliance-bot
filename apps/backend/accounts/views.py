import json
from functools import wraps
from uuid import UUID, uuid4

from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from support.queries import allowed_communities

from . import services, vk_id_client
from .models import AccessApplication, StaffProfile
from .policies import (
    CHANGE_ACTIONS, can_change_access, cleared_for_incident, has_work_access, profile_from_session,
    quarantine_incident, review_roles,
)

LOGIN_MESSAGES = {
    "unavailable": "Вход через ВК пока недоступен.",
    "failed": "Не удалось подтвердить вход через ВК. Попробуйте ещё раз.",
    "cancelled": "Вход отменён или время подтверждения истекло. Попробуйте ещё раз.",
}
ERROR_MESSAGES = {
    "authentication_required": "Войдите через ВК.",
    "forbidden": "У вас нет доступа к этому действию.",
    "invalid_request": "Проверьте заполненные поля.",
    "not_found": "Запись не найдена.",
    "ai_answer_not_found": "Ответ не найден.",
    "ai_processing_run_not_found": "Обработка не найдена.",
    "ai_dialog_stale": "Вопрос или режим диалога изменился. Обновите страницу.",
    "ai_dialog_inactive": "Подключение к этой автоматической обработке уже недоступно.",
    "human_ticket_exists": "Диалог уже передан сотрудникам. Откройте существующее обращение.",
    "operation_superseded": "Обращение закрыто или его назначение изменилось.",
    "review_revision_conflict": "Оценка изменилась. Сравните её со своими правками.",
    "comment_too_long": "Комментарий может содержать не более 2000 символов.",
    "review_revision_exhausted": "Достигнут предел ревизий оценки.",
    "operation_mismatch": "Эта команда уже была отправлена с другими данными.",
    "access_blocked": "Доступ заблокирован.",
    "access_already_granted": "Рабочий доступ уже предоставлен.",
    "application_not_available": "Обратитесь к руководителю, чтобы восстановить рабочий доступ.",
    "application_pending": "Заявка уже на рассмотрении.",
    "application_retry_too_soon": "Срок повторной подачи ещё не наступил.",
    "application_already_decided": "Заявка уже рассмотрена.",
    "applicant_identity_changed": "Данные для проверки личности изменились. Откройте заявку заново.",
    "applicant_access_changed": "Состояние доступа заявителя изменилось.",
    "access_version_conflict": "Права сотрудника изменились. Откройте карточку заново.",
    "access_state_conflict": "Действие недоступно в текущем состоянии.",
}


def private(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            response = view(request, *args, **kwargs)
        except services.AccessError as error:
            response = JsonResponse({
                "error": {"code": error.code, "message": ERROR_MESSAGES.get(error.code, "Действие недоступно."),
                          "field_errors": {}},
                **error.details,
            }, status=error.status)
        add_never_cache_headers(response)
        response.setdefault("Referrer-Policy", "no-referrer")
        response["X-Robots-Tag"] = "noindex"
        return response
    return wrapped


def render_form_page(request, context):
    response = render(request, "accounts/access.html", context)
    # Keep the origin on form submissions without exposing the page URL.
    response["Referrer-Policy"] = "strict-origin"
    return response


def home_redirect(reason=None):
    url = reverse("workspace-home")
    return redirect(f"{url}?login={reason}" if reason else url)


@private
@require_GET
def home(request):
    profile = profile_from_session(request.session)
    latest = services.latest_application(profile) if profile else None
    restriction = services.application_restriction(profile, latest) if profile else None
    return render_form_page(request, {
        "profile": profile,
        "application": latest,
        "can_apply": bool(profile and not restriction),
        "retry_at": restriction.details.get("retry_after_at") if restriction else None,
        "operation_id": uuid4(),
        "work_access": has_work_access(profile),
        "support_access": has_work_access(profile) and not quarantine_incident() and allowed_communities(profile).exists(),
        "can_review": bool(review_roles(profile)),
        "login_error": LOGIN_MESSAGES.get(request.GET.get("login")),
    })


@private
@require_POST
def start(request):
    if not vk_id_client.is_configured():
        return home_redirect("unavailable")
    if profile_from_session(request.session):
        return home_redirect()
    return redirect(services.start_login(request.session))


@private
@require_GET
def callback(request):
    fields = {}
    for name in ("code", "device_id", "state"):
        values = request.GET.getlist(name)
        fields[name] = values[0] if len(values) == 1 and len(values[0]) <= 4096 else ""
    if not fields["state"]:
        return home_redirect("cancelled")
    requested = bool(fields["code"] and fields["device_id"] and "error" not in request.GET)
    attempt = services.claim_login(request.session, fields["state"], requested)
    if attempt is None:
        return home_redirect("cancelled")
    try:
        identity = vk_id_client.exchange(**fields, verifier=attempt.verifier)
    except vk_id_client.VKLoginError:
        identity = None
    success = services.finish_login(request.session, attempt, identity)
    if success and attempt.recovery_id:
        return redirect("leader-recovery-confirmed")
    if success and request.session.get("participant_return"):
        return redirect("participant-entry")
    return home_redirect(None if success else "failed")


@private
@require_POST
def logout(request):
    services.end_login(request.session)
    return home_redirect()


def json_object(request, required, optional=()):
    try:
        if request.content_type == "application/json":
            data = json.loads(request.body)
        elif request.content_type == "application/x-www-form-urlencoded":
            if any(len(values) != 1 for _, values in request.POST.lists()):
                raise ValueError
            data = request.POST.dict()
            data.pop("csrfmiddlewaretoken", None)
        else:
            raise ValueError
        if not isinstance(data, dict) or not set(required) <= data.keys() <= set(required) | set(optional):
            raise ValueError
        data["client_operation_id"] = UUID(data["client_operation_id"])
    except (ValueError, TypeError, AttributeError):
        raise services.AccessError("invalid_request", 400) from None
    return data


def text_value(data, key):
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > 1000 or "\0" in value:
        raise services.AccessError("invalid_request", 400)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise services.AccessError("invalid_request", 400) from None
    return value


def version_value(data, key):
    value = data[key]
    if isinstance(value, str) and len(value) <= 10 and value.isascii() and value.isdigit():
        value = int(value)
    if type(value) is not int or not 0 < value < 2**31:
        raise services.AccessError("invalid_request", 400)
    return value


@private
@require_http_methods(["GET", "POST"])
def application(request):
    profile = profile_from_session(request.session)
    if not profile:
        raise services.AccessError("authentication_required", 401)
    if request.method == "POST":
        data = json_object(request, ("client_operation_id",), ("club_activity",))
        entry, replayed = services.apply_for_access(
            request.session, data["client_operation_id"], text_value(data, "club_activity"),
        )
        if request.content_type != "application/json":
            return home_redirect()
        return JsonResponse({"application": services.application_data(entry)}, status=200 if replayed else 201)
    entry = services.latest_application(profile)
    restriction = services.application_restriction(profile, entry)
    return JsonResponse({
        "profile": services.access_data(profile),
        "application": services.application_data(entry),
        "can_apply": restriction is None,
        **({"retry_after_at": restriction.details["retry_after_at"]}
           if restriction and "retry_after_at" in restriction.details else {}),
    })


def reviewer(request):
    profile = profile_from_session(request.session)
    roles = review_roles(profile)
    if not roles:
        raise services.AccessError("forbidden", 403)
    return profile, roles


@private
@require_GET
def applications(request):
    actor, roles = reviewer(request)
    entries = AccessApplication.objects.exclude(profile=actor).select_related("profile").order_by("-submitted_at", "-id")
    staff = StaffProfile.objects.exclude(pk=actor.pk).exclude(
        access_state=StaffProfile.Access.NOT_GRANTED,
    ).exclude(role=StaffProfile.Role.LEADER).order_by("display_name", "id")
    incident = quarantine_incident()
    if incident:
        entries = entries.filter(profile__cleared_incident=incident)
        staff = staff.filter(cleared_incident=incident)
    staff_rows = []
    for employee in staff:
        actions = [
            (action, label, uuid4()) for action, label in CHANGE_ACTIONS.items()
            if can_change_access(actor, employee, action) and services.applicable_access_change(employee, action)
        ]
        if employee.access_state == StaffProfile.Access.REVOKED:
            state_label = "Допуск отозван"
        elif employee.blocked_at:
            state_label = "Доступ заблокирован"
        else:
            state_label = "Доступ открыт"
        role_label = "Администратор" if employee.role == StaffProfile.Role.ADMINISTRATOR else "Менеджер"
        staff_rows.append((employee, f"{role_label} · {state_label}", actions))
    return render_form_page(request, {
        "review": True,
        "application_rows": [(entry, uuid4(), uuid4()) for entry in entries],
        "roles": roles,
        "staff_rows": staff_rows,
    })


@private
@require_GET
def application_detail(request, application_id):
    actor, roles = reviewer(request)
    entry = AccessApplication.objects.select_related("profile").filter(pk=application_id).first()
    if entry is None or not cleared_for_incident(entry.profile, quarantine_incident()):
        raise services.AccessError("not_found", 404)
    if entry.profile_id == actor.pk:
        raise services.AccessError("forbidden", 403)
    profile = entry.profile
    return JsonResponse({
        "application": services.application_data(entry),
        "profile": services.access_data(profile),
        "identity_version": profile.identity_version,
        "roles": roles if entry.status == AccessApplication.Status.PENDING else (),
    })


@private
@require_POST
def decision(request, application_id, action):
    required = ["client_operation_id", "expected_identity_version"]
    if action == "approve":
        data = json_object(request, [*required, "role"])
        value = data["role"]
        if value not in (StaffProfile.Role.MANAGER, StaffProfile.Role.ADMINISTRATOR):
            raise services.AccessError("invalid_request", 400)
    else:
        data = json_object(request, required, ("applicant_message",))
        value = text_value(data, "applicant_message").strip()
    entry, profile, replayed = services.decide_application(
        request.session, application_id, data["client_operation_id"],
        version_value(data, "expected_identity_version"), action, value,
    )
    if request.content_type != "application/json":
        return redirect("staff-applications")
    return JsonResponse({
        "application": services.application_data(entry), "profile": services.access_data(profile), "replayed": replayed,
    })


@private
@require_POST
def change_access(request, profile_id):
    data = json_object(request, ("client_operation_id", "expected_access_version", "action"))
    action = data["action"]
    if not isinstance(action, str) or action not in CHANGE_ACTIONS:
        raise services.AccessError("invalid_request", 400)
    event, profile, replayed = services.change_access(
        request.session, profile_id, data["client_operation_id"],
        version_value(data, "expected_access_version"), action,
    )
    if request.content_type != "application/json":
        return redirect("staff-applications")
    return JsonResponse({
        "result": event.after, "profile": services.access_data(profile), "replayed": replayed,
    })


@private
@require_http_methods(["GET", "POST"])
def recovery(request):
    code = request.GET.get("code", "") if request.method == "GET" else request.POST.get("recovery_code", "")
    if not code or len(code) > 128:
        return home_redirect("cancelled")
    if request.method == "GET":
        return render_form_page(request, {"recovery": True, "recovery_code": code})
    if not vk_id_client.is_configured():
        return home_redirect("unavailable")
    if profile_from_session(request.session):
        return home_redirect()
    try:
        url = services.start_login(request.session, recovery_code=code)
    except services.AccessError:
        return home_redirect("cancelled")
    return redirect(url)


@private
@require_GET
def recovery_confirmed(request):
    if not request.session.get("leader_recovery_verified"):
        return home_redirect()
    return render_form_page(request, {"recovery": True})
