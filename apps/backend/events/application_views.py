import copy
from uuid import uuid4

from django.http import HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from accounts.services import AccessError
from accounts.views import private
from participants.services import actor as participant_actor
from workspace.services import workspace_actor

from . import applications, http, queries, services, views
from .models import Application


def application_page(request, event, application, mode, staff, *, error=None, entered=None, operation_id=None, submit_operation_id=None):
    reviewer = mode == "staff" and services.organiser(event, staff)
    data = queries.application_data(application, reviewer=reviewer)
    form = application.form
    format_changed = False
    if application.status == "draft" and event.current_form_id:
        if event.current_form.schema["participation_format"] == application.participation_format:
            form = event.current_form
        else:
            format_changed = True
    values = copy.deepcopy(entered if entered is not None else application.values)
    for key in ("team_name", "contact", "category", "comment"):
        values.setdefault(key, "")
    values.setdefault("players", [])
    if not values["players"]:
        values["players"] = [{"is_self": application.participation_format == "individual"}]
    if mode == "owner" and application.status in {"draft", "needs_changes"}:
        edit_mode = "draft" if application.status == "draft" else "corrections"
    elif reviewer and application.status in {"submitted", "admitted"}:
        edit_mode = "staff"
    else:
        edit_mode = None
    if event.state != "planned":
        edit_mode = None
    messages = []
    if error:
        for field, message in error.details.get("field_errors", {}).items():
            if field.startswith("players."):
                message = f"Игрок {int(field.split('.')[1]) + 1}: {message}"
            messages.append(message)
    context = {"event": event, "application": application, "data": data, "mode": mode, "reviewer": reviewer,
        "values": values, "form_schema": form.schema, "form_id": form.pk, "edit_mode": edit_mode,
        "form_changed": form.pk != application.form_id, "format_changed": format_changed,
        "operation_id": operation_id or uuid4(), "submit_operation_id": submit_operation_id or uuid4(),
        "withdraw_operation_id": uuid4(), "review_operations": {key: uuid4() for key in ("admit", "reject", "request_changes", "reconsider", "revoke")},
        "field_messages": messages, "error_message": views.ERRORS.get(error.code, "Проверьте введённые данные и текущее состояние заявки.") if error else None,
        "can_withdraw": event.state in {"planned", "started"} and application.status in ({"admitted"} if event.started_at else {"submitted", "needs_changes", "admitted"}),
        "can_submit": edit_mode == "draft" and not format_changed and services.registration_is_open(event),
        "show_comparison": error is not None and error.code == "application_revision_conflict"}
    return views.page(request, "application", context, status=error.status if error else 200)


def response(request, command, replayed, *, staff=False):
    application_id = int(command.result["application_id"])
    event, application, mode, profile = queries.application_access(request.session, command.event_id, application_id, prefer_staff=staff)
    if http.as_json(request):
        return JsonResponse({"operation": {"client_operation_id": str(command.client_operation_id), "action": command.action,
            "replayed": replayed, "result": command.result},
            "application": queries.application_data(application, reviewer=mode == "staff" and services.organiser(event, profile))})
    return HttpResponseRedirect(reverse("event-application", args=[command.event_id, application_id]) + ("?view=staff" if staff else ""), status=303)


@private
@require_http_methods(["GET"])
def participant_entry(request, event_id):
    owner = participant_actor(request.session)
    event = services.get_event(event_id)
    existing = Application.objects.filter(event=event, owner=owner, first_submitted_at__isnull=False).first()
    if existing is None:
        existing = Application.objects.filter(event=event, owner=owner, status="draft", participation_format=event.participation_format).first()
    if existing:
        return HttpResponseRedirect(reverse("event-application", args=[event_id, existing.pk]), status=303)
    return views.page(request, "apply", {"event": event, "event_data": queries.event_data(event, public=True), "operation_id": uuid4()})


@private
@require_http_methods(["GET"])
def detail(request, event_id, application_id):
    event, application, mode, staff = queries.application_access(request.session, event_id, application_id, prefer_staff=request.GET.get("view") == "staff")
    if http.as_json(request):
        return JsonResponse({"application": queries.application_data(application, reviewer=mode == "staff" and services.organiser(event, staff)),
                             "form": application.form.schema})
    return application_page(request, event, application, mode, staff)


@private
@require_http_methods(["POST"])
def open_draft(request, event_id):
    participant_actor(request.session)
    data = http.command(request)
    try:
        result = applications.open_draft(request.session, event_id, data["client_operation_id"])
    except AccessError as error:
        if http.as_json(request) or error.status not in (400, 409, 422):
            raise
        event = services.get_event(event_id)
        return views.page(request, "apply", {"event": event, "event_data": queries.event_data(event, public=True),
            "operation_id": uuid4(), "error_message": views.ERRORS.get(error.code, "Регистрация недоступна.")}, status=error.status)
    return response(request, *result)


@private
@require_http_methods(["POST"])
def edit(request, event_id, application_id, kind):
    event, application, mode, staff = queries.application_access(request.session, event_id, application_id, prefer_staff=kind == "staff")
    if kind in {"draft", "corrections", "save_submit"} and mode != "owner" or kind == "staff" and mode != "staff":
        raise AccessError("permission_denied", 403)
    data = http.editor_command(request, form_required=kind in {"draft", "save_submit"}, submit=kind == "save_submit")
    values = data["values"]
    ui_action = data.get("editor_action", "")
    if ui_action and (ui_action == "add" or ui_action.startswith("remove:")):
        from .forms import validate_values
        schema = event.current_form.schema if application.status == "draft" and event.current_form_id and event.current_form.schema["participation_format"] == application.participation_format else application.form.schema
        values = validate_values(values, schema)
        expected = http.integer(request, data, "expected_application_revision")
        conflict = AccessError("application_revision_conflict", 409) if expected != application.revision else None
        if ui_action == "add":
            if len(values["players"]) >= 100:
                raise AccessError("invalid_roster_size", 422)
            values["players"].append({"is_self": False})
        else:
            index = ui_action.partition(":")[2]
            if not index.isascii() or not index.isdecimal() or not 0 <= int(index) < len(values["players"]):
                raise AccessError("invalid_request", 400)
            values["players"].pop(int(index))
        return application_page(request, event, application, mode, staff, entered=values, error=conflict,
                                operation_id=data["client_operation_id"], submit_operation_id=data.get("submit_operation_id"))
    try:
        expected = http.integer(request, data, "expected_application_revision")
        if kind in {"draft", "save_submit"}:
            form_id = http.integer(request, data, "form_revision_id")
            submit_id = http.identifier(data["submit_operation_id"]) if kind == "save_submit" else None
            window_id = http.identifier(data["registration_window_id"]) if kind == "save_submit" else None
            result = applications.save_draft(request.session, event_id, application_id, data["client_operation_id"], expected, form_id, values)
            if kind == "save_submit":
                saved_command, _ = result
                values = saved_command.eventhistory.payload["after"]
                result = applications.submit(request.session, event_id, application_id, submit_id,
                    saved_command.result["application_revision"], window_id, form_id)
        else:
            result = applications.edit_application(request.session, event_id, application_id, data["client_operation_id"], expected, values,
                                                   participant=kind == "corrections")
    except AccessError as error:
        if http.as_json(request) or error.status not in (400, 409, 422):
            raise
        event, application, mode, staff = queries.application_access(request.session, event_id, application_id, prefer_staff=kind == "staff")
        return application_page(request, event, application, mode, staff, error=error, entered=values)
    return response(request, *result, staff=kind == "staff")


@private
@require_http_methods(["POST"])
def submit(request, event_id, application_id):
    participant_actor(request.session)
    data = http.command(request, ["expected_application_revision", "registration_window_id", "form_revision_id"])
    result = applications.submit(request.session, event_id, application_id, data["client_operation_id"],
        http.integer(request, data, "expected_application_revision"), http.identifier(data["registration_window_id"]), http.integer(request, data, "form_revision_id"))
    return response(request, *result)


@private
@require_http_methods(["POST"])
def review(request, event_id, application_id):
    workspace_actor(request.session)
    data = http.command(request, ["expected_application_revision", "action"], ["internal_reason", "participant_explanation"])
    action = data["action"]
    if (action == "admit" and ({"internal_reason", "participant_explanation"} & set(data))
            or action == "request_changes" and "internal_reason" in data or action == "reconsider" and "participant_explanation" in data):
        raise AccessError("invalid_request", 400)
    try:
        result = applications.review(request.session, event_id, application_id, data["client_operation_id"],
            http.integer(request, data, "expected_application_revision"), action, internal_reason=data.get("internal_reason"), participant_explanation=data.get("participant_explanation"))
    except AccessError as error:
        if http.as_json(request) or error.status not in (400, 409, 422):
            raise
        event, application, mode, staff = queries.application_access(request.session, event_id, application_id, prefer_staff=True)
        return application_page(request, event, application, mode, staff, error=error)
    return response(request, *result, staff=True)


@private
@require_http_methods(["POST"])
def withdraw(request, event_id, application_id, *, staff=False, revoke=False):
    if staff:
        workspace_actor(request.session)
    else:
        participant_actor(request.session)
    data = http.command(request, ["expected_application_revision"], ["reason"])
    result = applications.withdraw(request.session, event_id, application_id, data["client_operation_id"],
        http.integer(request, data, "expected_application_revision"), by_organiser=staff, revoke=revoke, reason=data.get("reason"))
    return response(request, *result, staff=staff)
