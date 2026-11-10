from django.db import transaction
from types import SimpleNamespace

from accounts.services import AccessError
from events.forms import digest
from participants.models import ParticipantCommand
from participants.services import actor as participant_actor
from vk_channel.services import database_now

from .models import ParticipantSettings, Preference, SettingsEvent

CATEGORIES = {"announcements": "Анонсы клуба", "schedule": "Расписания и напоминания",
              "changes": "Переносы и отмены", "decisions": "Решения по заявкам"}


def settings_data(settings):
    return {"revision": settings.revision, "all_disabled": settings.all_disabled,
            "verification_required": settings.verification_required,
            "onboarding_completed": settings.onboarding_completed_at is not None,
            "choices": list(Preference.objects.filter(person=settings.person).order_by("category", "scope_kind", "subject_id")
                            .values("category", "scope_kind", "subject_id", "enabled"))}


def lock_settings(person):
    settings, _ = ParticipantSettings.objects.get_or_create(person=person)
    return ParticipantSettings.objects.select_for_update().get(pk=settings.pk)


def current_settings(person):
    return ParticipantSettings.objects.filter(person=person).first() or SimpleNamespace(
        person=person, revision=1, all_disabled=False, verification_required=False, onboarding_completed_at=None)


def validate_choices(choices):
    if not isinstance(choices, list) or len(choices) > 400:
        raise AccessError("invalid_notification_preferences", 422)
    result, keys = [], set()
    for choice in choices:
        if (not isinstance(choice, dict) or set(choice) != {"category", "scope_kind", "subject_id", "enabled"}
                or not isinstance(choice["category"], str) or choice["category"] not in CATEGORIES
                or not isinstance(choice["scope_kind"], str) or choice["scope_kind"] not in {"club", "section", "event"}
                or type(choice["subject_id"]) is not int or not 0 <= choice["subject_id"] < 2**63
                or type(choice["enabled"]) is not bool
                or (choice["scope_kind"] == "club") != (choice["subject_id"] == 0)):
            raise AccessError("invalid_notification_preferences", 422)
        key = (choice["category"], choice["scope_kind"], choice["subject_id"])
        if key in keys:
            raise AccessError("invalid_notification_preferences", 422)
        keys.add(key)
        result.append(choice)
    return sorted(result, key=lambda choice: (choice["category"], choice["scope_kind"], choice["subject_id"]))


@transaction.atomic
def save_preferences(session, operation_id, expected_revision, all_disabled, choices):
    person = participant_actor(session, lock=True)
    return apply_preferences(person, operation_id, expected_revision, all_disabled, choices, confirm_full=True)


@transaction.atomic
def skip_initial_choice(session, operation_id, expected_revision):
    person = participant_actor(session, lock=True)
    if type(expected_revision) is not int or not 0 < expected_revision < 2**63:
        raise AccessError("invalid_request", 400)
    fingerprint = digest({"action": "notification_skip", "person_id": person.pk, "revision": expected_revision})
    previous = SettingsEvent.objects.filter(settings__person=person, command__client_operation_id=operation_id).select_related("command").first()
    if previous:
        if previous.command.action != "notification_skip" or previous.command.fingerprint != fingerprint:
            raise AccessError("operation_mismatch", 409)
        return previous, True
    current = lock_settings(person)
    event, replayed = apply_preferences(person, operation_id, expected_revision, current.all_disabled,
                                        settings_data(current)["choices"], confirm_full=False)
    event.command.action, event.command.fingerprint = "notification_skip", fingerprint
    event.command.save(update_fields=["action", "fingerprint"])
    return event, replayed


@transaction.atomic
def apply_preferences(person, operation_id, expected_revision, all_disabled, choices, *, confirm_full=False, complete_onboarding=True):
    if type(expected_revision) is not int or not 0 < expected_revision < 2**63 or type(all_disabled) is not bool:
        raise AccessError("invalid_request", 400)
    clean = validate_choices(choices)
    fingerprint = digest({"person_id": person.pk, "revision": expected_revision, "all_disabled": all_disabled,
                          "choices": clean, "confirm_full": confirm_full, "complete_onboarding": complete_onboarding})
    # A person's stable record, rather than a replaceable login account, owns the UUID namespace.
    from .models import SettingsEvent
    previous_event = SettingsEvent.objects.filter(settings__person=person,
        command__client_operation_id=operation_id).select_related("command").first()
    if previous_event:
        if previous_event.command.fingerprint != fingerprint:
            raise AccessError("operation_mismatch", 409)
        return previous_event, True
    command, created = ParticipantCommand.objects.get_or_create(actor=person.identity.profile,
        client_operation_id=operation_id, defaults={"action": "notification_preferences", "fingerprint": fingerprint})
    if not created:
        if command.action != "notification_preferences" or command.fingerprint != fingerprint:
            raise AccessError("operation_mismatch", 409)
        return command.settingsevent, True
    settings = lock_settings(person)
    if settings.revision != expected_revision:
        raise AccessError("notification_preferences_conflict", 409, current_revision=settings.revision)
    before = settings_data(settings)
    settings.all_disabled, settings.revision = all_disabled, settings.revision + 1
    if confirm_full:
        settings.verification_required = False
    if complete_onboarding:
        settings.onboarding_completed_at = settings.onboarding_completed_at or database_now()
    settings.updated_at = database_now()
    Preference.objects.filter(person=person).delete()
    Preference.objects.bulk_create([Preference(person=person, revision=settings.revision, **choice) for choice in clean])
    settings.save()
    event = SettingsEvent.objects.create(command=command, settings=settings, before=before, after=settings_data(settings))
    command.result = {"settings_event_id": event.pk, "revision": settings.revision}
    command.save(update_fields=["result"])
    return event, False


def permitted(person, category, scope_kind, subject_id, *, lock=False):
    if category not in CATEGORIES or scope_kind not in {"event", "section", "club"}:
        return False
    rows = ParticipantSettings.objects.select_for_update() if lock else ParticipantSettings.objects
    settings = rows.filter(person=person).first()
    if settings is None or settings.all_disabled or settings.verification_required or person.quarantine_incident:
        return False
    choices = { (row.scope_kind, row.subject_id): row.enabled for row in
               Preference.objects.filter(person=person, category=category, scope_kind__in=["club", scope_kind]) }
    global_choice, local_choice = choices.get(("club", 0)), choices.get((scope_kind, subject_id))
    if global_choice is False or local_choice is False:
        return False
    return global_choice is True or local_choice is True
