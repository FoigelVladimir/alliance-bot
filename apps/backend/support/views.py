import json
from uuid import uuid4

from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.csrf import csrf_failure as default_csrf_failure
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.services import AccessError
from accounts.views import json_object, private, text_value
from knowledge.search import hybrid_search

from . import attachments
from .closing import close_ticket
from .delivery import retry_response, send_response
from .models import Draft, InboundRoute, OutgoingAttempt, OutgoingReconciliation, OutgoingResponse, Ticket
from .queries import allowed_communities, assignment_targets, can_edit_draft, draft_data, may_coordinate, open_ticket, queue_tickets, ticket_data, ticket_history
from .reconciliation import may_reconcile, request_reconciliation
from .services import change_assignment, review_inbound, save_draft, support_actor, take_ticket

MAX_ID = 2**63 - 1


def csrf_failure(request, reason=""):
    if request.path_info.startswith("/support/"):
        response = JsonResponse({"error": {
            "code": "csrf_failed", "message": "Обновите страницу и повторите действие.", "field_errors": {},
        }}, status=403)
        response["Cache-Control"] = "no-store"
        return response
    return default_csrf_failure(request, reason=reason)


def render_support(request, template, context):
    response = render(request, f"support/{template}.html", context)
    response["Referrer-Policy"] = "strict-origin"
    return response


def command_data(request, fields, optional=()):
    if request.content_type != "application/json":
        raise AccessError("invalid_request", 400)
    return json_object(request, ["client_operation_id", *fields], optional)


def operation(data, action, replayed, changed=True):
    return {
        "client_operation_id": str(data["client_operation_id"]),
        "action": action, "replayed": replayed, "changed": changed,
    }


def check_path_id(value):
    if not 0 < value <= MAX_ID:
        raise AccessError("not_found", 404)


@private
@require_safe
def queue(request):
    actor = support_actor(request.session)
    return render_support(request, "queue", queue_context(request, actor))


@private
@require_safe
def review_detail(request, route_id):
    from .ai_join import latest_state

    actor = support_actor(request.session)
    check_path_id(route_id)
    route = InboundRoute.objects.filter(
        pk=route_id, dialog__cursor_id__in=allowed_communities(actor), status="needs_review",
    ).select_related("message", "dialog").first()
    if route is None:
        raise AccessError("not_found", 404)
    return render_support(request, "ticket", {
        "route": route, "current_ticket": open_ticket(route.dialog_id),
        "route_operation_id": uuid4(), "dismiss_operation_id": uuid4(),
        "join": latest_state(actor, route.dialog_id), "join_operation_id": uuid4(),
    })


@private
@require_safe
def ticket_detail(request, ticket_id):
    actor = support_actor(request.session)
    check_path_id(ticket_id)
    ticket = read_ticket(actor, ticket_id)
    own_draft = Draft.objects.filter(ticket=ticket, author=actor).first()
    return render_support(request, "ticket", {
        **header_context(actor, ticket), "own_draft": own_draft,
        **ticket_history(ticket, request.GET.get("before")),
        **delivery_context(actor, ticket),
        "handoff_plans": ticket.handoff_plans.order_by("created_at", "pk")[:1],
    })


@private
@require_POST
def resolve_review(request, route_id, action):
    check_path_id(route_id)
    expected_ticket = None
    reason = ""
    if action == "route_to_support":
        data = command_data(request, ["expected_ticket_id"])
        value = data["expected_ticket_id"]
        if value is not None:
            if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 19:
                raise AccessError("invalid_request", 400)
            expected_ticket = int(value)
            if not 0 < expected_ticket <= MAX_ID:
                raise AccessError("invalid_request", 400)
    else:
        data = command_data(request, ["reason"])
        reason = text_value(data, "reason").strip()
        if not reason:
            raise AccessError("invalid_request", 400)
    decision, replayed = review_inbound(
        request.session, route_id, data["client_operation_id"], action, expected_ticket_id=expected_ticket, reason=reason,
    )
    return JsonResponse({
        "operation": operation(data, action, replayed),
        "ticket": ticket_data(decision.ticket) if decision.ticket_id else None,
    })


@private
@require_POST
def take(request, ticket_id):
    check_path_id(ticket_id)
    data = command_data(request, ["expected_assignment_version"])
    version = assignment_version(data)
    ticket, changed, replayed = take_ticket(request.session, ticket_id, data["client_operation_id"], version)
    return JsonResponse({"operation": operation(data, "take", replayed, changed), "ticket": ticket_data(ticket)})



def assignment_version(data):
    version = data["expected_assignment_version"]
    if type(version) is not int or not 0 < version <= MAX_ID:
        raise AccessError("invalid_request", 400)
    return version


def decimal_value(value, *, minimum=1):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise AccessError("invalid_request", 400)
    number = int(value)
    if not minimum <= number <= MAX_ID:
        raise AccessError("invalid_request", 400)
    return number


def delivery_data(attempt):
    response = attempt.response
    return {
        "response": {
            "id": str(response.pk), "ticket_id": str(response.ticket_id), "status": response.status,
            "assignment_version": response.assignment_version,
            "message_id": str(response.message_id) if response.message_id else None,
            "recovery_verification_required": response.recovery_verification_required,
        },
        "attempt": {
            "id": str(attempt.pk), "type": attempt.attempt_type, "status": attempt.status,
            "started_at": attempt.started_at, "finished_at": attempt.finished_at,
        },
    }


def delivery_result(data, action, attempt, replayed):
    result = delivery_data(attempt)
    status = 202 if result["response"]["status"] in ("pending", "unknown_outcome") else 200
    return JsonResponse({"operation": operation(data, action, replayed), **result}, status=status)


@private
@require_POST
def send(request, ticket_id):
    check_path_id(ticket_id)
    data = command_data(request, ["expected_assignment_version", "text", "source_draft"])
    text = data["text"]
    try:
        valid = isinstance(text, str) and text.strip() and len(text) <= 9000 and "\0" not in text
        if valid:
            text.encode("utf-8")
    except UnicodeError:
        valid = False
    if not valid:
        return JsonResponse({"error": {
            "code": "invalid_text", "message": "Введите текст ответа до 9000 символов.",
            "field_errors": {"text": ["Введите текст ответа до 9000 символов."]},
        }}, status=422)
    draft = data["source_draft"]
    if draft is not None:
        if not isinstance(draft, dict) or draft.keys() != {"id", "revision"}:
            raise AccessError("invalid_request", 400)
        draft = (decimal_value(draft["id"]), decimal_value(draft["revision"]))
    attempt, replayed = send_response(
        request.session, ticket_id, data["client_operation_id"], assignment_version(data), text, draft,
    )
    return delivery_result(data, "send", attempt, replayed)


@private
@require_POST
def retry(request, response_id):
    check_path_id(response_id)
    data = command_data(request, ["expected_assignment_version"])
    attempt, replayed = retry_response(
        request.session, response_id, data["client_operation_id"], assignment_version(data),
    )
    return delivery_result(data, "manual_retry", attempt, replayed)


@private
@require_safe
def response_state(request, response_id):
    check_path_id(response_id)
    actor = support_actor(request.session)
    attempt = OutgoingAttempt.objects.filter(
        response_id=response_id, response__ticket__dialog__cursor_id__in=allowed_communities(actor),
    ).select_related("response__ticket").order_by("-pk").first()
    if attempt is None:
        raise AccessError("response_not_found", 404)
    response = attempt.response
    ticket = response.ticket
    check = response.reconciliations.order_by("-pk").first()
    state = delivery_data(attempt)
    reasons = {
        OutgoingResponse.Status.UNKNOWN: "Результат отправки пока не подтверждён.",
        OutgoingResponse.Status.FAILED: "Ответ не отправлен.",
        OutgoingResponse.Status.CANCELLED: "Отправка отменена при смене назначения.",
    }
    state["response"]["failure_reason"] = (
        "После восстановления требуется сверка отправки."
        if response.recovery_verification_required else reasons.get(response.status)
    )
    return JsonResponse({
        "response": state["response"], "last_attempt": state["attempt"],
        "last_reconciliation": reconciliation_data(check) if check else None,
        "can_retry": (
            can_edit_draft(ticket, actor) and ticket.assignment_version == response.assignment_version
            and response.status == OutgoingResponse.Status.FAILED and attempt.status == OutgoingAttempt.Status.FAILED
            and not response.recovery_verification_required
        ),
    })


def reconciliation_data(check):
    return {"id": str(check.pk), "response_id": str(check.response_id),
            "status": check.status, "outcome": check.outcome or None}


@private
@require_POST
def reconcile_ai_part(request, part_id):
    from .ai_reconciliation import check_data, request_reconciliation

    check_path_id(part_id)
    data = command_data(request, [])
    check, replayed = request_reconciliation(request.session, part_id, data["client_operation_id"])
    return JsonResponse({"operation": operation(data, "reconcile_ai_part", replayed),
                         "reconciliation": check_data(check)}, status=200 if check.finished_at else 202)


@private
@require_safe
def ai_part_state(request, part_id):
    from .ai_reconciliation import delivery_state, visible_delivery

    actor = support_actor(request.session)
    check_path_id(part_id)
    return JsonResponse(delivery_state(actor, visible_delivery(actor, part_id)))


@private
@require_POST
def reconcile(request, response_id):
    check_path_id(response_id)
    data = command_data(request, [])
    check, replayed = request_reconciliation(request.session, response_id, data["client_operation_id"])
    return JsonResponse({
        "operation": operation(data, "reconcile_response", replayed),
        "reconciliation": reconciliation_data(check),
    }, status=200 if check.finished_at else 202)



@private
@require_http_methods(["GET", "HEAD", "POST"])
def draft(request, ticket_id):
    check_path_id(ticket_id)
    if request.method == "POST":
        try:
            if request.content_type != "application/json":
                raise ValueError
            data = json.loads(request.body)
            if not isinstance(data, dict) or data.keys() != {"expected_assignment_version", "expected_draft_revision", "text"}:
                raise ValueError
            text = data["text"]
            if not isinstance(text, str) or "\0" in text:
                raise ValueError
            text.encode("utf-8")
        except ValueError:
            raise AccessError("invalid_request", 400) from None
        version = assignment_version(data)
        revision = decimal_value(data["expected_draft_revision"], minimum=0)
        if len(text) > 20000:
            return JsonResponse({"error": {
                "code": "draft_too_long", "message": "Сократите черновик до 20 000 символов.",
                "field_errors": {"text": ["Сократите черновик до 20 000 символов."]},
            }}, status=422)
        return JsonResponse({"draft": draft_data(save_draft(request.session, ticket_id, version, revision, text))})

    if request.GET:
        raise AccessError("invalid_request", 400)
    actor = support_actor(request.session)
    ticket = Ticket.objects.filter(pk=ticket_id, dialog__cursor_id__in=allowed_communities(actor)).first()
    if ticket is None:
        raise AccessError("not_found", 404)
    own = Draft.objects.filter(ticket=ticket, author=actor).first()
    return JsonResponse({
        "draft": draft_data(own), "draft_revision": str(own.revision if own else 0),
        "editable": can_edit_draft(ticket, actor), "assignment_version": ticket.assignment_version,
    })



@private
@require_POST
def reassign(request, ticket_id, action):
    check_path_id(ticket_id)
    transferring = action == "reassign_coordinator"
    fields = ["expected_assignment_version", "target_owner_id"] if transferring else ["expected_assignment_version"]
    data = command_data(request, fields)
    target_id = decimal_value(data["target_owner_id"]) if transferring else None
    ticket, replayed = change_assignment(
        request.session, ticket_id, data["client_operation_id"], assignment_version(data), action, target_id,
    )
    return JsonResponse({"operation": operation(data, action, replayed), "ticket": ticket_data(ticket)})



@private
@require_POST
def close(request, ticket_id):
    check_path_id(ticket_id)
    data = command_data(request, ["expected_assignment_version", "confirm_warning", "warning_token"])
    confirm, token = data["confirm_warning"], data["warning_token"]
    if type(confirm) is not bool or (not confirm and token is not None) or (
        token is not None and (not isinstance(token, str) or len(token) > 4096)
    ):
        raise AccessError("invalid_request", 400)
    try:
        result, replayed = close_ticket(
            request.session, ticket_id, data["client_operation_id"], assignment_version(data), confirm, token,
        )
    except AccessError as error:
        messages = {
            "close_warning_required": "Перед закрытием проверь, что осталось в обращении.",
            "close_warning_stale": "Обращение изменилось после предупреждения. Проверь его ещё раз.",
            "warning_token_invalid": "Подтверждение не подходит. Проверь предупреждение ещё раз.",
        }
        if error.code not in messages:
            raise
        return JsonResponse({
            "error": {"code": error.code, "message": messages[error.code], "field_errors": {}},
            **error.details,
        }, status=409)
    return JsonResponse({"operation": operation(data, "close", replayed), "ticket": result})



@private
@require_safe
def reassign_targets(request, ticket_id):
    check_path_id(ticket_id)
    actor = support_actor(request.session)
    ticket = Ticket.objects.filter(
        pk=ticket_id, dialog__cursor_id__in=allowed_communities(actor),
    ).select_related("dialog").first()
    if ticket is None:
        raise AccessError("ticket_not_found", 404)
    if not may_coordinate(actor, ticket.dialog.cursor_id):
        raise AccessError("permission_denied", 403)
    candidates = assignment_targets(ticket, actor) if ticket.status == Ticket.Status.OPEN and ticket.current_owner_id else ()
    return JsonResponse({"targets": [
        {"id": str(member.profile_id), "display_name": member.profile.display_name} for member in candidates
    ]})



def read_ticket(actor, ticket_id):
    ticket = Ticket.objects.filter(
        pk=ticket_id, dialog__cursor_id__in=allowed_communities(actor),
    ).select_related("current_owner", "dialog").first()
    if ticket is None:
        raise AccessError("ticket_not_found", 404)
    return ticket


def ticket_permissions(actor, ticket):
    is_open = ticket.status == Ticket.Status.OPEN
    owner = ticket.current_owner_id
    can_edit = can_edit_draft(ticket, actor)
    coordinates = is_open and owner is not None and may_coordinate(actor, ticket.dialog.cursor_id)
    return {
        "can_take": is_open and owner is None,
        "can_takeover": is_open and owner not in (None, actor.pk),
        "can_reassign": bool(coordinates),
        "can_return": can_edit or bool(coordinates),
        "can_send": can_edit, "can_close": can_edit, "can_edit_draft": can_edit,
    }


@private
@require_safe
def ticket_state(request, ticket_id):
    from .ai_reconciliation import delivery_state
    from .models import AIPartDelivery

    check_path_id(ticket_id)
    actor = support_actor(request.session)
    ticket = read_ticket(actor, ticket_id)
    data = ticket_data(ticket)
    if ticket.status == Ticket.Status.CLOSED:
        data.update({
            "closed_at": ticket.closed_at, "closed_by_id": str(ticket.closed_by_id) if ticket.closed_by_id else None,
            "closed_reason": ticket.closed_reason, "closed_warning_ack": ticket.closed_warning_ack,
        })
    permissions = ticket_permissions(actor, ticket)
    return JsonResponse({
        "ticket": data, "permissions": permissions, "observed_at": timezone.now(),
        "bot_deliveries": [delivery_state(actor, delivery) for delivery in AIPartDelivery.objects.filter(
            part__answer__aiprocessingrun__handoff_ticket=ticket,
        ).select_related("part").order_by("pk")],
        "state_key": ticket_state_key(ticket, permissions),
    })


@private
@require_safe
def history_fragment(request, ticket_id):
    check_path_id(ticket_id)
    actor = support_actor(request.session)
    ticket = read_ticket(actor, ticket_id)
    return render_support(request, "fragments", {
        "ticket": ticket, **ticket_history(ticket, request.GET.get("before")), **delivery_context(actor, ticket),
    })



def queue_context(request, actor):
    view = request.GET.get("view", "queue")
    if view not in {"queue", "mine", "open", "closed"}:
        view = "queue"
    query = request.GET.get("q", "")[:200].replace("\0", "")
    try:
        page = max(1, int(request.GET.get("page", "1")))
    except ValueError:
        page = 1
    routes = InboundRoute.objects.filter(
        dialog__cursor_id__in=allowed_communities(actor), status="needs_review",
    ).select_related("message", "dialog").order_by("message__created_at", "id")
    return {
        "view": view, "query": query,
        "tickets": Paginator(queue_tickets(actor, view, query), 50).get_page(page),
        "reviews": Paginator(routes, 30).get_page(request.GET.get("review_page")),
    }


@private
@require_safe
def queue_fragment(request):
    actor = support_actor(request.session)
    return render_support(request, "fragments", {"fragment": "queue", **queue_context(request, actor)})


def header_context(actor, ticket):
    permissions = ticket_permissions(actor, ticket)
    return {
        "ticket": ticket, **permissions, "owned_by_actor": ticket.current_owner_id == actor.pk,
        "state_key": ticket_state_key(ticket, permissions), "send_operation_id": uuid4(),
        "operation_id": uuid4(), "takeover_id": uuid4(), "return_id": uuid4(), "close_id": uuid4(), "reassign_id": uuid4(),
        "reassign_targets": assignment_targets(ticket, actor) if permissions["can_reassign"] else (),
    }


@private
@require_safe
def header_fragment(request, ticket_id):
    check_path_id(ticket_id)
    actor = support_actor(request.session)
    return render_support(request, "fragments", {"fragment": "header", **header_context(actor, read_ticket(actor, ticket_id))})



def ticket_state_key(ticket, permissions):
    return json.dumps([
        ticket.pk, ticket.status, ticket.current_owner_id, ticket.assignment_version,
        ticket.importance, *permissions.values(),
    ], separators=(",", ":"))


def delivery_context(actor, ticket):
    deliveries = OutgoingResponse.objects.filter(ticket=ticket).exclude(
        status=OutgoingResponse.Status.SENT,
    ).prefetch_related(Prefetch(
        "reconciliations", queryset=OutgoingReconciliation.objects.order_by("-pk")[:1], to_attr="latest_check",
    ))
    return {
        "deliveries": [
            (response, uuid4(), next(iter(response.latest_check), None))
            for response in deliveries.order_by("-pk")[:20]
        ],
        "can_reconcile": may_reconcile(actor, ticket),
        "can_send": can_edit_draft(ticket, actor),
    }


@private
@require_POST
def prepare_attachment(request, attachment_id):
    check_path_id(attachment_id)
    data = command_data(request, [])
    preparation, state, replayed = attachments.prepare(request.session, attachment_id, data["client_operation_id"])
    return JsonResponse({
        "operation": operation(data, "prepare_attachment", replayed, not replayed),
        "attachment": state,
        "preparation": {
            "id": str(preparation.pk), "state": preparation.state, "reason": preparation.reason_code,
            "created_at": preparation.created_at, "finished_at": preparation.finished_at,
        },
    }, status=202 if preparation.state == "active" else 200)


@private
@require_safe
def attachment_state(request, attachment_id):
    check_path_id(attachment_id)
    return JsonResponse(attachments.read_state(request.session, attachment_id))


@private
@require_safe
def attachment_content(request, attachment_id, download=False):
    check_path_id(attachment_id)
    response = attachments.attachment_bytes(
        request.session, attachment_id, head=request.method == "HEAD", download=download,
        range_header=request.headers.get("Range") if "If-Range" not in request.headers else None,
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@private
@require_safe
def knowledge_search(request, ticket_id):
    actor = support_actor(request.session)
    check_path_id(ticket_id)
    read_ticket(actor, ticket_id)
    question = request.GET.get("q", "").strip()
    if len(question) > 500 or "\0" in question:
        raise AccessError("invalid_request", 400)
    try:
        hits = hybrid_search(question)
    except (ImportError, OSError, ValueError):
        raise AccessError("knowledge_search_unavailable", 503) from None
    return JsonResponse({"results": [
        {"title": hit.heading, "content": hit.content,
         "source_url": reverse("knowledge-draft", args=[hit.material_id, hit.version_id])}
        for hit in hits
    ]})
