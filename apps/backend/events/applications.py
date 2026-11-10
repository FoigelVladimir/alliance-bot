from django.db import transaction
from django.db.models import F

from accounts.services import AccessError
from participants.models import Person, PersonOrigin
from participants.services import actor as participant_actor
from vk_channel.services import database_now

from .forms import digest, text, validate_values
from .models import Application, ApplicationPlayer, FormRevision
from .services import (
    before_start, get_event, number, record, registration_is_open, require_organiser,
    reserve_command, staff_actor,
)


def application_row(event, application_id, *, owner=None):
    number(application_id)
    application = Application.objects.select_for_update().filter(pk=application_id, event=event).first()
    if application is None or owner and application.owner_id != owner.pk:
        raise AccessError("application_not_found", 404)
    return application


def application_revision(application, expected):
    number(expected)
    if application.revision != expected:
        raise AccessError("application_revision_conflict", 409, current_application_revision=application.revision)


def submitted_for(event, owner):
    return Application.objects.filter(event=event, owner=owner, first_submitted_at__isnull=False).first()


def values_digest(application):
    return digest({"form_id": application.form_id, "schema": application.form.schema, "values": application.values})


def synchronize_players(application, values, *, create_people=False):
    previous = {str(player.pk): player for player in application.players.select_related("person")}
    if any(player.get("id") and player["id"] not in previous for player in values["players"]):
        raise AccessError("application_player_not_found", 422)
    application.players.filter(is_active=True).update(position=F("position") + 1000, is_active=False)
    result = []
    person_ids = set()
    for position, fields in enumerate(values["players"]):
        player = previous.get(fields.get("id"))
        if player is None:
            player = ApplicationPlayer(application=application)
        person = player.person if player.person_id else None
        if fields["is_self"]:
            if person and person.pk != application.owner_id and person.identity_id:
                raise AccessError("verified_person_data_conflict", 409)
            person = application.owner
        elif person and player.values.get("is_self"):
            # Removing a player and adding a different row preserves the old identity's provenance.
            raise AccessError("verified_person_data_conflict", 409)
        if person and person.data_verified_at and any(
                fields[key] != getattr(person, key) for key in ("student_number", "vk_url") if getattr(person, key)):
            raise AccessError("verified_person_data_conflict", 409)
        created_person = person is None and create_people
        if created_person:
            person = Person.objects.create(**{key: fields[key] for key in ("full_name", "student_number", "vk_url", "study_group")})
        if person and person.pk in person_ids:
            raise AccessError("duplicate_application_person", 422)
        if person:
            person_ids.add(person.pk)
        player.person, player.position, player.is_active = person, position, True
        player.values = {key: value for key, value in fields.items() if key != "id"}
        player.save()
        clean = {**player.values, "id": str(player.pk)}
        result.append(clean)
        if create_people and person and (created_person or person.pk == application.owner_id) and not PersonOrigin.objects.filter(kind="application_player", source_id=str(player.pk)).exists():
            PersonOrigin.objects.create(person=person, kind="application_player", source_id=str(player.pk), values=clean)
    return {**values, "players": result}


def save_values(application, clean, *, complete=False):
    application.values = synchronize_players(application, clean, create_people=complete)
    application.data_revision += 1
    application.revision += 1
    application.updated_at = database_now()
    application.validation_digest = values_digest(application) if complete else ""
    application.save()


@transaction.atomic
def open_draft(session, event_id, operation_id):
    owner = participant_actor(session, lock=True)
    event = get_event(event_id, lock=True)
    command, created = reserve_command(owner.identity.profile, operation_id, "open_draft", event_id, {}, owner=owner)
    if not created:
        return command, True
    submitted = submitted_for(event, owner)
    if submitted:
        command.result = {"application_id": str(submitted.pk), "already_submitted": True}
        command.save(update_fields=["result"])
        return command, False
    existing = Application.objects.filter(event=event, owner=owner, status="draft", participation_format=event.participation_format).first()
    if existing is None:
        if not registration_is_open(event) or event.current_form_id is None:
            raise AccessError("registration_closed", 409)
        existing = Application.objects.create(event=event, owner=owner, form=event.current_form,
                                               participation_format=event.participation_format)
    record(command, event, "draft_opened", {"application_id": existing.pk}, application=existing,
           result={"application_id": str(existing.pk)}, bump_event=False)
    return command, False


@transaction.atomic
def save_draft(session, event_id, application_id, operation_id, expected_revision, form_id, values):
    owner = participant_actor(session, lock=True)
    event = get_event(event_id, lock=True)
    application = application_row(event, application_id, owner=owner)
    number(form_id)
    command, created = reserve_command(owner.identity.profile, operation_id, "save_application_draft", event_id,
        {"application_id": application_id, "revision": expected_revision, "form_id": form_id, "values": values}, owner=owner)
    if not created:
        return command, True
    application_revision(application, expected_revision)
    before_start(event)
    if application.status != "draft":
        raise AccessError("application_state_conflict", 409)
    form = FormRevision.objects.filter(pk=form_id, event=event).first()
    if (form is None or form_id not in {application.form_id, event.current_form_id}
            or form.schema["participation_format"] != application.participation_format):
        raise AccessError("registration_form_changed", 409)
    clean = validate_values(values, form.schema)
    previous = application.values
    application.form = form
    save_values(application, clean)
    record(command, event, "draft_saved", {"before": previous, "after": application.values}, application=application,
           result={"application_id": str(application.pk), "application_revision": application.revision}, bump_event=False)
    return command, False


@transaction.atomic
def submit(session, event_id, application_id, operation_id, expected_revision, window_id, form_id):
    owner = participant_actor(session, lock=True)
    event = get_event(event_id, lock=True)
    application = application_row(event, application_id, owner=owner)
    number(form_id)
    command, created = reserve_command(owner.identity.profile, operation_id, "submit_application", event_id,
        {"application_id": application_id, "revision": expected_revision, "window_id": str(window_id), "form_id": form_id}, owner=owner)
    if not created:
        return command, True
    existing = submitted_for(event, owner)
    if existing:
        raise AccessError("application_already_submitted", 409, application_id=str(existing.pk))
    now = database_now()
    if not registration_is_open(event, now=now):
        raise AccessError("registration_closed", 409)
    if str(event.current_window_id) != str(window_id):
        raise AccessError("registration_window_changed", 409)
    if event.current_form_id != form_id or application.form_id != form_id:
        raise AccessError("registration_form_changed", 409)
    if application.status != "draft":
        raise AccessError("application_state_conflict", 409)
    application_revision(application, expected_revision)
    clean = validate_values(application.values, application.form.schema, complete=True)
    application.values = synchronize_players(application, clean, create_people=True)
    application.status, application.first_submitted_at, application.submission_window = "submitted", now, event.current_window
    application.validation_digest = values_digest(application)
    application.revision += 1
    application.updated_at = now
    application.save()
    record(command, event, "application_submitted", {"form_id": form_id, "window_id": str(window_id),
        "values": application.values, "validation_digest": application.validation_digest}, application=application,
        result={"application_id": str(application.pk), "application_revision": application.revision, "submitted_at": now.isoformat()}, bump_event=False)
    return command, False


def review_reasons(action, internal_reason, participant_explanation):
    if action not in {"admit", "request_changes", "reject", "reconsider"}:
        raise AccessError("invalid_request", 400)
    if (action == "admit" and (internal_reason is not None or participant_explanation is not None)
            or action == "request_changes" and internal_reason is not None
            or action == "reconsider" and participant_explanation is not None):
        raise AccessError("invalid_review_reason", 422)
    internal_reason = text(internal_reason, 2000).strip() or None if internal_reason is not None else None
    participant_explanation = text(participant_explanation, 2000).strip() or None if participant_explanation is not None else None
    if (action == "admit" and (internal_reason is not None or participant_explanation is not None)
            or action == "request_changes" and (not participant_explanation or internal_reason is not None)
            or action == "reject" and not internal_reason
            or action == "reconsider" and (not internal_reason or participant_explanation is not None)):
        raise AccessError("invalid_review_reason", 422)
    return internal_reason, participant_explanation


@transaction.atomic
def review(session, event_id, application_id, operation_id, expected_revision, action, *, internal_reason=None, participant_explanation=None):
    actor, _ = staff_actor(session)
    event = get_event(event_id, lock=True)
    require_organiser(event, actor)
    application = application_row(event, application_id)
    internal_reason, participant_explanation = review_reasons(action, internal_reason, participant_explanation)
    command, created = reserve_command(actor, operation_id, "review_application", event_id,
        {"application_id": application_id, "revision": expected_revision, "action": action,
         "internal_reason": internal_reason, "participant_explanation": participant_explanation})
    if not created:
        return command, True
    application_revision(application, expected_revision)
    before_start(event)
    previous = application.status
    if action == "reconsider":
        if previous not in {"rejected", "withdrawn", "admission_revoked"}:
            raise AccessError("application_state_conflict", 409)
        next_state = "submitted"
    else:
        if previous != "submitted":
            raise AccessError("application_state_conflict", 409)
        next_state = {"admit": "admitted", "reject": "rejected", "request_changes": "needs_changes"}[action]
    if action == "admit" and (not application.validation_digest or application.validation_digest != values_digest(application)):
        raise AccessError("application_validation_required", 409)
    application.status, application.participant_explanation = next_state, participant_explanation or ""
    application.revision += 1
    application.updated_at = database_now()
    application.save(update_fields=["status", "participant_explanation", "revision", "updated_at"])
    record(command, event, "application_" + action, {"before": previous, "after": next_state,
        "internal_reason": internal_reason, "participant_explanation": participant_explanation}, application=application,
        result={"application_id": str(application.pk), "application_revision": application.revision}, bump_event=False)
    return command, False


@transaction.atomic
def edit_application(session, event_id, application_id, operation_id, expected_revision, values, *, participant=False):
    if participant:
        owner = participant_actor(session, lock=True)
        actor = owner.identity.profile
    else:
        head = Application.objects.filter(pk=application_id, event_id=event_id).values("owner_id", "owner__identity__profile_id").first()
        actor, _ = staff_actor(session, *([head["owner__identity__profile_id"]] if head and head["owner__identity__profile_id"] else []))
        if head:
            Person.objects.select_for_update().filter(pk=head["owner_id"]).first()
        owner = None
    event = get_event(event_id, lock=True)
    if not participant:
        require_organiser(event, actor)
    application = application_row(event, application_id, owner=owner)
    if not participant and head and application.owner_id != head["owner_id"]:
        raise AccessError("application_revision_conflict", 409, current_application_revision=application.revision)
    action = "application_corrections" if participant else "application_edited"
    command, created = reserve_command(actor, operation_id, action, event_id,
        {"application_id": application_id, "revision": expected_revision, "values": values}, owner=owner)
    if not created:
        return command, True
    application_revision(application, expected_revision)
    before_start(event)
    if application.status not in ({"needs_changes"} if participant else {"submitted", "admitted"}):
        raise AccessError("application_state_conflict", 409)
    clean = validate_values(values, application.form.schema, complete=True)
    previous = application.values
    if participant:
        application.status, application.participant_explanation = "submitted", ""
    save_values(application, clean, complete=True)
    record(command, event, action, {"before": previous, "after": application.values,
        "validation_digest": application.validation_digest}, application=application,
        result={"application_id": str(application.pk), "application_revision": application.revision}, bump_event=False)
    return command, False


@transaction.atomic
def withdraw(session, event_id, application_id, operation_id, expected_revision, *, by_organiser=False, revoke=False, reason=None):
    if by_organiser:
        actor, _ = staff_actor(session)
        owner = None
    else:
        owner = participant_actor(session, lock=True)
        actor = owner.identity.profile
    event = get_event(event_id, lock=True)
    if by_organiser:
        require_organiser(event, actor)
    if revoke and not by_organiser:
        raise AccessError("permission_denied", 403)
    reason = text(reason, 2000, required=revoke).strip() or None if reason is not None else None
    if revoke and not reason:
        raise AccessError("invalid_review_reason", 422)
    application = application_row(event, application_id, owner=owner)
    action = "revoke_admission" if revoke else "withdraw_application"
    command, created = reserve_command(actor, operation_id, action, event_id,
        {"application_id": application_id, "revision": expected_revision, "reason": reason}, owner=owner)
    if not created:
        return command, True
    application_revision(application, expected_revision)
    allowed = {"admitted"} if event.started_at or revoke else {"submitted", "needs_changes", "admitted"}
    if application.status not in allowed or event.state not in {"planned", "started"}:
        raise AccessError("application_state_conflict", 409)
    previous = application.status
    application.status = "admission_revoked" if revoke else "withdrawn"
    application.revision += 1
    application.updated_at = database_now()
    application.save(update_fields=["status", "revision", "updated_at"])
    record(command, event, action, {"before": previous, "after": application.status, "internal_reason": reason,
        "reported_by_organiser": by_organiser}, application=application,
        result={"application_id": str(application.pk), "application_revision": application.revision}, bump_event=False)
    return command, False
