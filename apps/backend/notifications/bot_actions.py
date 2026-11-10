import json
from uuid import NAMESPACE_OID, uuid5

from django.db import transaction

from accounts.policies import quarantine_incident
from accounts.services import AccessError
from jobs.services import enqueue
from vk_channel.models import StudentClick
from vk_channel.services import database_now

from .event_messages import recipient_context
from .models import ChannelMessage, PreferenceAction
from .preferences import apply_preferences, current_settings, lock_settings, settings_data

TASK_TYPE = "notifications.apply_preferences_click"


@transaction.atomic
def add_settings_control(link):
    account = link.account
    if account.person_id is None:
        return
    message = ChannelMessage.objects.select_for_update().filter(kind="participant_entry", reference_id=str(link.pk)).first()
    if message is None or message.status != "pending" or message.attempts.filter(admitted_at__isnull=False).exists():
        return
    current = current_settings(account.person)
    action, created = PreferenceAction.objects.get_or_create(source_link=link, defaults={
        "account": account, "person_key": account.person_id, "binding_version": account.binding_version,
        "automation_epoch": account.cursor.automation_epoch, "expected_revision": current.revision,
        "all_disabled": not current.all_disabled, "expires_at": link.expires_at})
    if not created:
        return
    label = "Отключить всё" if action.all_disabled else "Включить сохранённые"
    keyboard = {"inline": True, "buttons": [[{"action": {"type": "callback", "label": label,
        "payload": json.dumps({"action_id": str(action.pk)})}, "color": "secondary"}]]}
    message.keyboard = keyboard
    message.save(update_fields=["keyboard"])


def handle_preferences_click(task):
    def save():
        head_click = StudentClick.objects.get(pk=task.payload["click_id"])
        head = PreferenceAction.objects.select_related("account").get(pk=head_click.action_id)
        own = head_click.cursor_id == head.account.cursor_id and head_click.user_id == head_click.peer_id == head.account.sender_id
        context = recipient_context(head.person_key, head.account_id) if own and not quarantine_incident() else None
        click = StudentClick.objects.select_for_update().select_related("event").get(pk=head_click.pk)
        if click.result:
            return click.result
        result, text = {"status": "stale"}, "Кнопка недоступна. Откройте «Уведомления» заново."
        if context:
            person, account, cursor = context
            action = PreferenceAction.objects.select_for_update().get(pk=head.pk)
            if (action.binding_version == account.binding_version and action.automation_epoch == cursor.automation_epoch
                    and action.expires_at > database_now() and not cursor.automation_paused
                    and click.event.status == "accepted" and not click.event.gap_id
                    and click.event.automation_epoch == cursor.automation_epoch):
                if action.used_at:
                    result, text = {**action.result, "replayed": True}, "Этот выбор уже сохранён."
                else:
                    current = lock_settings(person)
                    operation_id = uuid5(NAMESPACE_OID, "notification-choice:" + str(action.pk))
                    try:
                        event, replayed = apply_preferences(person, operation_id, action.expected_revision, action.all_disabled,
                            settings_data(current)["choices"], confirm_full=False, complete_onboarding=False)
                    except AccessError as error:
                        if error.code not in {"notification_preferences_conflict", "operation_mismatch"}:
                            raise
                        result, text = {"status": "conflict"}, "Настройки изменились. Откройте «Уведомления» заново."
                    else:
                        result = {"status": "applied", "revision": event.after["revision"]}
                        text = "Все инициативные сообщения отключены." if action.all_disabled else "Сохранённые категории включены. Новые не добавлены."
                        action.used_at, action.result = database_now(), result
                        action.save(update_fields=["used_at", "result"])
        click.result, click.answer_text = result, text
        click.save(update_fields=["result", "answer_text"])
        from vk_channel.student_actions import ANSWER_CLICK, click_key
        enqueue(ANSWER_CLICK, click_key(ANSWER_CLICK, click.pk), {"click_id": click.pk}, priority=20)
        return result

    return save
