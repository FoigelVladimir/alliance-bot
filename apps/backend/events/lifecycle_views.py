from uuid import uuid4

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from accounts.services import AccessError
from accounts.views import private
from workspace.services import workspace_actor

from . import http, lifecycle, services, views


def preview_page(request, event_id, kind, *, error=None):
    event = services.get_event(event_id)
    if kind == "start":
        preview = lifecycle.start_preview(request.session, event_id)
        field = "start_preview_version"
    else:
        preview = lifecycle.completion_preview(request.session, event_id)
        field = "finish_preview_version" if kind == "finish" else "interruption_preview_version"
        preview[field] = preview["preview_version"]
    if http.as_json(request) and error is None:
        return JsonResponse({"event": {"id": str(event.pk), "title": event.title}, **preview})
    return views.page(request, "lifecycle_preview", {"event": event, "kind": kind, "preview": preview,
        "preview_field": field, "preview_version": preview[field], "operation_id": uuid4(),
        "error_message": views.ERRORS.get(error.code, "Данные изменились. Проверьте новый предпросмотр.") if error else None},
        status=error.status if error else 200)


@private
@require_http_methods(["POST"])
def transition(request, event_id, kind):
    workspace_actor(request.session)
    field = {"start": "start_preview_version", "finish": "finish_preview_version", "interrupt": "interruption_preview_version"}[kind]
    data = http.command(request, ["expected_event_revision", field, *(["reason"] if kind == "interrupt" else [])],
                        ["confirm_pending_applications"] if kind == "start" else [])
    try:
        if kind == "start":
            result = lifecycle.start_event(request.session, event_id, data["client_operation_id"],
                http.integer(request, data, "expected_event_revision"), data[field],
                confirm_pending=http.boolean(request, data, "confirm_pending_applications"))
        else:
            result = lifecycle.finish_event(request.session, event_id, data["client_operation_id"],
                http.integer(request, data, "expected_event_revision"), data[field],
                interrupt=kind == "interrupt", reason=data.get("reason"))
    except AccessError as error:
        if http.as_json(request) or error.status not in (400, 409, 422):
            raise
        return preview_page(request, event_id, kind, error=error)
    return views.command_response(request, *result)


@private
@require_http_methods(["GET"])
def preview(request, event_id, kind):
    workspace_actor(request.session)
    return preview_page(request, event_id, kind)


@private
@require_http_methods(["POST"])
def participation(request, event_id, mark_id):
    workspace_actor(request.session)
    data = http.command(request, ["expected_participation_revision", "mark"], ["correction_note"])
    try:
        result = lifecycle.mark_participation(request.session, event_id, mark_id, data["client_operation_id"],
            http.integer(request, data, "expected_participation_revision"), data["mark"], data.get("correction_note"))
    except AccessError as error:
        if http.as_json(request) or error.status not in (400, 409, 422):
            raise
        return views.detail_page(request, services.get_event(event_id), error=error)
    return views.command_response(request, *result)


def _reporting_status(request, event_id):
    workspace_actor(request.session)
    if request.method == "GET":
        data = request.GET.dict()
        if set(data) - {"action", "exclusion_kind", "primary_event_id"} or any(len(items) != 1 for _, items in request.GET.lists()):
            raise AccessError("invalid_request", 400)
    else:
        data = http.command(request, ["action", "reporting_preview_version", "reason"], ["exclusion_kind", "primary_event_id"])
    action = data.get("action", "exclude")
    primary_id = http.integer(request, data, "primary_event_id") if data.get("primary_event_id") else None
    kind = data.get("exclusion_kind")
    error = None
    if request.method == "POST":
        try:
            result = lifecycle.change_reporting_status(request.session, event_id, data["client_operation_id"], action,
                data["reporting_preview_version"], data["reason"], exclusion_kind=kind, primary_id=primary_id)
        except AccessError as caught:
            if http.as_json(request) or caught.status not in (400, 409, 422):
                raise
            error = caught
        else:
            return views.command_response(request, *result)
    preview = lifecycle.reporting_preview(request.session, event_id, action, kind, primary_id)
    if http.as_json(request):
        return JsonResponse(preview)
    return views.page(request, "reporting_preview", {"event": services.get_event(event_id), "preview": preview,
        "operation_id": uuid4(), "action": action, "exclusion_kind": kind, "primary_id": primary_id,
        "error_message": views.ERRORS.get(error.code, "Проверьте актуальный предпросмотр.") if error else None},
        status=error.status if error else 200)


@private
@require_http_methods(["GET"])
def reporting_preview(request, event_id):
    return _reporting_status(request, event_id)


@private
@require_http_methods(["POST"])
def reporting_status(request, event_id):
    return _reporting_status(request, event_id)
