import json
from datetime import timedelta
from uuid import NAMESPACE_OID, UUID, uuid5

from django.db import transaction

from accounts.policies import quarantine_incident
from jobs.models import BackgroundTask as Task
from jobs.services import ClockTimestamp, LeaseLost, admit_external_call, claim, enqueue, finish
from support.closing import cancel_waiting_on_close
from support.models import Dialog, OutgoingAttempt, OutgoingResponse, Ticket

from .client import DeliveryUnknown
from .models import InboundEvent, LongPollCursor, StudentAction, StudentClick

HANDLE_CLICK = "vk.handle_student_click"
ANSWER_CLICK = "vk.answer_student_click"
ANSWER_WINDOW = timedelta(seconds=10)
LABELS = {
    "resolve": "Вопрос решён",
    "call_manager": "Позвать менеджера",
    "continue": "Вопрос не решён",
}


@transaction.atomic
def issue_ticket_action(ticket_id, user_id, action, expires_at):
    from .services import database_now, valid_id

    if action not in LABELS or not valid_id(user_id) or expires_at <= database_now():
        raise ValueError("Invalid student action")
    dialog_id = Ticket.objects.values_list("dialog_id", flat=True).get(pk=ticket_id)
    dialog = Dialog.objects.select_for_update().get(pk=dialog_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    expected_status = Ticket.Status.CLOSED if action == "continue" else Ticket.Status.OPEN
    if ticket.status != expected_status or dialog.vk_peer_id != user_id:
        raise ValueError("Student action does not match the current ticket")
    token = StudentAction.objects.create(
        ticket=ticket, expected_user_id=user_id, action=action, mode_version=dialog.mode_version,
        expected_closed_at=ticket.closed_at, expires_at=expires_at,
    )
    return {"type": "callback", "label": LABELS[action], "payload": json.dumps({"action_id": str(token.pk)})}


def queue_click(cursor, update, gap):
    from .services import database_now, valid_id, valid_text

    event_type = "message_event"
    if gap is not None or type(update.get("group_id")) is not int or update["group_id"] != cursor.community_id:
        InboundEvent.objects.create(
            cursor=cursor, gap=gap, automation_epoch=cursor.automation_epoch, event_type=event_type,
            status=InboundEvent.Status.QUARANTINED, reason="invalid_click_origin",
        )
        return
    item = update.get("object")
    item = item if isinstance(item, dict) else {}
    peer, user, event_id = (item.get(key) for key in ("peer_id", "user_id", "event_id"))
    payload = item.get("payload")
    try:
        if isinstance(payload, str) and len(payload) <= 128:
            payload = json.loads(payload)
        action_id = UUID(payload["action_id"]) if isinstance(payload, dict) and set(payload) == {"action_id"} else None
    except (AttributeError, TypeError, ValueError):
        action_id = None
    if not (valid_id(peer) and valid_id(user) and valid_text(event_id) and 0 < len(event_id) <= 128 and action_id):
        InboundEvent.objects.create(
            cursor=cursor, automation_epoch=cursor.automation_epoch, event_type=event_type,
            status=InboundEvent.Status.QUARANTINED, reason="invalid_click_identity",
        )
        return
    key = {"cursor": cursor, "peer_id": peer, "user_id": user, "provider_event_id": event_id}
    if StudentClick.objects.filter(**key).exists():
        return
    now = database_now()
    event = InboundEvent.objects.create(
        cursor=cursor, peer_id=peer, automation_epoch=cursor.automation_epoch, event_type=event_type,
        status=InboundEvent.Status.ACCEPTED, received_at=now,
    )
    click = StudentClick.objects.create(**key, action_id=action_id, event=event, answer_deadline=now + ANSWER_WINDOW)
    enqueue(HANDLE_CLICK, click_key(HANDLE_CLICK, click.pk), {"click_id": click.pk}, priority=20)


def click_key(kind, pk):
    return uuid5(NAMESPACE_OID, f"{kind}:{pk}")


def queue_text(ticket):
    return "Менеджер уже назначен." if ticket.current_owner_id else "Менеджер скоро подключится."


def close_by_student(dialog, ticket, user_id, now):
    responses = list(OutgoingResponse.objects.select_for_update().filter(ticket=ticket).order_by("pk"))
    attempts = list(OutgoingAttempt.objects.select_for_update().filter(response__in=responses).order_by("pk"))
    ticket.status = Ticket.Status.CLOSED
    ticket.closed_at = now
    ticket.closed_by_vk_id = user_id
    ticket.closed_reason = "student_resolved"
    ticket.save(update_fields=["status", "closed_at", "closed_by_vk_id", "closed_reason"])
    dialog.mode = "ai_auto"
    dialog.mode_version += 1
    dialog.save(update_fields=["mode", "mode_version", "updated_at"])
    cancel_waiting_on_close(responses, attempts, now)


def apply_click(click):
    from .services import database_now

    stale = {"status": "stale"}, "Действие недоступно."
    cursor = LongPollCursor.objects.select_for_update().get(pk=click.cursor_id)
    if quarantine_incident() or cursor.automation_epoch != click.event.automation_epoch:
        return stale
    action = StudentAction.objects.filter(pk=click.action_id, expected_user_id=click.user_id).first()
    if action is None:
        return stale
    dialog = Dialog.objects.select_for_update().filter(
        pk=action.ticket.dialog_id, cursor_id=click.cursor_id, vk_peer_id=click.peer_id,
    ).first()
    if dialog is None or dialog.vk_peer_id != click.user_id:
        return stale
    ticket = Ticket.objects.select_for_update().get(pk=action.ticket_id)
    action = StudentAction.objects.select_for_update().get(pk=action.pk)
    if action.used_at:
        return {**action.result, "replayed": True}, "Действие уже выполнено."
    now = database_now()
    if now >= action.expires_at:
        return stale
    latest = Ticket.objects.filter(dialog=dialog).order_by("-pk").first()
    if action.action == "continue" and ticket.closed_at == action.expected_closed_at and (
        latest.status == Ticket.Status.OPEN and latest.parent_ticket_id == ticket.pk
    ):
        target, text = latest, queue_text(latest)
    elif dialog.mode_version != action.mode_version or latest.pk != ticket.pk:
        return stale
    elif action.action == "continue" and ticket.status == Ticket.Status.CLOSED and ticket.closed_at == action.expected_closed_at:
        target = Ticket.objects.create(dialog=dialog, parent_ticket=ticket, created_at=now)
        dialog.mode = "human_support"
        dialog.mode_version += 1
        dialog.save(update_fields=["mode", "mode_version", "updated_at"])
        text = queue_text(target)
    elif action.action == "resolve" and ticket.status == Ticket.Status.OPEN:
        close_by_student(dialog, ticket, click.user_id, now)
        target, text = ticket, "Вопрос закрыт."
    elif action.action == "call_manager" and ticket.status == Ticket.Status.OPEN:
        target, text = ticket, queue_text(ticket)
    else:
        return stale
    result = {"status": "applied", "ticket_id": str(target.pk), "action": action.action}
    action.used_at, action.result = now, result
    action.save(update_fields=["used_at", "result"])
    return result, text


def handle_click_task(task):
    def save_result():
        click = StudentClick.objects.select_for_update().get(pk=task.payload["click_id"])
        if click.result:
            return click.result
        click.result, click.answer_text = apply_click(click)
        click.save(update_fields=["result", "answer_text"])
        enqueue(ANSWER_CLICK, click_key(ANSWER_CLICK, click.pk), {"click_id": click.pk}, priority=20)
        return click.result

    return save_result


@transaction.atomic
def recover_answer():
    task = Task.objects.select_for_update(skip_locked=True).filter(
        task_type=ANSWER_CLICK, status=Task.Status.PROCESSING, locked_until__lte=ClockTimestamp(),
    ).first()
    if not task:
        return False
    click = StudentClick.objects.get(pk=task.payload["click_id"])
    from .services import database_now

    now = database_now()
    if task.admitted_at is not None:
        status, reason = Task.Status.UNKNOWN, "answer_outcome_unknown"
    elif now >= click.answer_deadline:
        status, reason = Task.Status.FAILED, "answer_expired"
    elif task.attempt_count >= task.max_attempts:
        status, reason = Task.Status.DEAD, "answer_budget_exhausted"
    else:
        status, reason = Task.Status.RETRY, "answer_not_started"
    Task.objects.filter(pk=task.pk).update(
        status=status, result={"reason": reason}, lease_token=None, locked_until=None, updated_at=now,
    )
    return True


def run_click_answer_once(community_id, send_answer):
    from .services import database_now

    while recover_answer():
        pass
    task = claim([ANSWER_CLICK])
    if task is None:
        return False
    try:
        with transaction.atomic():
            click = StudentClick.objects.select_related("cursor").get(pk=task.payload["click_id"])
            Task.objects.select_for_update().get(pk=task.pk)
            if database_now() >= click.answer_deadline:
                finish(task, {"reason": "answer_expired"}, status=Task.Status.FAILED)
                return True
            if click.cursor.community_id != community_id or quarantine_incident():
                finish(task, {"reason": "community_unavailable"}, status=Task.Status.FAILED)
                return True
            admit_external_call(task)
        try:
            send_answer(click.provider_event_id, click.user_id, click.peer_id, click.answer_text)
        except (DeliveryUnknown, OSError):
            finish(task, {"reason": "answer_outcome_unknown"}, status=Task.Status.UNKNOWN)
        else:
            finish(task, {"reason": "answer_sent"})
    except LeaseLost:
        pass
    return True
