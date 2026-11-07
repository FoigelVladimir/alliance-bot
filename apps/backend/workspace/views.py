from django.core.paginator import Paginator
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from accounts import views as account_views
from accounts.policies import has_work_access, profile_from_session, quarantine_incident
from accounts.services import AccessError
from accounts.views import private

from . import queries
from .models import Notification
from .services import notification_data, read_notification, workspace_actor


def render_workspace(request, template, context):
    response = render(request, f"workspace/{template}.html", context)
    response["Referrer-Policy"] = "strict-origin"
    return response


@private
@require_GET
def home(request):
    actor = profile_from_session(request.session)
    if not has_work_access(actor) or quarantine_incident():
        return account_views.home(request)
    return render_workspace(request, "home", {
        "actor": actor, "attention": queries.home_section(actor, "attention"),
        "summary": queries.home_section(actor, "summary"),
    })


@private
@require_GET
def home_fragment(request, section):
    actor = workspace_actor(request.session)
    if section not in queries.SECTIONS:
        raise AccessError("not_found", 404)
    return render_workspace(request, section, queries.home_section(actor, section))


@private
@require_GET
def notifications(request):
    actor = workspace_actor(request.session)
    values = request.GET.getlist("page")
    value = values[0] if len(values) == 1 else "1" if not values else ""
    if not value.isascii() or not value.isdecimal() or len(value) > 9 or int(value) < 1:
        raise AccessError("invalid_request", 400)
    rows = Notification.objects.filter(recipient=actor).select_related("source_event").order_by("-created_at", "-pk")
    paginator = Paginator(rows, 30)
    page_number = int(value)
    if page_number > paginator.num_pages:
        raise AccessError("not_found", 404)
    page = paginator.page(page_number)
    unread = Notification.objects.filter(recipient=actor, read_at__isnull=True).count()
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"notifications": [notification_data(item) for item in page],
                             "unread_count": unread, "page": page.number, "total_count": paginator.count,
                             "next_page": page.next_page_number() if page.has_next() else None})
    return render_workspace(request, "notifications", {"notifications": page, "unread_count": unread})


@private
@require_POST
def read(request, notification_id):
    if not 0 < notification_id < 2**63:
        raise AccessError("not_found", 404)
    item, changed = read_notification(request.session, notification_id)
    data = notification_data(item)
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"notification": data, "changed": changed})
    response = HttpResponseRedirect(data["target_url"])
    response.status_code = 303
    return response
