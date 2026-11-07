from django.db import connection, transaction
from django.urls import reverse

from accounts.policies import has_work_access, profile_from_session, quarantine_incident
from accounts.services import AccessError, lock_profiles
from jobs.services import ClockTimestamp

from .models import Notification


def workspace_actor(session, *, lock=False):
    profile_id = session.get("staff_profile")
    locked = lock_profiles(profile_id).get(profile_id) if lock else None
    actor = None if lock and locked is None else profile_from_session(session, locked)
    if actor is None:
        raise AccessError("authentication_required", 401)
    if quarantine_incident() or not has_work_access(actor):
        raise AccessError("permission_denied", 403)
    return actor


def notify_assignment(event):
    if not connection.in_atomic_block:
        raise RuntimeError("Assignment notification requires the event transaction")
    if event.changed and event.new_owner_id is not None and event.new_owner_id != event.previous_owner_id:
        return Notification.objects.get_or_create(source_event=event, recipient_id=event.new_owner_id)[0]
    return None


def notification_data(item):
    return {
        "id": str(item.pk), "source_event_id": str(item.source_event_id), "kind": "ticket_assignment",
        "ticket_id": str(item.source_event.ticket_id), "created_at": item.created_at, "read_at": item.read_at,
        "target_url": reverse("support-ticket", args=[item.source_event.ticket_id]),
    }


@transaction.atomic
def read_notification(session, notification_id):
    actor = workspace_actor(session, lock=True)
    item = Notification.objects.select_for_update().filter(pk=notification_id, recipient=actor).first()
    if item is None:
        raise AccessError("not_found", 404)
    changed = item.read_at is None
    if changed:
        Notification.objects.filter(pk=item.pk).update(read_at=ClockTimestamp())
        item.refresh_from_db()
    return item, changed
