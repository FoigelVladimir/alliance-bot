"""Cancellation notices retain only the approved text and recipient identifiers."""

from datetime import timedelta
from uuid import NAMESPACE_OID, uuid5
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Q

from accounts.services import lock_profiles
from events.models import Event
from jobs.services import enqueue
from participants.models import BotAccount, EntryLink, Person
from vk_channel.models import LongPollCursor
from vk_channel.services import database_now

from .models import CancellationBatch, ChannelMessage
from .outbox import enqueue_message
from .preferences import permitted

PREPARE_CANCELLATION = "notifications.prepare_cancellation"


def recipient_choice(account, event, *, binding_ok=True):
    person = account.person
    reason = ""
    if (not binding_ok or person is None or person.identity_id is None or person.merged_into_id
            or person.quarantine_incident or not person.identity.active or person.identity.profile.disabled
            or account.quarantine_incident):
        reason = "identity_unverified"
    elif str(account.cursor.community_id) != settings.VK_GROUP_ID:
        reason = "channel_unavailable"
    elif not permitted(person, "changes", "event", event.pk, lock=True):
        reason = "notifications_disabled"
    return {"account_key": account.pk, "person_key": person.pk if person else None,
            "binding_version": account.binding_version, "automation_epoch": account.cursor.automation_epoch,
            "eligible": not reason, "reason": reason}


def delivery_footprint(event):
    messages = list(ChannelMessage.objects.filter(event=event).exclude(kind="event_cancellation")
                    .select_related("account__person__identity__profile", "account__cursor").prefetch_related("attempts").order_by("pk"))
    versions, recipients = [], {}
    observed = False
    for message in messages:
        admitted = any(attempt.admitted_at is not None for attempt in message.attempts.all())
        if not admitted:
            continue
        versions.append({"id": str(message.pk), "status": message.status, "account_id": message.account_id})
        if message.status not in {"sent", "pending", "unknown_outcome"}:
            continue
        observed = True
        account = message.account
        person = account.person
        binding_ok = message.binding_version == account.binding_version
        if not binding_ok and message.kind == "participant_entry" and message.person_id is None:
            # The very same entry may have established the initial, explicitly confirmed binding.
            binding_ok = EntryLink.objects.filter(pk=message.reference_id, account=account,
                binding_version=account.binding_version, bindingattempt__status="confirmed", bindingattempt__person=person).exists()
        candidate = recipient_choice(account, event, binding_ok=binding_ok)
        if account.pk not in recipients or candidate["eligible"]:
            recipients[account.pk] = candidate
    owners = set(event.applications.filter(status__in=["submitted", "needs_changes", "admitted"]).values_list("owner_id", flat=True))
    accounts = BotAccount.objects.filter(person_id__in=owners, cursor__community_id=int(settings.VK_GROUP_ID)).select_related(
        "person__identity__profile", "cursor") if (settings.VK_GROUP_ID.isascii() and settings.VK_GROUP_ID.isdecimal()
            and len(settings.VK_GROUP_ID) <= 19 and 0 < int(settings.VK_GROUP_ID) < 2**63) else []
    found = set()
    for account in accounts:
        found.add(account.person_id)
        candidate = recipient_choice(account, event)
        if account.pk not in recipients or candidate["eligible"]:
            recipients[account.pk] = candidate
    for person_id in owners - found:
        recipients[f"person:{person_id}"] = {"account_key": None, "person_key": person_id,
            "binding_version": None, "automation_epoch": None, "eligible": False, "reason": "channel_unavailable"}
    return {"observed": observed, "versions": versions, "recipients": sorted(recipients.values(),
        key=lambda item: (item["account_key"] is None, item["account_key"] or item["person_key"]))}


def cancellation_snapshot(event, footprint):
    local = event.planned_start_at.astimezone(ZoneInfo(event.timezone_name)).strftime("%d.%m.%Y %H:%M")
    return {"text": f"Мероприятие «{event.title}», запланированное на {local} ({event.timezone_name}), отменено.",
            "recipients": footprint["recipients"]}


def create_cancellation(deletion):
    snapshot = deletion.notification_snapshot
    batch, created = CancellationBatch.objects.get_or_create(deletion=deletion,
        defaults={"text": snapshot.get("text", ""), "recipients": snapshot.get("recipients", []),
                  "verification_required": deletion.notification_verification_required})
    if created and not batch.verification_required:
        for recipient in batch.recipients:
            if recipient["eligible"]:
                key = uuid5(NAMESPACE_OID, f"cancellation-prepare:{batch.pk}:{recipient['account_key']}")
                enqueue(PREPARE_CANCELLATION, key, {"batch_id": str(batch.pk), "account_key": recipient["account_key"]})
    return batch


def recipient_context(person_key, account_key):
    head = Person.objects.filter(pk=person_key).values("identity__profile_id").first()
    if not head or not head["identity__profile_id"]:
        return None
    profile = lock_profiles(head["identity__profile_id"]).get(head["identity__profile_id"])
    person = Person.objects.select_for_update(of=("self",)).select_related("identity").get(pk=person_key)
    if (person.merged_into_id or person.quarantine_incident or person.identity_id is None or not person.identity.active
            or person.identity.profile_id != head["identity__profile_id"] or profile is None or profile.disabled):
        return None
    account_head = BotAccount.objects.filter(pk=account_key).values("cursor_id").first()
    if not account_head:
        return None
    cursor = LongPollCursor.objects.select_for_update().get(pk=account_head["cursor_id"])
    account = BotAccount.objects.select_for_update().get(pk=account_key)
    if account.person_id != person.pk or account.quarantine_incident:
        return None
    return person, account, cursor


def prepare_cancellation_task(task):
    batch = CancellationBatch.objects.select_related("deletion").get(pk=task.payload["batch_id"])
    recipient = next(item for item in batch.recipients if item["account_key"] == task.payload["account_key"])

    def save():
        context = recipient_context(recipient["person_key"], recipient["account_key"])
        if context is None:
            return {"reason": "identity_unverified"}
        person, account, cursor = context
        event = Event.objects.select_for_update().get(pk=batch.deletion.event_id)
        current = CancellationBatch.objects.select_for_update().select_related("deletion").get(pk=batch.pk)
        if (current.verification_required or current.deletion.status != "deleted" or event.state != "deleted"
                or account.binding_version != recipient["binding_version"] or cursor.automation_epoch != recipient["automation_epoch"]):
            return {"reason": "cancellation_verification_required"}
        if not permitted(person, "changes", "event", event.pk, lock=True):
            return {"reason": "notifications_disabled"}
        key = uuid5(NAMESPACE_OID, f"cancellation-message:{batch.pk}:{account.pk}")
        message = enqueue_message(account, "event_cancellation", batch.pk, current.text, key,
            current.created_at + timedelta(days=7), person=person, event=event)
        return {"message_id": str(message.pk)}

    return save


def cancellation_guard(message):
    if not message.person_id or not message.event_id:
        return False
    context = recipient_context(message.person_id, message.account_id)
    if context is None:
        return False
    person, account, cursor = context
    event = Event.objects.select_for_update().get(pk=message.event_id)
    batch = CancellationBatch.objects.filter(pk=message.reference_id, deletion__event=event, deletion__status="deleted").first()
    return (event.state == "deleted" and batch is not None and not batch.verification_required
            and permitted(person, "changes", "event", event.pk, lock=True))
