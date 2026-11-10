from uuid import uuid4

from django.http import HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from accounts.services import AccessError
from accounts.views import private
from workspace.services import workspace_actor

from . import deletion, http, views
from .models import Event

MESSAGES = {
    "deletion_journal_unavailable": "Журнал удалений пока недоступен или не настроен. Подтверждённое удаление возможно только после его записи.",
    "event_deletion_preview_changed": "Заявки или отправки изменились. Проверьте новый предпросмотр.",
    "deletion_notification_choice_required": "Сообщения уже отправлялись или их исход неизвестен. Выберите уведомление об отмене либо подтвердите другой способ связи.",
}


def preview_page(request, event_id, *, error=None):
    preview = deletion.deletion_preview(request.session, event_id)
    if http.as_json(request) and error is None:
        return JsonResponse(preview)
    return views.page(request, "deletion_preview", {"event_id": event_id, "preview": preview, "operation_id": uuid4(),
        "error_message": MESSAGES.get(error.code, views.ERRORS.get(error.code, "Проверьте актуальное состояние мероприятия.")) if error else None},
        status=error.status if error else 200)


@private
@require_http_methods(["GET"])
def preview(request, event_id):
    workspace_actor(request.session)
    return preview_page(request, event_id)


@private
@require_http_methods(["POST"])
def delete(request, event_id):
    workspace_actor(request.session)
    data = http.command(request, ["event_uuid", "expected_event_revision", "deletion_preview_version", "notice_mode"])
    try:
        intent, replayed = deletion.request_deletion(request.session, event_id, http.identifier(data["event_uuid"]),
            data["client_operation_id"], http.integer(request, data, "expected_event_revision"),
            data["deletion_preview_version"], data["notice_mode"])
    except AccessError as error:
        if http.as_json(request) or error.status not in (400, 409, 422, 503):
            raise
        return preview_page(request, event_id, error=error)
    url = reverse("event-deletion-status", args=[intent.pk])
    if http.as_json(request):
        return JsonResponse({"operation": {"client_operation_id": str(data["client_operation_id"]), "replayed": replayed},
            "deletion": deletion.intent_data(intent), "status_url": url}, status=202 if intent.status == "awaiting_ledger" else 200)
    return HttpResponseRedirect(url, status=303)


@private
@require_http_methods(["GET"])
def status(request, deletion_id):
    intent = deletion.read_status(request.session, deletion_id)
    data = deletion.intent_data(intent)
    if http.as_json(request):
        return JsonResponse({"deletion": data})
    return views.page(request, "deletion_status", {"deletion": data})
