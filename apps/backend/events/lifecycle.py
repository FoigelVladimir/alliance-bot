from django.db import transaction

from accounts.models import StaffProfile
from accounts.services import AccessError
from vk_channel.services import database_now

from .forms import digest, text
from .models import Event, Participation, StartSnapshot
from .services import before_start, get_event, number, record, require_organiser, reserve_command, revision, staff_actor


def entries_for_start(event):
    result, pending = [], []
    applications = event.applications.filter(first_submitted_at__isnull=False).select_related("form").order_by("pk")
    versions = []
    for application in applications:
        players = [{"id": str(player.pk), "person_id": str(player.person_id) if player.person_id else None,
                    "position": player.position, "values": player.values}
                   for player in application.players.filter(is_active=True).order_by("position", "pk")]
        versions.append({"id": application.pk, "revision": application.revision, "status": application.status,
                         "validation_digest": application.validation_digest, "values": application.values, "players": players})
        if application.status == "admitted":
            result.append({"application_id": str(application.pk), "owner_id": str(application.owner_id),
                "form_id": str(application.form_id), "application_revision": application.revision,
                "name": application.values.get("team_name", "") or (players[0]["values"].get("full_name", "") if players else ""),
                "category": application.values.get("category", ""), "players": players})
        elif application.status in {"submitted", "needs_changes"}:
            pending.append({"application_id": str(application.pk), "status": application.status,
                            "revision": application.revision, "name": application.values.get("team_name", "")})
    return result, pending, versions


def start_data(event):
    entries, pending, versions = entries_for_start(event)
    payload = {"event_revision": event.revision, "applications": versions}
    return {"start_preview_version": digest(payload), "event_revision": event.revision, "entries": entries,
            "pending_applications": pending, "tournament_structure_status": "regulation_required"}


@transaction.atomic
def start_preview(session, event_id):
    actor, _ = staff_actor(session)
    event = get_event(event_id, lock=True)
    require_organiser(event, actor)
    return start_data(event)


@transaction.atomic
def start_event(session, event_id, operation_id, expected_revision, preview_version, *, confirm_pending=False):
    if type(confirm_pending) is not bool or not isinstance(preview_version, str):
        raise AccessError("invalid_request", 400)
    actor, _ = staff_actor(session)
    event = get_event(event_id, lock=True)
    require_organiser(event, actor)
    command, created = reserve_command(actor, operation_id, "start_event", event_id,
        {"revision": expected_revision, "preview": preview_version, "confirm_pending": confirm_pending})
    if not created:
        return command, True
    existing = StartSnapshot.objects.filter(event=event).first()
    if existing:
        command.result = {"snapshot_id": str(existing.pk), "already_started": True}
        command.save(update_fields=["result"])
        return command, False
    revision(event, expected_revision)
    before_start(event)
    preview = start_data(event)
    if preview["start_preview_version"] != preview_version:
        raise AccessError("event_start_preview_changed", 409)
    if preview["pending_applications"] and not confirm_pending:
        raise AccessError("pending_applications_confirmation_required", 409)
    now = database_now()
    snapshot = StartSnapshot.objects.create(event=event, command=command, entries=preview["entries"],
                                            pending_applications=preview["pending_applications"], created_at=now)
    Participation.objects.bulk_create([Participation(snapshot=snapshot, application_id=entry["application_id"])
                                      for entry in preview["entries"]])
    event.state, event.started_at = "started", now
    record(command, event, "started", {"snapshot_id": snapshot.pk, "pending_applications": preview["pending_applications"]},
           result={"snapshot_id": str(snapshot.pk)})
    return command, False


def completion_data(event):
    snapshot = StartSnapshot.objects.filter(event=event).first()
    marks = list(Participation.objects.filter(snapshot=snapshot).order_by("pk").values("id", "application_id", "mark", "revision")) if snapshot else []
    gaps = {"participation_unmarked": [str(mark["id"]) for mark in marks if mark["mark"] == "unmarked"],
            "results_status": "regulation_required"}
    version = digest({"event_revision": event.revision, "marks": marks, "gaps": gaps})
    return {"preview_version": version, "event_revision": event.revision,
            "snapshot_id": str(snapshot.pk) if snapshot else None, "participation": marks, "gaps": gaps}


@transaction.atomic
def completion_preview(session, event_id):
    actor, _ = staff_actor(session)
    event = get_event(event_id, lock=True)
    require_organiser(event, actor)
    return completion_data(event)


@transaction.atomic
def finish_event(session, event_id, operation_id, expected_revision, preview_version, *, interrupt=False, reason=None):
    if interrupt:
        reason = text(reason, 2000, required=True).strip()
    elif reason is not None:
        raise AccessError("invalid_request", 400)
    actor, _ = staff_actor(session)
    event = get_event(event_id, lock=True)
    require_organiser(event, actor)
    action = "interrupt_event" if interrupt else "finish_event"
    command, created = reserve_command(actor, operation_id, action, event_id,
        {"revision": expected_revision, "preview": preview_version, "reason": reason})
    if not created:
        return command, True
    revision(event, expected_revision)
    if event.state != "started" or event.started_at is None:
        raise AccessError("event_state_conflict", 409)
    preview = completion_data(event)
    if preview["preview_version"] != preview_version:
        raise AccessError("event_interruption_preview_changed" if interrupt else "event_finish_preview_changed", 409)
    event.state, event.finished_at = "stopped" if interrupt else "completed", database_now()
    record(command, event, "interrupted" if interrupt else "completed", {"reason": reason, "gaps": preview["gaps"]})
    return command, False


@transaction.atomic
def mark_participation(session, event_id, mark_id, operation_id, expected_revision, mark, note=None):
    number(mark_id)
    number(expected_revision)
    if mark not in {"unmarked", "participated", "not_participated"}:
        raise AccessError("invalid_participation_mark", 422)
    note = text(note, 2000).strip() or None if note is not None else None
    actor, _ = staff_actor(session)
    event = get_event(event_id, lock=True)
    require_organiser(event, actor)
    command, created = reserve_command(actor, operation_id, "mark_participation", event_id,
        {"mark_id": mark_id, "revision": expected_revision, "mark": mark, "note": note})
    if not created:
        return command, True
    if event.state not in {"started", "completed", "stopped"}:
        raise AccessError("event_state_conflict", 409)
    item = Participation.objects.select_for_update().filter(pk=mark_id, snapshot__event=event).first()
    if item is None:
        raise AccessError("participation_not_found", 404)
    if item.revision != expected_revision:
        raise AccessError("participation_revision_conflict", 409, current_participation_revision=item.revision)
    previous = item.mark
    item.mark, item.revision, item.changed_by, item.changed_at = mark, item.revision + 1, actor, database_now()
    item.save(update_fields=["mark", "revision", "changed_by", "changed_at"])
    record(command, event, "participation_marked", {"mark_id": mark_id, "before": previous, "after": mark,
                                                   "correction_note": note, "archive_correction": event.state != "started"},
           result={"participation_revision": item.revision}, bump_event=False)
    return command, False


def reporting_data(event, action, exclusion_kind, primary):
    current = completion_data(event)
    intent = {"action": action, "exclusion_kind": exclusion_kind, "primary_id": primary.pk if primary else None}
    version = digest({"intent": intent, "event": current, "excluded": event.reporting_excluded,
        "primary": completion_data(primary) if primary else None, "primary_excluded": primary.reporting_excluded if primary else None})
    return {**current, "reporting_preview_version": version, **intent,
            "conducted_delta": (-1 if action == "exclude" else 1),
            "participated_delta": sum(mark["mark"] == "participated" for mark in current["participation"]) * (-1 if action == "exclude" else 1)}


def reporting_context(session, event_id, action, exclusion_kind, primary_id, *, check_state=True):
    actor, _ = staff_actor(session)
    if actor.role != StaffProfile.Role.LEADER:
        raise AccessError("permission_denied", 403)
    if action not in {"exclude", "restore"}:
        raise AccessError("invalid_request", 400)
    head = Event.objects.filter(pk=event_id).first()
    if head is None:
        raise AccessError("event_not_found", 404)
    if action == "restore":
        primary_id = head.reporting_primary_id
        exclusion_kind = head.reporting_exclusion_kind
    if action == "exclude" and exclusion_kind not in {"error", "duplicate"}:
        raise AccessError("invalid_request", 400)
    if exclusion_kind == "duplicate":
        number(primary_id)
        if primary_id == event_id:
            raise AccessError("invalid_reporting_primary", 422)
    elif primary_id is not None:
        raise AccessError("invalid_request", 400)
    locked = {item.pk: item for item in Event.objects.select_for_update().filter(pk__in=[event_id, primary_id] if primary_id else [event_id]).order_by("pk")}
    event = get_event(event_id)
    primary = get_event(primary_id) if primary_id else None
    if check_state and primary and (primary.state != "completed" or primary.reporting_excluded):
        raise AccessError("invalid_reporting_primary", 409)
    if check_state and event.state != "completed":
        raise AccessError("event_state_conflict", 409)
    if action == "restore" and event.reporting_primary_id != primary_id:
        raise AccessError("reporting_preview_changed", 409)
    return actor, event, primary, exclusion_kind


@transaction.atomic
def reporting_preview(session, event_id, action, exclusion_kind=None, primary_id=None):
    actor, event, primary, kind = reporting_context(session, event_id, action, exclusion_kind, primary_id)
    return reporting_data(event, action, kind, primary)


@transaction.atomic
def change_reporting_status(session, event_id, operation_id, action, preview_version, reason, *, exclusion_kind=None, primary_id=None):
    reason = text(reason, 2000, required=True).strip()
    actor, event, primary, kind = reporting_context(session, event_id, action, exclusion_kind, primary_id, check_state=False)
    command, created = reserve_command(actor, operation_id, "reporting_status", event_id,
        {"action": action, "preview": preview_version, "reason": reason,
         "exclusion_kind": exclusion_kind, "primary_id": primary_id})
    if not created:
        return command, True
    if event.state != "completed":
        raise AccessError("event_state_conflict", 409)
    if primary and (primary.state != "completed" or primary.reporting_excluded):
        raise AccessError("invalid_reporting_primary", 409)
    if event.reporting_excluded == (action == "exclude"):
        raise AccessError("event_state_conflict", 409)
    if action == "exclude" and event.excluded_duplicates.filter(reporting_excluded=True).exists():
        raise AccessError("reporting_primary_in_use", 409)
    preview = reporting_data(event, action, kind, primary)
    if preview["reporting_preview_version"] != preview_version:
        raise AccessError("reporting_preview_changed", 409)
    previous = {"excluded": event.reporting_excluded, "kind": event.reporting_exclusion_kind,
                "primary_id": event.reporting_primary_id}
    event.reporting_excluded = action == "exclude"
    event.reporting_exclusion_kind = kind if action == "exclude" else ""
    event.reporting_primary = primary if action == "exclude" else None
    event.reporting_reason = reason
    record(command, event, "reporting_" + action, {"before": previous,
        "after": {"excluded": event.reporting_excluded, "kind": event.reporting_exclusion_kind, "primary_id": event.reporting_primary_id}, "reason": reason})
    return command, False
