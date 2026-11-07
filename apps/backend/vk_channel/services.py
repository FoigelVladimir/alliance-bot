import re
from dataclasses import dataclass
from datetime import timedelta
from uuid import NAMESPACE_OID, UUID, uuid4, uuid5

from django.conf import settings
from django.db import connection, transaction

from accounts.policies import quarantine_incident
from jobs.services import enqueue
from support.ai_processing import begin_lookup
from support.attachment_source import source_url
from support.handoff import route_live_message
from support.models import Attachment, Dialog, InboundRoute, Message, Ticket

from .models import InboundEvent, LongPollCursor, LongPollGap
from .student_actions import queue_click

TASK_TYPE = "support.route_inbound"
GAP_TASK = "vk.backfill_long_poll_gap"
LEASE_DURATION = timedelta(seconds=90)
MAX_ID = 2**63 - 1
ATTACHMENT_TYPES = {"photo", "audio_message", "doc", "graffiti"}


class InvalidBatch(ValueError):
    pass


class ReaderLeaseLost(Exception):
    pass


@dataclass(frozen=True)
class Reader:
    cursor_id: int
    community_id: int
    position: str
    token: UUID
    receipt_version: int


def database_now():
    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        return cursor.fetchone()[0]


def valid_id(value):
    return type(value) is int and 0 < value <= MAX_ID


def position_value(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,128}", value):
        raise InvalidBatch("Invalid VK position")
    return value


@transaction.atomic
def claim_reader(community_id, initial_position):
    if not valid_id(community_id):
        raise InvalidBatch("Invalid community ID")
    position = position_value(initial_position)
    cursor, _ = LongPollCursor.objects.select_for_update().get_or_create(
        community_id=community_id, defaults={"position": position},
    )
    now = database_now()
    if cursor.leased_until and cursor.leased_until > now:
        return None
    cursor.lease_token = uuid4()
    cursor.leased_until = now + LEASE_DURATION
    cursor.save(update_fields=["lease_token", "leased_until"])
    return Reader(cursor.pk, community_id, cursor.position, cursor.lease_token, cursor.receipt_version)


def locked_cursor(reader):
    cursor = LongPollCursor.objects.select_for_update().get(pk=reader.cursor_id, community_id=reader.community_id)
    now = database_now()
    if (
        cursor.lease_token != reader.token or cursor.leased_until is None
        or cursor.leased_until <= now or cursor.position != reader.position
        or cursor.receipt_version != reader.receipt_version
    ):
        raise ReaderLeaseLost
    return cursor


@transaction.atomic
def renew_reader(reader):
    cursor = locked_cursor(reader)
    cursor.leased_until = database_now() + LEASE_DURATION
    cursor.save(update_fields=["leased_until"])


@transaction.atomic
def release_reader(reader):
    LongPollCursor.objects.filter(pk=reader.cursor_id, lease_token=reader.token).update(
        lease_token=None, leased_until=None,
    )


def attachment_metadata(message, attachments):
    for index, attachment in enumerate(attachments):
        kind = attachment.get("type") if isinstance(attachment, dict) else None
        if not isinstance(kind, str) or kind not in ATTACHMENT_TYPES:
            yield Attachment(message=message, source_position=index, attachment_type="other", state="unsupported")
            continue
        data = attachment.get(kind)
        if not isinstance(data, dict):
            yield Attachment(message=message, source_position=index, attachment_type=kind, state="unavailable")
            continue
        metadata = {}
        for key in ("size", "duration"):
            if valid_id(data.get(key)):
                metadata[key] = data[key]
        title = data.get("title")
        if isinstance(title, str) and valid_text(title):
            metadata["title"] = title[:255]
        url = source_url(kind, data)
        ref = {"peer_id": message.dialog.vk_peer_id, "cmid": message.conversation_message_id}
        if url:
            ref["url"] = url
        owner_id, media_id = data.get("owner_id"), data.get("id")
        attach_id = f"{owner_id}_{media_id}" if type(owner_id) is int and type(media_id) is int else None
        yield Attachment(
            message=message, source_position=index, attachment_type=kind, state="metadata_only", meta_info=metadata,
            source_ref=ref, vk_attach_id=attach_id if attach_id and len(attach_id) <= 100 else None,
        )


def valid_text(value):
    if not isinstance(value, str) or "\0" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def save_event(cursor, update, gap=None):
    status = InboundEvent.Status
    kind = update["type"]
    base = {"cursor": cursor, "event_type": kind, "automation_epoch": cursor.automation_epoch, "gap": gap}
    if kind == "message_event":
        queue_click(cursor, update, gap)
        return
    if kind != "message_new":
        InboundEvent.objects.create(**base, status=status.UNSUPPORTED, reason="unsupported_type")
        return
    if type(update.get("group_id")) is not int or update["group_id"] != cursor.community_id:
        InboundEvent.objects.create(**base, status=status.QUARANTINED, reason="community_mismatch")
        return
    obj = update.get("object")
    data = obj.get("message") if isinstance(obj, dict) else None
    if not isinstance(data, dict):
        InboundEvent.objects.create(**base, status=status.QUARANTINED, reason="missing_message")
        return
    peer_id, cmid, sender_id = (data.get(key) for key in ("peer_id", "conversation_message_id", "from_id"))
    if not all(map(valid_id, (peer_id, cmid, sender_id))) or data.get("out", 0) != 0:
        InboundEvent.objects.create(**base, status=status.QUARANTINED, reason="invalid_message_identity")
        return
    text, attachments = data.get("text", ""), data.get("attachments", [])
    if not valid_text(text) or not isinstance(attachments, list):
        InboundEvent.objects.create(**base, status=status.QUARANTINED, reason="invalid_message_content")
        return
    event, created = InboundEvent.objects.get_or_create(
        cursor=cursor, event_type=kind, peer_id=peer_id, conversation_message_id=cmid, status=status.ACCEPTED,
        defaults={"automation_epoch": cursor.automation_epoch, "gap": gap},
    )
    if not created:
        return
    dialog, _ = Dialog.objects.select_for_update().get_or_create(cursor=cursor, vk_peer_id=peer_id)
    dialog.last_inbound_sequence += 1
    dialog.save(update_fields=["last_inbound_sequence", "updated_at"])
    ticket = None
    if gap is None and not cursor.automation_paused:
        ticket = Ticket.objects.filter(dialog=dialog, status=Ticket.Status.OPEN).first()
    message = Message.objects.create(
        dialog=dialog, ticket=ticket, inbound_event=event, conversation_message_id=cmid,
        sender_vk_id=sender_id, text=text,
    )
    Attachment.objects.bulk_create(attachment_metadata(message, attachments))
    reply = data.get("reply_message")
    reply_to_cmid = reply.get("conversation_message_id") if isinstance(reply, dict) else None
    route = InboundRoute.objects.create(
        message=message, dialog=dialog, sequence=dialog.last_inbound_sequence, mode_version=dialog.mode_version,
        reply_to_cmid=reply_to_cmid if valid_id(reply_to_cmid) else None,
        automation_epoch=cursor.automation_epoch, historical=gap is not None, automation_paused=cursor.automation_paused,
    )
    enqueue(TASK_TYPE, uuid5(NAMESPACE_OID, f"{TASK_TYPE}:{route.pk}"), {"route_id": route.pk})


def validate_updates(updates):
    if not isinstance(updates, list) or any(
        not isinstance(update, dict) or not isinstance(update.get("type"), str)
        or not re.fullmatch(r"[a-z][a-z_0-9]{0,63}", update["type"])
        for update in updates
    ):
        raise InvalidBatch("Unrecognizable event batch")


def pilot_updates(updates, community_id, peer_id):
    if not isinstance(updates, list) or not valid_id(peer_id):
        raise InvalidBatch("Invalid pilot batch or dialog")
    scoped = []
    for update in updates:
        if not isinstance(update, dict) or not valid_id(update.get("group_id")) or update["group_id"] != community_id:
            continue
        item = update.get("object")
        kind = update.get("type")
        if not isinstance(item, dict) or kind not in ("message_new", "message_event"):
            continue
        data = item.get("message") if kind == "message_new" else item
        if isinstance(data, dict) and valid_id(data.get("peer_id")) and data["peer_id"] == peer_id:
            scoped.append(update)
    return scoped


@transaction.atomic
def commit_batch(reader, next_position, updates, *, pilot_peer_id=None):
    next_position = position_value(next_position)
    if pilot_peer_id is not None:
        updates = pilot_updates(updates, reader.community_id, pilot_peer_id)
    validate_updates(updates)
    cursor = locked_cursor(reader)
    for update in updates:
        save_event(cursor, update)
    locked_cursor(reader)
    now = database_now()
    cursor.position = next_position
    cursor.last_batch_at = now
    cursor.leased_until = now + LEASE_DURATION
    cursor.receipt_version += 1
    cursor.save(update_fields=["position", "last_batch_at", "leased_until", "receipt_version"])
    return Reader(cursor.pk, cursor.community_id, next_position, reader.token, cursor.receipt_version)


@transaction.atomic
def record_gap(reader, next_position, reason, *, allow_history=True):
    next_position = position_value(next_position)
    if reason not in {"position_lost", "key_recovery_failed", "database_restore"}:
        raise InvalidBatch("Invalid gap reason")
    return open_gap(locked_cursor(reader), next_position, reason, allow_history=allow_history)


def open_gap(cursor, next_position, reason, *, incident="", allow_history=True):
    scoped = not allow_history or bool(settings.VK_LONG_POLL_PILOT_PEER_ID)
    history_enabled = settings.VK_GAP_HISTORY_VERIFIED and not scoped
    gap = LongPollGap.objects.create(
        cursor=cursor, period=cursor.period, previous_position=cursor.position, next_position=next_position,
        previous_batch_at=cursor.last_batch_at, reason=reason, restore_incident=incident,
        status="queued" if history_enabled else "needs_review",
        coverage={} if history_enabled else {"reasons": ["pilot_dialog_scope" if scoped else "history_unverified"]},
    )
    cursor.position = next_position
    cursor.period += 1
    cursor.automation_epoch += 1
    cursor.automation_paused = True
    cursor.lease_token = None
    cursor.leased_until = None
    cursor.save(update_fields=[
        "position", "period", "automation_epoch", "automation_paused", "lease_token", "leased_until",
    ])
    if history_enabled:
        enqueue(GAP_TASK, uuid5(NAMESPACE_OID, f"{GAP_TASK}:{gap.pk}"), {"gap_id": gap.pk})
    return gap


@transaction.atomic
def save_historical_batch(gap_id, updates):
    validate_updates(updates)
    cursor_id = LongPollGap.objects.values_list("cursor_id", flat=True).get(pk=gap_id)
    cursor = LongPollCursor.objects.select_for_update().get(pk=cursor_id)
    gap = LongPollGap.objects.get(pk=gap_id)
    for update in updates:
        save_event(cursor, update, gap)


def route_inbound_task(task):
    def save_result():
        dialog_id, cursor_id = InboundRoute.objects.values_list("dialog_id", "dialog__cursor_id").get(
            pk=task.payload["route_id"],
        )
        cursor = LongPollCursor.objects.select_for_update().get(pk=cursor_id)
        dialog = Dialog.objects.select_for_update().get(pk=dialog_id)
        route = InboundRoute.objects.select_for_update().get(pk=task.payload["route_id"])
        if route.status == "pending":
            message = Message.objects.select_for_update().get(pk=route.message_id)
            if message.ticket_id:
                route.status = "attached"
            elif route.historical:
                route.status, route.reason = "needs_review", "historical"
            elif route.automation_paused or cursor.automation_paused or quarantine_incident():
                route.status, route.reason = "needs_review", "channel_paused"
            elif not route_live_message(cursor, dialog, route, message):
                if not begin_lookup(cursor, dialog, route, message):
                    route.status, route.reason = "needs_review", "manual_support"
            route.save(update_fields=["status", "reason"])
        return {"route_id": route.pk, "status": route.status}
    return save_result



def poll_once(reader, server, read, discover, *, pilot_peer_id=None):
    """Read outside transactions and return the new reader and current server parameters."""
    renew_reader(reader)
    result = read(server, reader.position)
    refreshed = type(result.get("failed")) is int and result["failed"] == 2
    if refreshed:
        server = discover()
        renew_reader(reader)
        result = read(server, reader.position)
    if "failed" not in result:
        return commit_batch(reader, result.get("ts"), result.get("updates"), pilot_peer_id=pilot_peer_id), server
    if type(result["failed"]) is int and result["failed"] == 1 and "updates" not in result:
        record_gap(reader, result.get("ts"), "key_recovery_failed" if refreshed else "position_lost",
                   allow_history=pilot_peer_id is None)
        return None, server
    raise InvalidBatch("Unexpected Long Poll failure")
