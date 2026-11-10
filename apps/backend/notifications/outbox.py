"""One admitted VK call per durable attempt; an uncertain call is never retried."""

from hashlib import sha256
from uuid import NAMESPACE_OID, uuid5

from django.conf import settings
from django.db import transaction

from accounts.policies import quarantine_incident
from jobs.models import BackgroundTask as Task
from jobs.services import ClockTimestamp, LeaseLost, READY, admit_external_call, claim, enqueue, finish
from support.vk_keys import reserve_random_id
from vk_channel.client import DeliveryRejected, DeliveryUnknown
from vk_channel.services import database_now

from .models import ChannelAttempt, ChannelMessage

TASK_TYPE = "notifications.send_channel_message"
SUPPORT_TYPES = ("support.send_response", "send_vk_bot_part")


def enqueue_message(account, kind, reference_id, text, source_key, expires_at, *, keyboard=None, person=None, event=None, priority=-20):
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Message enqueue requires the source transaction")
    values = {"account": account, "kind": kind, "reference_id": str(reference_id), "text": text,
              "text_sha256": sha256(text.encode("utf-8")).hexdigest(),
              "binding_version": account.binding_version, "automation_epoch": account.cursor.automation_epoch,
              "expires_at": expires_at, "keyboard": keyboard, "person": person, "event": event}
    message, created = ChannelMessage.objects.get_or_create(source_key=source_key, defaults=values)
    if not created and (message.account_id != account.pk or message.kind != kind or message.reference_id != str(reference_id)
                        or message.text != text or message.keyboard != keyboard or message.person_id != (person.pk if person else None)
                        or message.event_id != (event.pk if event else None)):
        raise ValueError("Message source key was reused for a different intent")
    if created:
        task = enqueue(TASK_TYPE, uuid5(NAMESPACE_OID, "notice:" + str(message.pk)), {"message_id": str(message.pk)}, priority=priority)
        ChannelAttempt.objects.create(message=message, task=task, random_id_id=reserve_random_id())
    return message


def record(message, attempt, status, *, reason="", provider_id=None, error_code=None):
    now = database_now()
    message.status, message.reason, message.finished_at = status, reason, now
    message.provider_message_id = provider_id
    message.save(update_fields=["status", "reason", "finished_at", "provider_message_id"])
    attempt.status, attempt.finished_at = status, now
    attempt.provider_message_id, attempt.vk_error_code = provider_id, error_code
    attempt.save(update_fields=["status", "finished_at", "provider_message_id", "vk_error_code"])


@transaction.atomic
def admit(task, community_id, peer_id):
    head = ChannelAttempt.objects.select_related("message__account__cursor").get(task=task)
    source = head.message
    # The source-specific guard acquires session/profile/cursor/account locks before the outbox.
    if source.kind in {"participant_entry", "participant_confirmation"}:
        from participants.services import notice_guard
        permitted = notice_guard(source)
    elif source.kind == "event_cancellation":
        from .event_messages import cancellation_guard
        permitted = cancellation_guard(source)
    else:
        permitted = False
    if source.event_id:
        from events.models import Event
        event = Event.objects.select_for_update().get(pk=source.event_id)
        if event.restoration_verification_required or source.kind != "event_cancellation" and (event.deleted_at or event.deletion_pending):
            permitted = False
    message = ChannelMessage.objects.select_for_update().get(pk=source.pk)
    attempt = ChannelAttempt.objects.select_for_update().get(pk=head.pk)
    if message.status != "pending" or attempt.status != "pending" or attempt.admitted_at:
        raise LeaseLost(task.pk)
    account = message.account
    if (not permitted or quarantine_incident() or not settings.VK_GENERAL_SEND_ENABLED or not settings.VK_SEND_ENABLED
            or account.cursor.community_id != community_id or account.sender_id != peer_id
            or account.cursor.automation_paused or message.automation_epoch != account.cursor.automation_epoch
            or message.binding_version != account.binding_version or message.recovery_verification_required
            or database_now() >= message.expires_at):
        record(message, attempt, "cancelled", reason="source_no_longer_available")
        finish(task, {"reason": message.reason}, status=Task.Status.FAILED)
        return None
    admit_external_call(task)
    attempt.admitted_at = database_now()
    attempt.save(update_fields=["admitted_at"])
    return account.sender_id, attempt.random_id_id, message.text, message.keyboard


@transaction.atomic
def complete(task, status, **kwargs):
    head = ChannelAttempt.objects.values("id", "message_id", "message__event_id").get(task=task)
    if head["message__event_id"]:
        from events.models import Event
        Event.objects.select_for_update().get(pk=head["message__event_id"])
    message = ChannelMessage.objects.select_for_update().get(pk=head["message_id"])
    attempt = ChannelAttempt.objects.select_for_update().get(pk=head["id"])
    if attempt.status != "pending" or not attempt.admitted_at:
        raise LeaseLost(task.pk)
    record(message, attempt, status, **kwargs)
    finish(task, {"message_id": str(message.pk), "status": status},
           status={"sent": Task.Status.DONE, "failed": Task.Status.FAILED, "unknown_outcome": Task.Status.UNKNOWN}[status])


@transaction.atomic
def recover_one():
    task_id = Task.objects.filter(task_type=TASK_TYPE, status="processing", locked_until__lte=ClockTimestamp()).order_by("pk").values_list("pk", flat=True).first()
    if task_id is None:
        return False
    head = ChannelAttempt.objects.values("id", "message_id", "message__event_id").get(task_id=task_id)
    if head["message__event_id"]:
        from events.models import Event
        Event.objects.select_for_update().get(pk=head["message__event_id"])
    message = ChannelMessage.objects.select_for_update().get(pk=head["message_id"])
    attempt = ChannelAttempt.objects.select_for_update().get(pk=head["id"])
    task = Task.objects.select_for_update().get(pk=task_id)
    now = database_now()
    if task.status != "processing" or task.locked_until > now:
        return True
    if task.admitted_at or attempt.admitted_at:
        record(message, attempt, "unknown_outcome", reason="confirmation_lost")
        task.status = Task.Status.UNKNOWN
    elif task.attempt_count >= task.max_attempts or now >= message.expires_at:
        record(message, attempt, "failed", reason="send_budget_exhausted")
        task.status = Task.Status.DEAD
    else:
        task.status, task.scheduled_at = Task.Status.RETRY, now
    task.lease_token = task.locked_until = None
    task.updated_at = now
    task.save(update_fields=["status", "scheduled_at", "lease_token", "locked_until", "updated_at"])
    return True


def run_sender_once(community_id, peer_id, send):
    while recover_one():
        pass
    # Yield between recipients whenever support has work ready.
    if Task.objects.filter(task_type__in=SUPPORT_TYPES, status__in=READY, scheduled_at__lte=ClockTimestamp()).exists():
        return False
    candidates = ChannelAttempt.objects.filter(message__account__cursor__community_id=community_id,
                                              message__account__sender_id=peer_id).values_list("task_id", flat=True)
    task = claim([TASK_TYPE], task_ids=candidates)
    if task is None:
        return False
    try:
        args = admit(task, community_id, peer_id)
        if args is None:
            return True
        try:
            provider_id = send(*args)
        except DeliveryRejected as error:
            complete(task, "failed", reason="vk_rejected", error_code=error.code)
        except (DeliveryUnknown, OSError):
            complete(task, "unknown_outcome", reason="confirmation_lost")
        else:
            complete(task, "sent", provider_id=provider_id)
    except LeaseLost:
        pass
    return True
