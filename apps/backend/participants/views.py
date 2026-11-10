from uuid import UUID

from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from django.utils.cache import add_never_cache_headers

from accounts.policies import profile_from_session
from accounts.services import AccessError
from accounts.views import private

from . import services
from .models import BindingAttempt


def page(request, template, context, *, status=200):
    response = render(request, "participants/" + template + ".html", context, status=status)
    response["Referrer-Policy"] = "no-referrer"
    add_never_cache_headers(response)
    return response


@private
@require_http_methods(["GET"])
def open_link(request):
    link = services.link_from_key(request.GET.get("key", ""))
    if not services.link_valid(link, link.account, link.account.cursor):
        return page(request, "entry", {"state": "expired"}, status=410)
    request.session["participant_launch"] = str(link.pk)
    request.session["participant_return"] = True
    return HttpResponseRedirect(reverse("participant-entry"), status=303)


def destination(link):
    # The entry target is entirely server-owned; it never accepts a return URL.
    if link.purpose == "event":
        return reverse("event-participant-entry", args=[link.target_id])
    if link.purpose == "notifications":
        return reverse("participant-notifications")
    return reverse("participant-requests")


def activated_destination(person, link):
    from notifications.preferences import current_settings
    if current_settings(person).onboarding_completed_at is None:
        return reverse("participant-notifications") + "?initial=1"
    return destination(link)


@private
@require_http_methods(["GET", "POST"])
def entry(request):
    if not services.enabled():
        return page(request, "entry", {"state": "unavailable"}, status=503)
    try:
        person, link = services.activate(request.session)
    except AccessError as error:
        if error.code == "authentication_required":
            state = "login"
        elif error.code == "participant_confirmation_required":
            state = "confirm"
        else:
            return page(request, "entry", {"state": "expired"}, status=410)
    else:
        request.session.pop("participant_return", None)
        return HttpResponseRedirect(activated_destination(person, link), status=303)
    attempt = None
    message = None
    if state == "confirm" and request.method == "POST":
        try:
            if set(request.POST) - {"csrfmiddlewaretoken"}:
                raise AccessError("invalid_request", 400)
            attempt = services.start_binding(request.session)
            state = "waiting"
        except AccessError as error:
            state = "conflict" if error.code == "participant_binding_conflict" else "confirm"
            message = {
                "participant_attempt_limit": "Слишком много попыток. Подождите час и получите новую ссылку в боте.",
                "participant_link_unavailable": "Ссылка истекла. Получите новую в боте.",
            }.get(error.code, "Не удалось начать подтверждение. Проверьте аккаунт и получите новую ссылку в боте.")
    elif state == "confirm":
        link = services.selected_link(request.session)
        attempt = BindingAttempt.objects.filter(link=link, session_key=request.session.session_key,
                                                 status="pending", expires_at__gt=services.database_now()).order_by("-created_at").first()
        if attempt:
            state = "waiting"
    return page(request, "entry", {"state": state, "attempt": attempt, "message": message,
                                   "profile": profile_from_session(request.session)})


@private
@require_http_methods(["GET"])
def entry_status(request):
    try:
        person, link = services.activate(request.session)
    except AccessError as error:
        status = "waiting" if error.code == "participant_confirmation_required" else "expired"
        return JsonResponse({"status": status})
    return JsonResponse({"status": "confirmed", "next_url": activated_destination(person, link)})


@private
@require_http_methods(["GET"])
def my_requests(request):
    person = services.actor(request.session)
    from events.queries import participant_requests
    return page(request, "requests", {"person": person, "registrations": participant_requests(person), "enrollments": []})
