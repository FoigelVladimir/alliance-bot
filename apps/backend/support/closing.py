from hashlib import sha256
import json

from django.core import signing
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from accounts.services import AccessError

from .models import Dialog, Draft, Message, OutgoingAttempt as Attempt, OutgoingResponse as Response, Ticket, TicketEvent
from .queries import ticket_data
from .services import cancel_unstarted_attempt, reserve_command, support_actor, visible_dialog

SALT = "support.close-warning.v1"


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def snapshot_for_close(dialog, ticket, actor_id, responses, attempts):
    draft = Draft.objects.select_for_update().filter(ticket=ticket, author_id=actor_id).exclude(text="").first()
    incoming_id = Message.objects.filter(dialog=dialog, direction="inbound").aggregate(latest=Max("pk"))["latest"]
    return {
        "ticket_status": ticket.status, "owner_id": ticket.current_owner_id, "assignment_version": ticket.assignment_version,
        "dialog_mode": dialog.mode, "dialog_mode_version": dialog.mode_version, "latest_inbound_id": incoming_id,
        "responses": [
            {
                "id": item.pk, "author_id": item.command.actor_id, "assignment_version": item.assignment_version,
                "status": item.status, "failure_reason": item.failure_reason, "message_id": item.message_id,
                "recovery_verification_required": item.recovery_verification_required,
                "active_recovery_guard_id": item.recovery_guards.filter(resolved_at__isnull=True).values_list("pk", flat=True).first(),
            } for item in responses
        ],
        "attempts": [
            {
                "id": item.pk, "response_id": item.response_id, "type": item.attempt_type,
                "status": item.status, "vk_error_code": item.vk_error_code,
                "started_at": item.started_at.isoformat() if item.started_at else None,
                "finished_at": item.finished_at.isoformat() if item.finished_at else None,
            } for item in attempts
        ],
        "draft": {"id": draft.pk, "author_id": draft.author_id, "revision": draft.revision} if draft else None,
    }


def warning_for(snapshot, version):
    failed = {item["id"] for item in snapshot["responses"]
              if item["status"] == Response.Status.FAILED and item["assignment_version"] == version
              and not item["recovery_verification_required"]}
    running = {item["response_id"] for item in snapshot["attempts"] if item["status"] == Attempt.Status.IN_FLIGHT}
    unknown = {item["response_id"] for item in snapshot["attempts"] if item["status"] == Attempt.Status.UNKNOWN}
    unknown.update(item["id"] for item in snapshot["responses"]
                   if item["status"] == Response.Status.UNKNOWN or item["recovery_verification_required"])
    if not (failed or running or unknown or snapshot["draft"]):
        return None
    return {
        "has_failed_responses": bool(failed), "has_in_flight_attempts": bool(running),
        "has_unknown_outcome": bool(unknown), "has_uncommitted_draft": snapshot["draft"] is not None,
        "has_waiting_attempts": any(
            item["status"] == Attempt.Status.IN_FLIGHT and item["started_at"] is None for item in snapshot["attempts"]
        ),
        "has_started_attempts": any(
            item["status"] == Attempt.Status.IN_FLIGHT and item["started_at"] is not None for item in snapshot["attempts"]
        ),
        "responses": [
            {"id": str(item["id"]), "status": item["status"],
             "recovery_verification_required": item["recovery_verification_required"]}
            for item in snapshot["responses"] if item["id"] in failed | running | unknown
        ],
    }


def load_token(token):
    if not token:
        return None
    try:
        result = signing.loads(token, salt=SALT)
    except (signing.BadSignature, ValueError, TypeError):
        return None
    return result if isinstance(result, dict) else None


@transaction.atomic
def close_ticket(session, ticket_id, operation_id, version, confirm, token):
    actor = support_actor(session, lock=True)
    dialog_id = Ticket.objects.filter(pk=ticket_id).values_list("dialog_id", flat=True).first()
    visible_dialog(actor, dialog_id)
    dialog = Dialog.objects.select_for_update().get(pk=dialog_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    intent = {"expected_assignment_version": version, "confirm_warning": confirm}
    binding = {
        "purpose": "close_support_ticket", "schema_version": 1,
        "actor_id": str(actor.pk), "ticket_id": str(ticket.pk), "assignment_version": version,
    }
    proof = load_token(token) if confirm else None
    existing = TicketEvent.objects.filter(
        command__client_operation_id=operation_id, command__action="close",
        command__actor=actor, ticket=ticket,
    ).first()
    if existing is not None and confirm and proof is None and token:
        previous = existing.payload.get("warning_confirmation", {})
        if digest(token) == previous.get("token_digest"):
            proof = previous["verified_load"]

    command, created = reserve_command(actor, operation_id, "close", "ticket", ticket.pk, {**intent, "warning": proof})
    if not created:
        return existing.payload["result"], True

    if ticket.status != Ticket.Status.OPEN:
        raise AccessError("ticket_closed", 409, ticket=ticket_data(ticket))
    if ticket.assignment_version != version or ticket.current_owner_id != actor.pk:
        raise AccessError("assignment_version_conflict", 409, ticket=ticket_data(ticket))

    responses = list(Response.objects.select_related("command").select_for_update(of=("self",)).filter(ticket=ticket).order_by("pk"))
    attempts = list(Attempt.objects.select_for_update().filter(response__in=responses).order_by("pk"))
    snapshot = snapshot_for_close(dialog, ticket, actor.pk, responses, attempts)
    warning = warning_for(snapshot, version)
    expected_proof = {**binding, "snapshot_digest": digest(snapshot)}
    details = {"warning": {**warning, "warning_token": signing.dumps(expected_proof, salt=SALT)}} if warning else {}
    if confirm:
        if proof is None or set(proof) != {*binding, "snapshot_digest"} or any(proof[key] != value for key, value in binding.items()):
            raise AccessError("warning_token_invalid", 409, **details)
        if proof != expected_proof or not warning:
            raise AccessError("close_warning_stale", 409, **details)
    elif warning:
        raise AccessError("close_warning_required", 409, **details)

    now = timezone.now()
    ticket.status = Ticket.Status.CLOSED
    ticket.closed_at = now
    ticket.closed_by = actor
    ticket.closed_warning_ack = confirm
    ticket.closed_reason = "manager_forced_with_warning" if confirm else "manager_resolved"
    ticket.save(update_fields=["status", "closed_at", "closed_by", "closed_warning_ack", "closed_reason"])
    dialog.mode = "ai_auto"
    dialog.mode_version += 1
    dialog.save(update_fields=["mode", "mode_version", "updated_at"])
    cancel_waiting_on_close(responses, attempts, now)

    result = {**ticket_data(ticket), "opened_at": ticket.created_at.isoformat().replace("+00:00", "Z"),
              "closed_at": now.isoformat().replace("+00:00", "Z"), "closed_by_id": str(actor.pk),
              "closed_reason": ticket.closed_reason, "closed_warning_ack": confirm}
    payload = {"request": {**intent, "warning": proof}, "request_fingerprint": command.request_fingerprint,
               "result": result}
    if confirm:
        payload["warning_confirmation"] = {
            "actor_id": str(actor.pk), "confirmed_at": result["closed_at"],
            "verified_load": proof, "snapshot": snapshot, "token_digest": digest(token),
        }
    TicketEvent.objects.create(
        ticket=ticket, command=command, assignment_version=version, previous_owner_id=actor.pk, new_owner_id=actor.pk,
        payload=payload,
    )
    return result, False


def cancel_waiting_on_close(responses, attempts, now):
    cancelled = set()
    for attempt in attempts:
        if attempt.status == Attempt.Status.IN_FLIGHT and attempt.started_at is None:
            cancel_unstarted_attempt(attempt, "ticket_closed", now)
            cancelled.add(attempt.response_id)
    for response in responses:
        if response.pk in cancelled and response.status == Response.Status.PENDING:
            response.status = Response.Status.FAILED
            response.failure_reason = "ticket_closed"
            response.save(update_fields=["status", "failure_reason"])
