from uuid import uuid4

from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from accounts.services import AccessError
from accounts.views import private
from events import http
from events.models import Application, Event
from participants.services import actor as participant_actor

from . import preferences


def next_url(session):
    target = session.get("participant_after_preferences", {})
    identifier = target.get("target_id") if isinstance(target, dict) else None
    if isinstance(target, dict) and target.get("purpose") == "event" and type(identifier) is int and 0 < identifier < 2**63:
        return reverse("event-participant-entry", args=[identifier])
    return reverse("participant-requests")


def parse_form(request):
    data = request.POST.dict()
    data.pop("csrfmiddlewaretoken", None)
    required = {"client_operation_id", "expected_revision"}
    if not required <= set(data) or any(len(items) != 1 for _, items in request.POST.lists()):
        raise AccessError("invalid_request", 400)
    choices = []
    for key, value in data.items():
        if key in required | {"all_disabled"}:
            continue
        parts = key.split(".")
        if (len(parts) != 4 or parts[0] != "choice" or parts[1] not in {"club", "event", "section"}
                or not parts[2].isascii() or not parts[2].isdecimal() or len(parts[2]) > 19
                or parts[3] not in preferences.CATEGORIES or value not in {"", "allow", "deny"}):
            raise AccessError("invalid_request", 400)
        if value:
            choices.append({"scope_kind": parts[1], "subject_id": int(parts[2]), "category": parts[3], "enabled": value == "allow"})
    return {"client_operation_id": http.identifier(data["client_operation_id"]),
            "expected_revision": http.integer(request, data, "expected_revision"),
            "all_disabled": http.boolean(request, data, "all_disabled"), "choices": preferences.validate_choices(choices)}


def scopes_for(person, choices):
    scopes = {("club", 0): "Весь клуб"}
    event_ids = set(Application.objects.filter(owner=person, event__deleted_at__isnull=True,
        event__deletion_pending=False, event__restoration_verification_required=False).values_list("event_id", flat=True))
    event_ids.update(choice["subject_id"] for choice in choices if choice["scope_kind"] == "event")
    names = dict(Event.objects.filter(pk__in=event_ids, deleted_at__isnull=True, deletion_pending=False,
        restoration_verification_required=False).values_list("pk", "title"))
    for identifier in sorted(event_ids):
        scopes[("event", identifier)] = names.get(identifier, "Мероприятие недоступно")
    for choice in choices:
        if choice["scope_kind"] == "section":
            scopes[("section", choice["subject_id"])] = "Секция №" + str(choice["subject_id"])
    selected = {(choice["scope_kind"], choice["subject_id"], choice["category"]): choice["enabled"] for choice in choices}
    return [{"kind": kind, "id": identifier, "name": name,
        "categories": [{"key": category, "name": label,
                        "value": "allow" if selected.get((kind, identifier, category)) is True else
                                 "deny" if selected.get((kind, identifier, category)) is False else ""}
                       for category, label in preferences.CATEGORIES.items()]}
        for (kind, identifier), name in scopes.items()]


@private
@require_http_methods(["GET", "POST"])
def settings(request):
    person = participant_actor(request.session)
    error = None
    entered = None
    if request.method == "POST":
        try:
            data = http.command(request, ["expected_revision", "all_disabled", "choices"]) if request.content_type == "application/json" else parse_form(request)
            event, replayed = preferences.save_preferences(request.session, data["client_operation_id"],
                data["expected_revision"], data["all_disabled"], data["choices"])
        except AccessError as caught:
            if http.as_json(request) or caught.status not in (400, 409, 422):
                raise
            error = caught
            entered = data if "data" in locals() else None
        else:
            if http.as_json(request):
                return JsonResponse({"operation": {"replayed": replayed, "client_operation_id": str(data["client_operation_id"])},
                    "settings": preferences.settings_data(preferences.current_settings(person))})
            return HttpResponseRedirect(next_url(request.session), status=303)
    current = preferences.settings_data(preferences.current_settings(person))
    if http.as_json(request):
        return JsonResponse({"settings": current, "categories": preferences.CATEGORIES})
    values = entered or current
    response = render(request, "notifications/preferences.html", {"settings": current, "values": values,
        "scopes": scopes_for(person, values["choices"]), "current_scopes": scopes_for(person, current["choices"]), "initial": not current["onboarding_completed"],
        "operation_id": uuid4(), "skip_operation_id": uuid4(),
        "error_message": "Настройки изменились. Сравните текущий выбор с вашим и сохраните заново." if error and error.code == "notification_preferences_conflict"
                         else "Проверьте настройки и повторите сохранение." if error else None}, status=error.status if error else 200)
    response["Referrer-Policy"] = "strict-origin"
    return response


@private
@require_http_methods(["POST"])
def skip(request):
    participant_actor(request.session)
    data = http.command(request, ["expected_revision"])
    revision = http.integer(request, data, "expected_revision")
    event, replayed = preferences.skip_initial_choice(request.session, data["client_operation_id"], revision)
    if http.as_json(request):
        return JsonResponse({"replayed": replayed, "settings": event.after})
    return HttpResponseRedirect(next_url(request.session), status=303)
