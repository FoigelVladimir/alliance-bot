from datetime import datetime

from django.core import signing
from django.db.models import Exists, F, OuterRef, Prefetch, Q, Value
from django.db.models.functions import Collate
from django.utils import timezone

from accounts.services import AccessError

from .models import Attachment, Message, OutgoingResponse, SupportMembership, Ticket, TicketEvent


def allowed_communities(profile):
    return SupportMembership.objects.filter(
        profile=profile, access_generation=profile.access_generation,
    ).values_list("cursor_id", flat=True)


def ticket_data(ticket):
    return {
        "id": str(ticket.pk),
        "status": ticket.status,
        "current_owner_id": str(ticket.current_owner_id) if ticket.current_owner_id is not None else None,
        "assignment_version": ticket.assignment_version,
        "importance": ticket.importance,
        "opened_at": ticket.created_at,
    }


def open_ticket(dialog_id):
    return Ticket.objects.filter(dialog_id=dialog_id, status=Ticket.Status.OPEN).first()



def draft_data(draft):
    if draft is None:
        return None
    return {
        "id": str(draft.pk), "ticket_id": str(draft.ticket_id), "text": draft.text,
        "revision": str(draft.revision), "updated_at": draft.updated_at,
    }


def can_edit_draft(ticket, actor):
    return ticket.status == Ticket.Status.OPEN and ticket.current_owner_id == actor.pk



def may_coordinate(actor, cursor_id):
    return SupportMembership.objects.filter(
        profile=actor, cursor_id=cursor_id, access_generation=actor.access_generation, coordinates_support=True,
    ).exists()



def assignment_targets(ticket, actor):
    return SupportMembership.objects.filter(
        cursor_id=ticket.dialog.cursor_id,
        access_generation=F("profile__access_generation"),
        profile__access_state="granted", profile__blocked_at__isnull=True,
        profile__disabled=False, profile__bindings__active=True,
    ).exclude(profile_id__in=[actor.pk, ticket.current_owner_id]).select_related("profile").order_by(
        "profile__display_name", "profile_id",
    )


HISTORY_SIZE = 50
HISTORY_SALT = "support.history.v1"


def history_boundary(ticket, token):
    if token is None:
        return None
    try:
        data = signing.loads(token, salt=HISTORY_SALT)
        if not isinstance(data, list) or len(data) != 5:
            raise ValueError
        version, ticket_id, timestamp, rank, record_id = data
        instant = datetime.fromisoformat(timestamp)
        if (
            version != 1 or ticket_id != str(ticket.pk) or timezone.is_naive(instant)
            or type(rank) is not int or not 0 <= rank <= 2
            or type(record_id) is not int or not 0 < record_id < 2**63
        ):
            raise ValueError
        return instant, rank, record_id
    except (signing.BadSignature, TypeError, ValueError):
        raise AccessError("invalid_cursor", 400) from None


def history_keys(rows, time_field, rank, boundary):
    if boundary is not None:
        instant, boundary_rank, boundary_id = boundary
        predicate = Q(**{f"{time_field}__lt": instant})
        if rank < boundary_rank:
            predicate |= Q(**{time_field: instant})
        elif rank == boundary_rank:
            predicate |= Q(pk__lt=boundary_id, **{time_field: instant})
        rows = rows.filter(predicate)
    return rows.order_by().annotate(
        history_at=F(time_field), history_rank=Value(rank), history_id=F("pk"),
    ).values_list("history_at", "history_rank", "history_id")


def ticket_history(ticket, token=None):
    boundary = history_boundary(ticket, token)
    messages = Message.objects.filter(ticket=ticket, dialog_id=ticket.dialog_id, outgoingresponse__isnull=True)
    responses = OutgoingResponse.objects.filter(ticket=ticket)
    events = TicketEvent.objects.filter(ticket=ticket, changed=True)
    keys = list(history_keys(messages, "created_at", 0, boundary).union(
        history_keys(responses, "command__created_at", 1, boundary),
        history_keys(events, "command__created_at", 2, boundary),
        all=True,
    ).order_by("-history_at", "-history_rank", "-history_id")[:HISTORY_SIZE + 1])
    selected = keys[:HISTORY_SIZE]
    grouped = [[pk for _, kind, pk in selected if kind == rank] for rank in range(3)]
    message_rows = messages.filter(pk__in=grouped[0]).prefetch_related(
        Prefetch("attachments", queryset=Attachment.objects.select_related("file_object").order_by("source_position")),
    ).in_bulk()
    response_rows = responses.filter(pk__in=grouped[1]).select_related("message").in_bulk()
    event_rows = events.filter(pk__in=grouped[2]).select_related("command__actor").in_bulk()
    rows = [message_rows, response_rows, event_rows]
    earlier = None
    if len(keys) > HISTORY_SIZE:
        instant, rank, record_id = selected[-1]
        earlier = signing.dumps([1, str(ticket.pk), instant.isoformat(), rank, record_id], salt=HISTORY_SALT)
    return {
        "history_entries": [
            (instant, rank, rows[rank][pk]) for instant, rank, pk in reversed(selected) if pk in rows[rank]
        ],
        "earlier_history": earlier, "older_history_page": boundary is not None,
    }



def queue_tickets(actor, view, query):
    tickets = Ticket.objects.filter(dialog__cursor_id__in=allowed_communities(actor)).select_related("current_owner")
    if view == "closed":
        tickets = tickets.filter(status=Ticket.Status.CLOSED)
    else:
        tickets = tickets.filter(status=Ticket.Status.OPEN)
        if view == "mine":
            tickets = tickets.filter(current_owner=actor)
        elif view == "queue":
            tickets = tickets.filter(current_owner__isnull=True)
    if query:
        messages = Message.objects.filter(ticket_id=OuterRef("pk")).alias(search_text=Collate("text", "und-x-icu"))
        matches = Q(Exists(messages.filter(search_text__icontains=query)))
        if len(query) <= 19 and query.isascii() and query.isdecimal() and int(query) < 2**63:
            matches |= Q(pk=int(query))
        tickets = tickets.filter(matches)
    return tickets.order_by("-closed_at", "-id") if view == "closed" else tickets.order_by("importance", "created_at", "id")
