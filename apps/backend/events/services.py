from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.conf import settings

from accounts.models import StaffProfile
from accounts.policies import has_work_access, profile_from_session, quarantine_incident
from accounts.services import AccessError, lock_profiles
from vk_channel.services import database_now

from .forms import compatible, digest, schema_for, text, validate_schema
from .models import Application, Event, EventCommand, EventHistory, FormRevision, HelperAssignment, RegistrationWindow


def number(value, *, minimum=1):
    if type(value) is not int or not minimum <= value < 2**63:
        raise AccessError("invalid_request", 400)
    return value


def staff_actor(session, *other_profiles):
    actor_id = session.get("staff_profile")
    profiles = lock_profiles(actor_id, *other_profiles)
    actor = profile_from_session(session, profiles.get(actor_id))
    if actor is None:
        raise AccessError("authentication_required", 401)
    if quarantine_incident() or not eligible(actor):
        raise AccessError("permission_denied", 403)
    return actor, profiles


def eligible(profile):
    return (profile is not None and has_work_access(profile) and not profile.disabled
            and profile.role in {StaffProfile.Role.MANAGER, StaffProfile.Role.ADMINISTRATOR, StaffProfile.Role.LEADER}
            and profile.bindings.filter(active=True).exists())


def organiser(event, actor):
    if not eligible(actor):
        return False
    if event.responsible_id == actor.pk and event.responsible_generation == actor.access_generation:
        return True
    return HelperAssignment.objects.filter(event=event, profile=actor, ended_at__isnull=True,
                                            access_generation=actor.access_generation).exists()


def require_organiser(event, actor):
    if not organiser(event, actor):
        raise AccessError("event_organiser_required", 403)


def require_responsible(event, actor):
    if event.responsible_id != actor.pk or event.responsible_generation != actor.access_generation:
        raise AccessError("event_responsible_required", 403)


def get_event(event_id, *, lock=False, allow_deleted=False):
    if quarantine_incident():
        raise AccessError("event_temporarily_unavailable", 503)
    number(event_id)
    events = Event.objects.select_for_update() if lock else Event.objects
    event = events.filter(pk=event_id).first()
    if event is None or (event.deleted_at and not allow_deleted):
        raise AccessError("event_not_found", 404)
    if event.restoration_verification_required or event.deletion_pending:
        raise AccessError("event_temporarily_unavailable", 503)
    return event


def revision(event, expected):
    number(expected)
    if event.revision != expected:
        raise AccessError("event_revision_conflict", 409, current_event_revision=event.revision)


def before_start(event):
    if event.state != "planned" or event.started_at is not None or event.cancelled_at or event.deleted_at:
        raise AccessError("event_state_conflict", 409)


def reserve_command(actor, operation_id, action, event_id, parameters, *, owner=None):
    fingerprint = digest({"action": action, "event_id": event_id, "parameters": parameters})
    if owner is None:
        from .models import DeletionIntent
        deletion = DeletionIntent.objects.filter(environment=settings.APP_ENVIRONMENT_ID, actor_uuid=actor.stable_id,
                                                  client_operation_id=operation_id).first()
        if deletion and (action != "delete_event" or fingerprint != deletion.request_fingerprint):
            raise AccessError("operation_mismatch", 409)
    scope = {"owner": owner} if owner else {"actor": actor, "owner": None}
    command, created = EventCommand.objects.get_or_create(
        **scope, client_operation_id=operation_id,
        defaults={"actor": actor, "action": action, "fingerprint": fingerprint, "event_id": event_id},
    )
    if not created and (command.action != action or command.fingerprint != fingerprint):
        raise AccessError("operation_mismatch", 409)
    return command, created


def record(command, event, action, payload, *, application=None, result=None, bump_event=True):
    if bump_event:
        event.revision += 1
        event.save()
    history = EventHistory.objects.create(command=command, event=event, actor=command.actor,
                                          application=application, action=action, payload=payload)
    command.event, command.result = event, {"history_id": str(history.pk), **(result or {})}
    command.save(update_fields=["event", "result"])
    return command


def card_values(values):
    required = {"title", "sport", "participation_format", "planned_start_at", "timezone_name", "location"}
    if not isinstance(values, dict) or not required <= set(values) <= required | {"description"}:
        raise AccessError("invalid_event_data", 422)
    clean = {key: text(values.get(key, ""), limit, required=key != "description") for key, limit in
             (("title", 200), ("sport", 100), ("location", 500), ("description", 20_000), ("timezone_name", 64))}
    try:
        ZoneInfo(clean["timezone_name"])
    except (ZoneInfoNotFoundError, ValueError):
        raise AccessError("invalid_event_timezone", 422) from None
    instant = values["planned_start_at"]
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise AccessError("invalid_event_datetime", 422)
    if values["participation_format"] not in {"team", "individual"}:
        raise AccessError("invalid_participation_format", 422)
    return {**clean, "planned_start_at": instant, "participation_format": values["participation_format"]}


def fingerprint_card(values):
    return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in values.items()}


@transaction.atomic
def create_event(session, operation_id, responsible_id, values):
    number(responsible_id)
    actor, profiles = staff_actor(session, responsible_id)
    clean = card_values(values)
    command, created = reserve_command(actor, operation_id, "create_event", None,
                                       {"responsible_profile_id": responsible_id, "card": fingerprint_card(clean)})
    if not created:
        return command, True
    responsible = profiles.get(responsible_id)
    if not eligible(responsible):
        raise AccessError("responsible_not_eligible", 409)
    event = Event.objects.create(**clean, responsible=responsible, responsible_generation=responsible.access_generation, created_by=actor)
    form = FormRevision.objects.create(event=event, revision=1, schema=schema_for(event.participation_format), created_by=actor)
    record(command, event, "created", {"creator_id": actor.pk, "responsible_id": responsible_id,
                                      "responsible_generation": responsible.access_generation},
           result={"event_id": str(event.pk), "form_revision_id": str(form.pk)}, bump_event=False)
    return command, False


@transaction.atomic
def save_card(session, event_id, operation_id, expected_revision, values):
    actor, _ = staff_actor(session)
    event = get_event(event_id, lock=True)
    clean = card_values(values)
    command, created = reserve_command(actor, operation_id, "save_card", event_id,
                                       {"revision": expected_revision, "card": fingerprint_card(clean)})
    if not created:
        return command, True
    revision(event, expected_revision)
    before_start(event)
    if clean["participation_format"] != event.participation_format:
        raise AccessError("event_format_requires_form_revision", 409)
    previous = fingerprint_card({key: getattr(event, key) for key in clean})
    for key, value in clean.items():
        setattr(event, key, value)
    record(command, event, "card_updated", {"before": previous, "after": fingerprint_card(clean)})
    return command, False


@transaction.atomic
def change_helper(session, event_id, operation_id, expected_revision, profile_id, action):
    number(profile_id)
    if action not in {"add", "remove"}:
        raise AccessError("invalid_request", 400)
    actor, profiles = staff_actor(session, profile_id)
    event = get_event(event_id, lock=True)
    command, created = reserve_command(actor, operation_id, "helper", event_id,
                                       {"revision": expected_revision, "profile_id": profile_id, "action": action})
    if not created:
        return command, True
    require_responsible(event, actor)
    revision(event, expected_revision)
    current = HelperAssignment.objects.filter(event=event, profile_id=profile_id, ended_at__isnull=True).first()
    if action == "add":
        candidate = profiles.get(profile_id)
        if not eligible(candidate) or profile_id == event.responsible_id:
            raise AccessError("helper_not_eligible", 409)
        if current and current.access_generation == candidate.access_generation:
            raise AccessError("helper_unchanged", 409)
        if current:
            current.ended_at = database_now()
            current.save(update_fields=["ended_at"])
        assignment = HelperAssignment.objects.create(event=event, profile=candidate, access_generation=candidate.access_generation, assigned_by=actor)
    else:
        if current is None:
            raise AccessError("helper_unchanged", 409)
        current.ended_at = database_now()
        current.save(update_fields=["ended_at"])
        assignment = current
    record(command, event, "helper_" + action, {"profile_id": profile_id, "assignment_id": assignment.pk,
                                                "generation": assignment.access_generation})
    return command, False


@transaction.atomic
def change_responsible(session, event_id, operation_id, expected_revision, profile_id, reason=None):
    number(profile_id)
    if reason is not None:
        reason = text(reason, 1000).strip() or None
    actor, profiles = staff_actor(session, profile_id)
    event = get_event(event_id, lock=True)
    command, created = reserve_command(actor, operation_id, "responsible", event_id,
                                       {"revision": expected_revision, "profile_id": profile_id, "reason": reason})
    if not created:
        return command, True
    current = event.responsible_id == actor.pk and event.responsible_generation == actor.access_generation
    if not current and (actor.role != StaffProfile.Role.LEADER or not reason):
        raise AccessError("event_responsible_required", 403)
    revision(event, expected_revision)
    candidate = profiles.get(profile_id)
    if not eligible(candidate):
        raise AccessError("responsible_not_eligible", 409)
    if event.responsible_id == profile_id and event.responsible_generation == candidate.access_generation:
        raise AccessError("responsible_unchanged", 409)
    previous = {"profile_id": event.responsible_id, "generation": event.responsible_generation}
    event.responsible, event.responsible_generation = candidate, candidate.access_generation
    HelperAssignment.objects.filter(event=event, profile=candidate, ended_at__isnull=True).update(ended_at=database_now())
    record(command, event, "responsible_changed", {"before": previous,
        "after": {"profile_id": profile_id, "generation": candidate.access_generation}, "replacement_reason": reason})
    return command, False


@transaction.atomic
def prepare_form(session, event_id, operation_id, expected_revision, schema):
    actor, _ = staff_actor(session)
    event = get_event(event_id, lock=True)
    clean = validate_schema(schema)
    command, created = reserve_command(actor, operation_id, "prepare_form", event_id, {"revision": expected_revision, "schema": clean})
    if not created:
        return command, True
    revision(event, expected_revision)
    before_start(event)
    latest = event.forms.order_by("-revision").values_list("revision", flat=True).first() or 0
    form = FormRevision.objects.create(event=event, revision=latest + 1, schema=clean, created_by=actor)
    record(command, event, "form_prepared", {"form_revision_id": form.pk}, result={"form_revision_id": str(form.pk)})
    return command, False


def publish_form_row(event, form):
    if event.applications.filter(first_submitted_at__isnull=False).exists():
        if event.current_form_id is None or not compatible(event.current_form.schema, form.schema):
            raise AccessError("registration_form_incompatible", 409)
    if form.published_at is None:
        form.published_at = database_now()
        form.save(update_fields=["published_at"])
    event.current_form = form
    event.participation_format = form.schema["participation_format"]


@transaction.atomic
def publish_form(session, event_id, operation_id, expected_revision, form_id):
    actor, _ = staff_actor(session)
    event = get_event(event_id, lock=True)
    number(form_id)
    command, created = reserve_command(actor, operation_id, "publish_form", event_id, {"revision": expected_revision, "form_id": form_id})
    if not created:
        return command, True
    revision(event, expected_revision)
    before_start(event)
    if event.current_window_id is None:
        raise AccessError("registration_not_opened", 409)
    form = FormRevision.objects.filter(pk=form_id, event=event).first()
    if form is None:
        raise AccessError("form_revision_not_found", 404)
    previous = event.current_form_id
    publish_form_row(event, form)
    record(command, event, "form_published", {"previous_form_id": previous, "form_revision_id": form_id})
    return command, False


def registration_is_open(event, *, now=None):
    now = database_now() if now is None else now
    return (event.state == "planned" and not event.cancelled_at and not event.deleted_at
            and not event.deletion_pending and not event.restoration_verification_required
            and event.current_window_id is not None and event.current_window.deadline_at > now)


@transaction.atomic
def registration_window(session, event_id, operation_id, expected_revision, action, deadline, *, form_id=None):
    if action not in {"open", "reopen", "deadline"}:
        raise AccessError("invalid_request", 400)
    if not isinstance(deadline, datetime) or deadline.tzinfo is None or deadline.utcoffset() is None:
        raise AccessError("invalid_event_datetime", 422)
    actor, _ = staff_actor(session)
    event = get_event(event_id, lock=True)
    command, created = reserve_command(actor, operation_id, "registration_" + action, event_id,
        {"revision": expected_revision, "deadline": deadline.isoformat(), "form_id": form_id})
    if not created:
        return command, True
    revision(event, expected_revision)
    before_start(event)
    now = database_now()
    if deadline <= now:
        raise AccessError("registration_deadline_invalid", 422)
    old = event.current_window
    if action == "open":
        if old:
            raise AccessError("registration_state_conflict", 409)
        number(form_id)
        form = FormRevision.objects.filter(pk=form_id, event=event).first()
        if form is None:
            raise AccessError("form_revision_not_found", 404)
        publish_form_row(event, form)
    elif action == "reopen":
        if old is None or old.deadline_at > now:
            raise AccessError("registration_state_conflict", 409)
        if event.current_form_id is None:
            raise AccessError("form_revision_not_found", 404)
    elif old is None or old.deadline_at <= now:
        raise AccessError("registration_state_conflict", 409)
    if action == "deadline":
        previous = old.deadline_at
        old.deadline_at = deadline
        old.save(update_fields=["deadline_at"])
    else:
        previous = old.deadline_at if old else None
        event.current_window = RegistrationWindow.objects.create(event=event, deadline_at=deadline, opened_by=actor, opened_at=now)
    record(command, event, "registration_" + action, {"previous_deadline_at": previous.isoformat() if previous else None,
        "deadline_at": deadline.isoformat(), "registration_window_id": str(event.current_window_id), "form_revision_id": event.current_form_id})
    return command, False
