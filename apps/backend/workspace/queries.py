from django.db import DatabaseError, transaction
from django.db.models import Count, Q

from accounts.policies import has_work_access, profile_from_session, quarantine_incident, review_roles
from support.models import InboundRoute, OutgoingResponse, Ticket
from support.queries import allowed_communities, queue_tickets

from .models import Notification


def attention(actor):
    if not allowed_communities(actor).exists():
        return {"available": False}
    mine = queue_tickets(actor, "mine", "")
    free = queue_tickets(actor, "queue", "")
    reviews = InboundRoute.objects.filter(dialog__cursor_id__in=allowed_communities(actor), status="needs_review")
    return {
        "available": True, "mine_count": mine.count(), "free_count": free.count(),
        "mine": list(mine[:5]), "free": list(free[:5]), "review_count": reviews.count(),
    }


def summary(actor):
    communities = allowed_communities(actor)
    if not communities.exists():
        return {"available": False}
    totals = Ticket.objects.filter(dialog__cursor_id__in=communities, status=Ticket.Status.OPEN).aggregate(
        open_count=Count("pk"), free_count=Count("pk", filter=Q(current_owner__isnull=True)),
    )
    return {
        "available": True, **totals, "assigned_count": totals["open_count"] - totals["free_count"],
        "verification_count": OutgoingResponse.objects.filter(ticket__dialog__cursor_id__in=communities).filter(
            Q(status=OutgoingResponse.Status.UNKNOWN) | Q(recovery_verification_required=True),
        ).count(),
    }


SECTIONS = {"attention": attention, "summary": summary}


def home_section(actor, name):
    try:
        with transaction.atomic():
            return {"section": name, "data": SECTIONS[name](actor), "load_error": False}
    except DatabaseError:
        return {"section": name, "data": None, "load_error": True}


def navigation(request):
    actor = profile_from_session(request.session)
    if not has_work_access(actor) or quarantine_incident():
        return {}
    try:
        with transaction.atomic():
            unread = Notification.objects.filter(recipient=actor, read_at__isnull=True).count()
    except DatabaseError:
        unread = None
    return {"workspace_navigation": {
        "actor": actor, "unread_count": unread, "support_access": allowed_communities(actor).exists(),
        "can_review": bool(review_roles(actor)),
    }}
