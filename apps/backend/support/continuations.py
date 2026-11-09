"""Continuation of a known closed human ticket; callers hold the cursor and dialog locks."""

import re

from accounts.policies import quarantine_incident
from vk_channel.models import StudentAction

from .models import AIRequest, InboundRoute, Message, Ticket

UNRESOLVED_PHRASES = {
    "вопрос не решен", "мой вопрос не решен", "вопрос остался нерешенным",
    "проблема не решена", "моя проблема не решена",
    "я так и не понял, куда обращаться", "я так и не понял куда обращаться",
}
ACK_PREFIX = re.compile(r"^(?:спасибо(?: большое)?|большое спасибо|понятно)\s*,?\s*но\s+")


def is_unresolved_text(phrase):
    return ACK_PREFIX.sub("", phrase.replace("ё", "е")) in UNRESOLVED_PHRASES


def activate_continuations(ticket, dialog, now):
    actions = list(StudentAction.objects.select_for_update().filter(
        ticket=ticket, action="continue", awaiting_close=True, mode_version=dialog.mode_version,
        expires_at__gt=now,
    ).order_by("pk"))
    for action in actions:
        action.awaiting_close, action.expected_closed_at = False, ticket.closed_at
        action.save(update_fields=["awaiting_close", "expected_closed_at"])
    return len(actions)


def current_closed_origin(cursor, dialog, parent, *, before_sequence=None):
    if (quarantine_incident() or cursor.automation_paused or dialog.mode != "ai_auto"
            or parent.dialog_id != dialog.pk or parent.status != Ticket.Status.CLOSED):
        return False
    latest = Ticket.objects.filter(dialog=dialog).order_by("-pk").first()
    if latest is None or latest.pk != parent.pk:
        return False
    questions = AIRequest.objects.filter(dialog=dialog, mode_version=dialog.mode_version)
    routes = InboundRoute.objects.filter(
        dialog=dialog, mode_version=dialog.mode_version, historical=False,
        status__in=("pending", "ai_processing", "needs_review"), message__ticket__isnull=True,
    )
    if before_sequence is not None:
        questions = questions.filter(message_links__message__inboundroute__sequence__lt=before_sequence)
        routes = routes.filter(sequence__lt=before_sequence)
    return not questions.exists() and not routes.exists()


def open_continuation(cursor, dialog, parent, now, *, before_sequence=None):
    from .handoff_plans import finish_explicit_handoff

    parent = Ticket.objects.select_for_update().get(pk=parent.pk)
    if parent.dialog_id != dialog.pk or parent.status != Ticket.Status.CLOSED:
        return None
    latest = Ticket.objects.select_for_update().filter(dialog=dialog).order_by("-pk").first()
    if latest and latest.status == Ticket.Status.OPEN and latest.parent_ticket_id == parent.pk:
        return latest
    if not current_closed_origin(cursor, dialog, parent, before_sequence=before_sequence):
        return None
    following_requests = ()
    if before_sequence is not None:
        following_requests = list(AIRequest.objects.filter(
            dialog=dialog, mode_version=dialog.mode_version, resolved_at__isnull=True,
            message_links__message__inboundroute__sequence__gte=before_sequence,
        ).values_list("pk", flat=True).distinct())
    child = Ticket.objects.create(dialog=dialog, parent_ticket=parent, handoff_reason="student_unresolved", created_at=now)
    finish_explicit_handoff(cursor, dialog, child, planned_request_ids=following_requests, force=True)
    return child


def route_unresolved_text(cursor, dialog, route, message, phrase):
    from storage.services import database_time

    if not is_unresolved_text(phrase):
        return False
    if route.reply_to_cmid is not None:
        original = Message.objects.filter(
            dialog=dialog, conversation_message_id=route.reply_to_cmid, ticket__status=Ticket.Status.CLOSED,
        ).select_related("ticket").first()
        parent = original.ticket if original else None
    else:
        parent = Ticket.objects.filter(dialog=dialog).order_by("-pk").first()
    child = open_continuation(cursor, dialog, parent, database_time(), before_sequence=route.sequence) if parent else None
    if child is None:
        route.status, route.reason = "needs_review", "manual_support"
    else:
        message.ticket_id = child.pk
        route.status, route.reason = "attached", "student_unresolved"
    return True
