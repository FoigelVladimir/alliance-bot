"""Participant ownership is proved by both VK ID and a verified bot callback."""

import json
import re
from datetime import timedelta
from urllib.parse import urlsplit, urlencode
from uuid import NAMESPACE_OID, UUID, uuid5

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core import signing
from django.db import transaction
from django.urls import reverse

from accounts.models import IdentityBinding
from accounts.policies import profile_from_session, quarantine_incident
from accounts.services import AccessError, lock_profiles
from jobs.services import enqueue
from notifications.outbox import enqueue_message
from vk_channel.models import LongPollCursor, StudentClick
from vk_channel.services import database_now

from .models import BindingAttempt, BotAccount, EntryLink, ParticipantEvent, Person

CONFIRM_TASK = "participants.confirm_binding"
LINK_TTL = timedelta(minutes=10)


def enabled():
    return settings.PARTICIPANT_BINDING_VERIFIED and not quarantine_incident()


def signer():
    return signing.Signer(salt="participants.entry.v1")


def public_url(link):
    base = settings.PUBLIC_BASE_URL
    parsed = urlsplit(base)
    if (not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/") or (parsed.scheme != "https" and not (
                settings.DEBUG and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}))):
        raise AccessError("participant_not_configured", 503)
    return base + reverse("participant-open") + "?" + urlencode({"key": signer().sign(str(link.pk))})


def link_from_key(key):
    try:
        if not isinstance(key, str) or len(key) > 256:
            raise ValueError
        identifier = UUID(signer().unsign(key))
    except (ValueError, signing.BadSignature):
        raise AccessError("participant_link_unavailable", 410) from None
    link = EntryLink.objects.filter(pk=identifier).first()
    if link is None:
        raise AccessError("participant_link_unavailable", 410)
    return link


def link_valid(link, account, cursor):
    return (enabled() and link.expires_at > database_now() and not cursor.automation_paused
            and link.account_id == account.pk and link.binding_version == account.binding_version
            and link.automation_epoch == cursor.automation_epoch and not account.quarantine_incident)


def issue_link(cursor, message, purpose="my_requests", target_id=None):
    source_event = message.inbound_event
    if (not enabled() or source_event is None or source_event.status != "accepted" or source_event.gap_id
            or source_event.cursor_id != cursor.pk or source_event.event_type != "message_new"
            or source_event.automation_epoch != cursor.automation_epoch or cursor.automation_paused
            or message.sender_vk_id != source_event.peer_id or message.dialog.vk_peer_id != source_event.peer_id):
        raise AccessError("participant_origin_unverified", 409)
    account, _ = BotAccount.objects.select_for_update().get_or_create(cursor=cursor, sender_id=source_event.peer_id)
    link, created = EntryLink.objects.get_or_create(source_event=source_event, defaults={
        "account": account, "purpose": purpose, "target_id": target_id,
        "binding_version": account.binding_version, "automation_epoch": cursor.automation_epoch,
        "expires_at": database_now() + LINK_TTL,
    })
    if created:
        event = None
        if purpose == "event":
            from events.models import Event
            event = Event.objects.filter(pk=target_id).first()
        text = "Откройте свою форму: " + public_url(link) + "\nСсылка действует 10 минут. Войдите через ВК и подтвердите связь в этом диалоге."
        if account.person_id is None or purpose == "notifications":
            text += "\nКатегории уведомлений: анонсы, расписания и напоминания, переносы и отмены, решения по заявкам. Сам вход не включает их; выбор можно пропустить."
        enqueue_message(account, "participant_entry", link.pk, text,
                        uuid5(NAMESPACE_OID, "participant-entry:" + str(message.inbound_event_id)), link.expires_at, event=event, priority=10)
    return link


def route_message(cursor, message):
    if not enabled():
        return False
    command = message.text.strip().casefold()
    if command in {"мои заявки", "/my_requests"}:
        issue_link(cursor, message)
        return True
    if command in {"уведомления", "настройки уведомлений"}:
        link = issue_link(cursor, message, "notifications")
        from notifications.bot_actions import add_settings_control
        add_settings_control(link)
        return True
    match = re.fullmatch(r"запись на мероприятие ([1-9][0-9]{0,18})", command)
    if not match or int(match[1]) >= 2**63:
        return False
    from events.models import Event
    if not Event.objects.filter(pk=int(match[1]), deleted_at__isnull=True, deletion_pending=False,
                                restoration_verification_required=False, current_form__isnull=False).exists():
        return False
    issue_link(cursor, message, "event", int(match[1]))
    return True


def browser_identity(session, *, lock=False):
    queryset = Session.objects.select_for_update() if lock else Session.objects
    row = queryset.filter(session_key=session.session_key, expire_date__gt=database_now()).first()
    if row is None:
        raise AccessError("authentication_required", 401)
    values = row.get_decoded()
    profile_id = values.get("staff_profile")
    locked = lock_profiles(profile_id).get(profile_id) if lock else None
    profile = profile_from_session(values, locked)
    if profile is None or not enabled():
        raise AccessError("authentication_required", 401)
    identity = IdentityBinding.objects.filter(pk=values.get("staff_binding"), profile=profile, active=True).first()
    if identity is None:
        raise AccessError("authentication_required", 401)
    return profile, identity


def actor(session, *, lock=False):
    profile, identity = browser_identity(session, lock=lock)
    people = Person.objects.select_for_update() if lock else Person.objects
    person = people.filter(pk=session.get("participant_person"), identity=identity, merged_into__isnull=True).first()
    if person is None or person.quarantine_incident:
        raise AccessError("participant_confirmation_required", 403)
    head = BotAccount.objects.filter(pk=session.get("participant_account"), person=person).values("cursor_id").first()
    if head is None:
        raise AccessError("participant_confirmation_required", 403)
    if lock:
        LongPollCursor.objects.select_for_update().get(pk=head["cursor_id"])
    accounts = BotAccount.objects.select_for_update() if lock else BotAccount.objects
    account = accounts.select_related("cursor").get(pk=session["participant_account"])
    if (account.person_id != person.pk or account.binding_version != session.get("participant_binding_version")
            or account.quarantine_incident):
        raise AccessError("participant_confirmation_required", 403)
    return person


def selected_link(session):
    try:
        identifier = UUID(session.get("participant_launch", ""))
    except (ValueError, TypeError, AttributeError):
        raise AccessError("participant_link_unavailable", 410) from None
    link = EntryLink.objects.filter(pk=identifier).first()
    if link is None:
        raise AccessError("participant_link_unavailable", 410)
    return link


@transaction.atomic
def activate(session):
    profile, identity = browser_identity(session, lock=True)
    person = Person.objects.select_for_update().filter(identity=identity, merged_into__isnull=True).first()
    link = selected_link(session)
    cursor = LongPollCursor.objects.select_for_update().get(pk=link.account.cursor_id)
    account = BotAccount.objects.select_for_update().get(pk=link.account_id)
    if not link_valid(link, account, cursor):
        raise AccessError("participant_link_unavailable", 410)
    if person is None or account.person_id != person.pk or person.quarantine_incident:
        raise AccessError("participant_confirmation_required", 403)
    session.update({"participant_person": person.pk, "participant_account": account.pk,
                    "participant_binding_version": account.binding_version, "participant_epoch": cursor.automation_epoch,
                    "participant_after_preferences": {"purpose": link.purpose, "target_id": link.target_id}})
    session.save()
    return person, link


@transaction.atomic
def start_binding(session):
    profile, identity = browser_identity(session, lock=True)
    person = Person.objects.select_for_update().filter(identity=identity, merged_into__isnull=True).first()
    link = selected_link(session)
    cursor = LongPollCursor.objects.select_for_update().get(pk=link.account.cursor_id)
    account = BotAccount.objects.select_for_update().get(pk=link.account_id)
    if not link_valid(link, account, cursor):
        raise AccessError("participant_link_unavailable", 410)
    if account.person_id and (person is None or account.person_id != person.pk):
        raise AccessError("participant_binding_conflict", 409)
    now = database_now()
    previous = BindingAttempt.objects.filter(link=link, identity=identity, session_key=session.session_key,
                                             status="pending", expires_at__gt=now).first()
    if previous:
        return previous
    if BindingAttempt.objects.filter(identity=identity, created_at__gt=now - timedelta(hours=1)).count() >= 5:
        raise AccessError("participant_attempt_limit", 429)
    attempt = BindingAttempt.objects.create(link=link, identity=identity, session_key=session.session_key,
                                            session_epoch=profile.session_epoch, binding_version=account.binding_version,
                                            expires_at=min(link.expires_at, now + LINK_TTL))
    purpose = {"my_requests": "Мои заявки", "notifications": "Настройки уведомлений", "event": "Регистрация на мероприятие", "section": "Запись в секцию", "team": "Расписание команды"}[link.purpose]
    text = f"Подтвердите вход в приложение клуба. Аккаунт: {profile.display_name or 'пользователь ВК'}. Назначение: {purpose}. Если это не вы, не нажимайте кнопку."
    keyboard = {"inline": True, "buttons": [[{"action": {"type": "callback", "label": "Подтвердить вход",
                 "payload": json.dumps({"action_id": str(attempt.pk)})}, "color": "primary"}]]}
    enqueue_message(account, "participant_confirmation", attempt.pk, text,
                    uuid5(NAMESPACE_OID, "participant-confirm:" + str(attempt.pk)), attempt.expires_at,
                    keyboard=keyboard, priority=10)
    return attempt


def binding_context(head):
    """Session -> identity profile -> existing person -> cursor -> bot identity -> attempt."""
    row = Session.objects.select_for_update().filter(session_key=head.session_key, expire_date__gt=database_now()).first()
    profile = lock_profiles(head.identity.profile_id).get(head.identity.profile_id)
    person = Person.objects.select_for_update().filter(identity_id=head.identity_id, merged_into__isnull=True).first()
    cursor = LongPollCursor.objects.select_for_update().get(pk=head.link.account.cursor_id)
    account = BotAccount.objects.select_for_update().get(pk=head.link.account_id)
    attempt = BindingAttempt.objects.select_for_update().select_related("link").get(pk=head.pk)
    values = row.get_decoded() if row else {}
    current = profile_from_session(values, profile) if row else None
    identity_valid = IdentityBinding.objects.filter(pk=head.identity_id, profile=profile, active=True).exists()
    valid = (current is not None and identity_valid and values.get("staff_binding") == head.identity_id
             and profile.session_epoch == head.session_epoch and link_valid(attempt.link, account, cursor)
             and (person is None or not person.quarantine_incident)
             and attempt.binding_version == account.binding_version and attempt.expires_at > database_now())
    return valid, person, cursor, account, attempt


def notice_guard(message):
    if not enabled():
        return False
    if message.kind == "participant_entry":
        link = EntryLink.objects.filter(pk=message.reference_id, account_id=message.account_id).first()
        if not link:
            return False
        cursor = LongPollCursor.objects.select_for_update().get(pk=link.account.cursor_id)
        account = BotAccount.objects.select_for_update().get(pk=link.account_id)
        return link_valid(link, account, cursor)
    head = BindingAttempt.objects.select_related("identity", "link__account").filter(pk=message.reference_id).first()
    if head is None or head.link.account_id != message.account_id:
        return False
    valid, _, _, _, attempt = binding_context(head)
    return valid and attempt.status == "pending"


def confirm_binding_task(task):
    def save():
        head_click = StudentClick.objects.get(pk=task.payload["click_id"])
        head = BindingAttempt.objects.select_related("identity", "link__account").get(pk=head_click.action_id)
        from vk_channel.student_actions import ANSWER_CLICK, click_key

        # Wrong senders never invalidate the rightful sender's pending attempt.
        own = (head_click.cursor_id == head.link.account.cursor_id
               and head_click.user_id == head_click.peer_id == head.link.account.sender_id)
        if own:
            valid, person, cursor, account, attempt = binding_context(head)
        else:
            valid = False
        click = StudentClick.objects.select_for_update().select_related("event").get(pk=head_click.pk)
        if click.result:
            return click.result
        result, text = {"status": "stale"}, "Подтверждение недоступно."
        if own and valid and click.event.status == "accepted" and not click.event.gap_id and click.event.automation_epoch == cursor.automation_epoch:
            if attempt.status == "confirmed" and account.person_id == attempt.person_id:
                result, text = {"status": "applied", "replayed": True}, "Вход уже подтверждён."
            elif attempt.status == "pending":
                conflict = (account.person_id is not None and (person is None or account.person_id != person.pk))
                conflict = conflict or (person is not None and (person.quarantine_incident or BotAccount.objects.filter(
                    cursor=cursor, person=person).exclude(pk=account.pk).exists()))
                if conflict:
                    attempt.status, attempt.finished_at = "conflict", database_now()
                    attempt.save(update_fields=["status", "finished_at"])
                    text = "Связь аккаунтов требует проверки администратором."
                else:
                    now = database_now()
                    if person is None:
                        person = Person.objects.create(identity_id=attempt.identity_id,
                            full_name=head.identity.profile.display_name, identity_verified_at=now)
                    if account.person_id is None:
                        account.person, account.binding_version = person, account.binding_version + 1
                        account.save(update_fields=["person", "binding_version"])
                        attempt.link.binding_version = account.binding_version
                        attempt.link.save(update_fields=["binding_version"])
                    attempt.status, attempt.person, attempt.proof, attempt.finished_at = "confirmed", person, click, now
                    attempt.binding_version = account.binding_version
                    attempt.save(update_fields=["status", "person", "proof", "finished_at", "binding_version"])
                    ParticipantEvent.objects.create(person=person, action="identity_confirmed", payload={
                        "attempt_id": str(attempt.pk), "account_id": account.pk, "binding_version": account.binding_version,
                        "proof_click_id": click.pk})
                    result, text = {"status": "applied"}, "Вход подтверждён. В приложении выберите уведомления или пропустите этот шаг."
        click.result, click.answer_text = result, text
        click.save(update_fields=["result", "answer_text"])
        enqueue(ANSWER_CLICK, click_key(ANSWER_CLICK, click.pk), {"click_id": click.pk}, priority=20)
        return result

    return save
