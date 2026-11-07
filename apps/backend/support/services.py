import hashlib
import json

from django.db import transaction
from django.utils import timezone

from accounts.policies import has_work_access, profile_from_session, quarantine_incident
from accounts.services import AccessError, lock_profiles
from jobs.models import BackgroundTask

from .models import (
    Dialog, Draft, InboundRoute, Message, OutgoingAttempt, OutgoingResponse,
    ReviewDecision, SupportCommand, SupportMembership, Ticket, TicketEvent,
)
from .queries import allowed_communities, can_edit_draft, draft_data, may_coordinate, open_ticket, ticket_data


def support_actor(session, *, lock=False):
    actor_id = session.get("staff_profile")
    locked = lock_profiles(actor_id).get(actor_id) if lock else None
    actor = None if lock and locked is None else profile_from_session(session, locked)
    if actor is None:
        raise AccessError("authentication_required", 401)
    if quarantine_incident() or not has_work_access(actor) or not allowed_communities(actor).exists():
        raise AccessError("permission_denied", 403)
    return actor


def visible_dialog(actor, dialog_id, *, missing="not_found"):
    dialog = Dialog.objects.filter(pk=dialog_id, cursor_id__in=allowed_communities(actor)).first()
    if dialog is None:
        raise AccessError(missing, 404)
    return dialog


def reserve_command(actor, operation_id, action, target_type, target_id, parameters):
    digest = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()
    expected = {
        "actor_id": actor.pk, "action": action, "target_type": target_type,
        "target_id": target_id, "request_fingerprint": digest,
    }
    command, created = SupportCommand.objects.get_or_create(
        client_operation_id=operation_id, defaults=expected,
    )
    if not created and any(getattr(command, key) != value for key, value in expected.items()):
        raise AccessError("operation_mismatch", 409)
    return command, created


@transaction.atomic
def grant_support_membership(profile_id, cursor_id, *, coordination=None):
    profile = lock_profiles(profile_id).get(profile_id)
    if quarantine_incident() or not has_work_access(profile) or profile.disabled or not profile.bindings.filter(active=True).exists():
        raise AccessError("permission_denied", 403)
    membership, created = SupportMembership.objects.get_or_create(
        profile=profile, cursor_id=cursor_id,
        defaults={"access_generation": profile.access_generation, "coordinates_support": bool(coordination)},
    )
    if not created and (membership.access_generation != profile.access_generation or coordination is not None):
        membership.access_generation = profile.access_generation
        membership.coordinates_support = bool(coordination)
        membership.save(update_fields=["access_generation", "coordinates_support"])
    return membership


@transaction.atomic
def review_inbound(session, route_id, operation_id, action, *, expected_ticket_id=None, reason=""):
    actor = support_actor(session, lock=True)
    dialog_id = InboundRoute.objects.filter(pk=route_id).values_list("dialog_id", flat=True).first()
    dialog = visible_dialog(actor, dialog_id)
    dialog = Dialog.objects.select_for_update().get(pk=dialog.pk)
    route = InboundRoute.objects.select_for_update().get(pk=route_id, dialog=dialog)
    command, created = reserve_command(actor, operation_id, action, "inbound_route", route.pk, {
        "expected_ticket_id": expected_ticket_id, "reason": reason,
    })
    if not created:
        return ReviewDecision.objects.get(command=command), True
    message = Message.objects.select_for_update().get(pk=route.message_id)
    if route.status != "needs_review" or message.ticket_id is not None or message.dialog_id != dialog.pk:
        raise AccessError("review_state_changed", 409)
    ticket = None
    if action == "route_to_support":
        ticket = open_ticket(dialog.pk)
        if (ticket.pk if ticket else None) != expected_ticket_id:
            raise AccessError("review_state_changed", 409)
        if ticket is None:
            ticket = Ticket.objects.create(dialog=dialog)
        message.ticket = ticket
        message.save(update_fields=["ticket"])
        if dialog.mode != "human_support":
            dialog.mode = "human_support"
            dialog.mode_version += 1
            dialog.save(update_fields=["mode", "mode_version", "updated_at"])
    route.status = "reviewed"
    route.save(update_fields=["status"])
    return ReviewDecision.objects.create(route=route, command=command, ticket=ticket, reason=reason), False


@transaction.atomic
def take_ticket(session, ticket_id, operation_id, version):
    actor = support_actor(session, lock=True)
    dialog_id = Ticket.objects.filter(pk=ticket_id).values_list("dialog_id", flat=True).first()
    visible_dialog(actor, dialog_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    command, created = reserve_command(actor, operation_id, "take", "ticket", ticket.pk, {
        "expected_assignment_version": version,
    })
    if not created:
        event = TicketEvent.objects.get(command=command)
        if (
            ticket.status != Ticket.Status.OPEN or ticket.current_owner_id != actor.pk
            or ticket.assignment_version != event.assignment_version
        ):
            raise AccessError("operation_state_changed", 409, ticket=ticket_data(ticket))
        return ticket, event.changed, True
    if ticket.status != Ticket.Status.OPEN:
        raise AccessError("ticket_closed", 409, ticket=ticket_data(ticket))
    if ticket.assignment_version != version:
        raise AccessError("assignment_version_conflict", 409, ticket=ticket_data(ticket))
    if ticket.current_owner_id not in (None, actor.pk):
        raise AccessError("ticket_already_assigned", 409, ticket=ticket_data(ticket))
    changed = ticket.current_owner_id is None
    if changed:
        ticket.current_owner = actor
        ticket.assignment_version += 1
        ticket.save(update_fields=["current_owner", "assignment_version"])
    TicketEvent.objects.create(
        ticket=ticket, command=command, assignment_version=ticket.assignment_version, changed=changed,
        previous_owner_id=None if changed else actor.pk, new_owner_id=actor.pk,
    )
    return ticket, changed, False



@transaction.atomic
def save_draft(session, ticket_id, version, revision, text):
    actor = support_actor(session, lock=True)
    ticket = Ticket.objects.select_for_update().filter(
        pk=ticket_id, dialog__cursor_id__in=allowed_communities(actor),
    ).first()
    if ticket is None:
        raise AccessError("not_found", 404)
    if not can_edit_draft(ticket, actor) or ticket.assignment_version != version:
        raise AccessError("assignment_version_conflict", 409)

    draft = Draft.objects.select_for_update().filter(ticket=ticket, author=actor).first()
    if (draft.revision if draft else 0) != revision:
        raise AccessError("draft_revision_conflict", 409, draft=draft_data(draft), draft_revision=str(draft.revision if draft else 0))
    if draft is None:
        return Draft.objects.create(ticket=ticket, author=actor, text=text)
    draft.text = text
    draft.revision += 1
    draft.updated_at = timezone.now()
    draft.save(update_fields=["text", "revision", "updated_at"])
    return draft



def cancel_stale_responses(ticket):
    responses = list(OutgoingResponse.objects.select_for_update().filter(
        ticket=ticket, assignment_version__lt=ticket.assignment_version,
        status__in=[OutgoingResponse.Status.PENDING, OutgoingResponse.Status.FAILED],
    ).order_by("pk"))
    by_response = {}
    attempts = OutgoingAttempt.objects.select_for_update().filter(response__in=responses).order_by("pk")
    for attempt in attempts:
        by_response.setdefault(attempt.response_id, []).append(attempt)

    now = timezone.now()
    for response in responses:
        relevant = by_response.get(response.pk, ())
        if any(
            attempt.status == OutgoingAttempt.Status.UNKNOWN
            or (attempt.status == OutgoingAttempt.Status.IN_FLIGHT and attempt.started_at is not None)
            for attempt in relevant
        ):
            continue
        response.status = OutgoingResponse.Status.CANCELLED
        response.failure_reason = "assignment_changed"
        response.save(update_fields=["status", "failure_reason"])
        for attempt in relevant:
            if attempt.status != OutgoingAttempt.Status.IN_FLIGHT:
                continue
            cancel_unstarted_attempt(attempt, "assignment_changed", now)


def cancel_unstarted_attempt(attempt, reason, now):
    attempt.status = OutgoingAttempt.Status.FAILED
    attempt.finished_at = now
    attempt.failure_reason = reason
    attempt.save(update_fields=["status", "finished_at", "failure_reason"])
    BackgroundTask.objects.filter(pk=attempt.task_id).update(
        status=BackgroundTask.Status.FAILED, lease_token=None, locked_until=None, updated_at=now,
        result={"reason": reason},
    )


@transaction.atomic
def change_assignment(session, ticket_id, operation_id, version, action, target_owner_id=None):
    transferring = action == "reassign_coordinator"
    profiles = lock_profiles(session.get("staff_profile"), target_owner_id)
    actor = support_actor(session)
    dialog_id = Ticket.objects.filter(pk=ticket_id).values_list("dialog_id", flat=True).first()
    dialog = visible_dialog(actor, dialog_id, missing="ticket_not_found" if transferring else "not_found")
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    returning = action == "return_to_queue"
    parameters = {"expected_assignment_version": version}
    if transferring:
        if not may_coordinate(actor, dialog.cursor_id):
            raise AccessError("permission_denied", 403)
        parameters["target_owner_id"] = target_owner_id
    command, created = reserve_command(actor, operation_id, action, "ticket", ticket.pk, parameters)
    if not created:
        event = TicketEvent.objects.get(command=command)
        if returning and event.previous_owner_id != actor.pk and not may_coordinate(actor, dialog.cursor_id):
            raise AccessError("permission_denied", 403)
        if (
            ticket.status != Ticket.Status.OPEN or ticket.assignment_version != event.assignment_version
            or ticket.current_owner_id != event.new_owner_id
        ):
            raise AccessError("operation_state_changed", 409, ticket=ticket_data(ticket))
        return ticket, True
    if ticket.status != Ticket.Status.OPEN:
        raise AccessError("ticket_closed", 409, ticket=ticket_data(ticket))
    if ticket.assignment_version != version:
        raise AccessError("assignment_version_conflict", 409, ticket=ticket_data(ticket))
    previous = ticket.current_owner_id
    if previous is None:
        raise AccessError("ticket_unassigned", 409, ticket=ticket_data(ticket))
    if returning:
        if previous != actor.pk and not may_coordinate(actor, dialog.cursor_id):
            raise AccessError("permission_denied", 403)
        ticket.current_owner = None
        ticket.importance = "high"
    elif transferring:
        if target_owner_id in (actor.pk, previous):
            raise AccessError("invalid_target_owner", 422)
        target = profiles.get(target_owner_id)
        if (
            not has_work_access(target) or target.disabled
            or not target.bindings.filter(active=True).exists()
            or dialog.cursor_id not in allowed_communities(target)
        ):
            raise AccessError("target_owner_unavailable", 409)
        ticket.current_owner = target
    else:
        if previous == actor.pk:
            raise AccessError("already_owner", 409, ticket=ticket_data(ticket))
        ticket.current_owner = actor
    ticket.assignment_version += 1
    ticket.save(update_fields=["current_owner", "assignment_version", "importance"])
    cancel_stale_responses(ticket)
    TicketEvent.objects.create(
        ticket=ticket, command=command, assignment_version=ticket.assignment_version,
        previous_owner_id=previous, new_owner_id=ticket.current_owner_id,
    )
    return ticket, False
