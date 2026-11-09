from django.db.models import Q

from .ai_processing import handoff_active_requests
from .models import InboundRoute, Message, Ticket

MANAGER_REQUESTS = {
    "позвать менеджера",
    "позовите менеджера",
    "позовите, пожалуйста, менеджера",
    "позовите менеджера, пожалуйста",
    "можно поговорить с менеджером",
    "хочу поговорить с менеджером",
    "нужен менеджер",
}
CLOSING_ACKNOWLEDGEMENTS = {"спасибо", "большое спасибо", "спасибо большое", "понятно", "всё понятно"}


def plain_phrase(text):
    return " ".join(text.casefold().split()).rstrip(".!?…").rstrip()


def route_live_message(cursor, dialog, route, message):
    """Apply the narrow text rules while the cursor, dialog and route are locked."""
    if (route.automation_epoch != cursor.automation_epoch or cursor.automation_paused
            or message.sender_vk_id != dialog.vk_peer_id or message.direction != "inbound"):
        return False
    phrase = plain_phrase(message.text)
    if route.mode_version != dialog.mode_version or dialog.mode != "ai_auto":
        return False
    from .continuations import route_unresolved_text

    if route_unresolved_text(cursor, dialog, route, message, phrase):
        return True
    if (phrase in CLOSING_ACKNOWLEDGEMENTS and not message.attachments.exists()
            and Ticket.objects.filter(dialog=dialog, status=Ticket.Status.CLOSED).exists()):
        route.status, route.reason = "ignored", "closing_acknowledgement"
        return True
    if phrase not in MANAGER_REQUESTS:
        return False
    ticket = Ticket.objects.select_for_update().filter(dialog=dialog, status=Ticket.Status.OPEN).first()
    if ticket is None:
        ticket = Ticket.objects.create(dialog=dialog, handoff_reason="student_requested_manager", handoff_route=route)
    from .handoff_plans import finish_explicit_handoff

    if not finish_explicit_handoff(cursor, dialog, ticket):
        handoff_active_requests(cursor, dialog, ticket)
    # Entries already stored behind this request in the same live cycle belong to the new card.
    following = InboundRoute.objects.filter(
        Q(status="pending") | Q(status="needs_review", reason="manual_support")
        | Q(status="ignored", reason="closing_acknowledgement"),
        dialog=dialog, sequence__gt=route.sequence, historical=False,
        automation_paused=False, automation_epoch=route.automation_epoch, mode_version=route.mode_version,
        message__ticket__isnull=True,
    )
    following_ids = list(following.values_list("pk", flat=True))
    Message.objects.filter(inboundroute__pk__in=following_ids).update(ticket=ticket)
    InboundRoute.objects.filter(pk__in=following_ids).update(status="attached", reason="student_requested_manager")
    message.ticket = ticket
    message.save(update_fields=["ticket"])
    if dialog.mode == "ai_auto":
        dialog.mode = "human_support"
        dialog.mode_version += 1
        dialog.save(update_fields=["mode", "mode_version", "updated_at"])
    route.status, route.reason = "attached", "student_requested_manager"
    return True
