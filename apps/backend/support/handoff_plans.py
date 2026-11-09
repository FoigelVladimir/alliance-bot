import json
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from accounts.policies import quarantine_incident
from jobs.models import BackgroundTask as Task
from jobs.services import ClockTimestamp, LeaseLost, claim, enqueue, finish, recover_one
from storage.services import database_time
from vk_channel.models import LongPollCursor

from .models import (
    AIPartDelivery, AIProcessingRun, AIRequest, Dialog, HandoffPlan, HandoffPlanAnswer,
    InboundRoute, Message, Ticket,
)

TASK_TYPE = "support.handoff_deadline"
HANDOFF_BUDGET = timedelta(seconds=30)
TERMINAL_DELIVERIES = {"sent", "failed", "cancelled", "unknown_outcome"}


def lock_dialog(dialog_id):
    cursor_id = Dialog.objects.values_list("cursor_id", flat=True).get(pk=dialog_id)
    cursor = LongPollCursor.objects.select_for_update().get(pk=cursor_id)
    dialog = Dialog.objects.select_for_update().get(pk=dialog_id)
    return cursor, dialog


def enqueue_timer(plan):
    key = uuid5(NAMESPACE_URL, f"alliance:handoff:{plan.pk}:{plan.timer_revision}")
    enqueue(TASK_TYPE, key, {"plan_id": plan.pk, "revision": plan.timer_revision},
            scheduled_at=plan.handoff_deadline_at)


def current_inputs(plan, cursor, dialog):
    if (cursor.automation_paused or cursor.automation_epoch != plan.automation_epoch
            or dialog.mode != "ai_auto" or dialog.mode_version != plan.mode_version):
        return []
    versions = dict(AIRequest.objects.filter(dialog=dialog, resolved_at__isnull=True).values_list("pk", "request_version"))
    return [item for item in plan.inputs_snapshot if versions.get(item["request_id"]) == item["version"]]


def refresh_plan(plan, cursor, dialog, now):
    """The cursor and dialog serialize version changes, delivery outcomes and timers."""
    if plan.status != "pending":
        return []
    inputs = current_inputs(plan, cursor, dialog)
    if not any(item["remaining_work"] for item in inputs):
        plan.status, plan.finished_at = "superseded", now
        plan.save(update_fields=["status", "finished_at"])
        return []
    deadline = min(datetime.fromisoformat(item["version_started_at"]) for item in inputs) + plan.budget
    if deadline != plan.handoff_deadline_at:
        plan.handoff_deadline_at = deadline
        plan.timer_revision += 1
        plan.save(update_fields=["handoff_deadline_at", "timer_revision"])
        enqueue_timer(plan)
    return inputs


@transaction.atomic
def ensure_handoff(run, *, remaining_work=None):
    """Persist an established lack of reliable information, even before text is ready.

    An optional mapping gives the unresolved residue for each request in a partial answer.
    All run inputs participate in the stable key and the initial common deadline.
    """
    if quarantine_incident():
        return None
    cursor, dialog = lock_dialog(run.dialog_id)
    locked = AIProcessingRun.objects.select_for_update().get(pk=run.pk)
    if locked.handoff_plan_id:
        plan = HandoffPlan.objects.get(pk=locked.handoff_plan_id)
        evaluate_locked(plan, cursor, dialog, database_time())
        return plan
    if (cursor.automation_paused or cursor.automation_epoch != locked.automation_epoch
            or dialog.mode != "ai_auto" or dialog.mode_version != locked.mode_version):
        return None
    inputs = []
    for item in locked.inputs.select_related("request").order_by("request_id"):
        request = item.request
        if request.request_version != item.request_version:
            return None
        inputs.append({
            "request_id": request.pk, "version": item.request_version,
            "version_started_at": request.request_version_started_at.isoformat(),
            "question": item.question_text,
            "message_ids": list(item.messages.order_by("pk").values_list("pk", flat=True)),
            "remaining_work": (remaining_work.get(request.pk, "") if remaining_work is not None
                               else "Не найдены надёжные сведения для ответа на вопрос"),
        })
    if not inputs or not any(item["remaining_work"] for item in inputs):
        raise ValueError("A handoff requires an established unresolved question")
    identity = [dialog.pk, locked.mode_version, locked.automation_epoch,
                [[item["request_id"], item["version"]] for item in inputs]]
    deadline = min(datetime.fromisoformat(item["version_started_at"]) for item in inputs) + HANDOFF_BUDGET
    now = database_time()
    plan, created = HandoffPlan.objects.get_or_create(
        fingerprint=sha256(json.dumps(identity).encode()).hexdigest(), defaults={
            "dialog": dialog, "mode_version": locked.mode_version, "automation_epoch": locked.automation_epoch,
            "inputs_snapshot": inputs, "budget": HANDOFF_BUDGET, "initial_deadline_at": deadline,
            "handoff_deadline_at": deadline, "created_at": now,
        },
    )
    locked.handoff_plan = plan
    locked.save(update_fields=["handoff_plan"])
    run.handoff_plan = plan
    if created:
        enqueue_timer(plan)
    if locked.answer_id:
        bind_answer(locked)
    evaluate_locked(plan, cursor, dialog, now)
    return plan


def bind_answer(run):
    """Called in the transaction saving the answer and its parts."""
    if not run.handoff_plan_id:
        return
    plan = HandoffPlan.objects.get(pk=run.handoff_plan_id)
    HandoffPlanAnswer.objects.get_or_create(plan=plan, answer_id=run.answer_id, defaults={
        "required_part_ids": list(run.answer.parts.order_by("position").values_list("pk", flat=True)),
    })
    if plan.ticket_id:
        from .ai_delivery import cancel_for_handoff

        run.handoff_ticket_id = plan.ticket_id
        run.save(update_fields=["handoff_ticket"])
        AIPartDelivery.objects.filter(part__answer_id=run.answer_id).update(handoff_ticket_id=plan.ticket_id)
        cancel_for_handoff(run.answer_id, database_time())


def preparation_finished(plan):
    runs = list(plan.runs.values_list("status", flat=True))
    if not runs or "running" in runs:
        return False
    for link in plan.answers.all():
        outcomes = dict(AIPartDelivery.objects.filter(part_id__in=link.required_part_ids)
                        .values_list("part_id", "status"))
        if not link.required_part_ids or any(outcomes.get(pk) not in TERMINAL_DELIVERIES for pk in link.required_part_ids):
            return False
    return True


def unclassified_routes(cursor, dialog):
    return InboundRoute.objects.filter(
        Q(status="pending") | Q(status="needs_review", reason="manual_support"),
        dialog=dialog, mode_version=dialog.mode_version, automation_epoch=cursor.automation_epoch,
        historical=False, automation_paused=False, message__ticket__isnull=True,
        message__airequestmessage__isnull=True,
    )


def work_for_ticket(cursor, dialog, plans, *, planned_request_ids=()):
    residue = {
        item["request_id"]: item["remaining_work"]
        for plan in plans for item in current_inputs(plan, cursor, dialog) if item["remaining_work"]
    }
    work = []
    requests = AIRequest.objects.filter(dialog=dialog, mode_version=dialog.mode_version,
                                        automation_epoch=cursor.automation_epoch, resolved_at__isnull=True).order_by("pk")
    for request in requests:
        if request.pk in residue or request.pk in planned_request_ids or request.status in {"pending", "failed"}:
            work.append({"kind": "question", "request_id": request.pk, "version": request.request_version,
                         "question": request.normalized_query,
                         "remaining_work": residue.get(request.pk, "Студент попросил помощи сотрудника"
                                                       if request.pk in planned_request_ids else "Вопрос ожидает ответа сотрудника"),
                         "message_ids": list(request.message_links.values_list("message_id", flat=True))})
    for route in unclassified_routes(cursor, dialog).select_related("message").order_by("sequence"):
        work.append({"kind": "question", "question": route.message.text,
                     "remaining_work": "Сообщение ожидает разбора сотрудника", "message_ids": [route.message_id]})
    deliveries = AIPartDelivery.objects.filter(
        part__answer__aiprocessingrun__dialog=dialog,
        part__answer__aiprocessingrun__mode_version=dialog.mode_version,
        part__answer__aiprocessingrun__automation_epoch=cursor.automation_epoch,
        part__aianswerpartinput__isnull=False,
    ).exclude(status="sent").select_related("part").distinct().order_by("part__answer_id", "part__position")
    for delivery in deliveries:
        if any(item.request.resolved_at is None and item.request.request_version == item.request_version_snapshot
               for item in delivery.part.answer.inputs.select_related("request")):
            work.append({"kind": "unconfirmed_part", "part_id": delivery.part_id,
                         "text": delivery.part.text_snapshot, "status": delivery.status})
    return work


def transfer_locked(cursor, dialog, plans, now, *, ticket=None, planned_request_ids=()):
    from .ai_delivery import cancel_for_handoff
    from .ai_processing import handoff_active_requests

    work = work_for_ticket(cursor, dialog, plans, planned_request_ids=planned_request_ids)
    ticket = ticket or Ticket.objects.select_for_update().filter(dialog=dialog, status=Ticket.Status.OPEN).first()
    if ticket is None:
        ticket = Ticket.objects.create(dialog=dialog, handoff_reason="no_reliable_basis", created_at=now)
    for plan in plans:
        plan.status, plan.ticket, plan.finished_at, plan.work_snapshot = "transferred", ticket, now, work
        plan.save(update_fields=["status", "ticket", "finished_at", "work_snapshot"])
    # Persist the card before cancelling attempts, whose outcome handlers revisit the plan.
    request_ids = set(planned_request_ids) | {item["request_id"] for plan in plans for item in current_inputs(plan, cursor, dialog)}
    handoff_active_requests(cursor, dialog, ticket, reason=ticket.handoff_reason or "no_reliable_basis",
                            planned_request_ids=request_ids)
    route_ids = list(unclassified_routes(cursor, dialog).values_list("pk", flat=True))
    Message.objects.filter(inboundroute__pk__in=route_ids).update(ticket=ticket)
    InboundRoute.objects.filter(pk__in=route_ids).update(
        status="attached", reason=ticket.handoff_reason or "no_reliable_basis",
    )
    for run in AIProcessingRun.objects.filter(handoff_plan__in=plans).order_by("answer_id", "pk"):
        run.handoff_ticket = ticket
        run.save(update_fields=["handoff_ticket"])
        if run.answer_id:
            AIPartDelivery.objects.filter(part__answer_id=run.answer_id).update(handoff_ticket=ticket)
            Message.objects.filter(ai_answer_part__answer_id=run.answer_id, ticket__isnull=True).update(ticket=ticket)
            cancel_for_handoff(run.answer_id, now)
    if dialog.mode == "ai_auto":
        dialog.mode = "human_support"
        dialog.mode_version += 1
        dialog.save(update_fields=["mode", "mode_version", "updated_at"])
    return ticket


def evaluate_locked(plan, cursor, dialog, now, *, revision=None):
    if quarantine_incident() or plan.status != "pending":
        return plan.ticket
    inputs = refresh_plan(plan, cursor, dialog, now)
    if not inputs or revision is not None and revision != plan.timer_revision:
        return None
    if now < plan.handoff_deadline_at and not preparation_finished(plan):
        return None
    plans = []
    for candidate in HandoffPlan.objects.filter(dialog=dialog, status="pending").order_by("pk"):
        if refresh_plan(candidate, cursor, dialog, now):
            plans.append(candidate)
    ticket = transfer_locked(cursor, dialog, plans, now)
    plan.refresh_from_db()
    return ticket


@transaction.atomic
def evaluate_plan(plan_id, *, revision=None):
    original = HandoffPlan.objects.get(pk=plan_id)
    cursor, dialog = lock_dialog(original.dialog_id)
    plan = HandoffPlan.objects.select_for_update().get(pk=plan_id)
    return evaluate_locked(plan, cursor, dialog, database_time(), revision=revision)


def refresh_dialog_plans(cursor, dialog):
    for plan in HandoffPlan.objects.filter(dialog=dialog, status="pending").order_by("pk"):
        evaluate_locked(plan, cursor, dialog, database_time())
        if dialog.mode != "ai_auto":
            break


def finish_explicit_handoff(cursor, dialog, ticket, *, planned_request_ids=(), force=False):
    plans = [plan for plan in HandoffPlan.objects.filter(dialog=dialog, status="pending").order_by("pk")
             if refresh_plan(plan, cursor, dialog, database_time())]
    if plans or planned_request_ids or force:
        transfer_locked(cursor, dialog, plans, database_time(), ticket=ticket, planned_request_ids=planned_request_ids)
    return bool(plans or planned_request_ids or force)


def answer_outcome_changed(answer_id):
    link = HandoffPlanAnswer.objects.filter(answer_id=answer_id).first()
    if link is not None:
        evaluate_plan(link.plan_id)


def answer_dispatch_blocked(answer_id):
    plan = HandoffPlan.objects.filter(answers__answer_id=answer_id).first()
    return plan is not None and (plan.status != "pending" or database_time() >= plan.handoff_deadline_at)


def run_handoffs_once():
    if not settings.SUPPORT_AI_HANDOFF_ENABLED or quarantine_incident():
        return False
    # The sweep also covers missing timers, exhausted leases and a restart after the deadline.
    overdue = HandoffPlan.objects.filter(status="pending", handoff_deadline_at__lte=ClockTimestamp()).order_by(
        "handoff_deadline_at", "pk",
    ).first()
    if overdue is not None:
        evaluate_plan(overdue.pk)
        return True
    while recover_one([TASK_TYPE]):
        pass
    task = claim([TASK_TYPE])
    if task is None:
        return False
    try:
        with transaction.atomic():
            plan_id, revision = task.payload["plan_id"], task.payload["revision"]
            if task.idempotency_key != uuid5(NAMESPACE_URL, f"alliance:handoff:{plan_id}:{revision}"):
                finish(task, {"reason": "invalid_handoff_timer"}, status=Task.Status.FAILED)
            else:
                ticket = evaluate_plan(plan_id, revision=revision)
                finish(task, {"ticket_id": ticket.pk if ticket else None})
    except LeaseLost:
        pass
    return True
