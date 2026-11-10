from django.urls import reverse

from accounts.services import AccessError
from participants.services import actor as participant_actor
from workspace.services import workspace_actor

from .models import Application, Event, EventHistory, Participation
from .services import get_event, organiser, registration_is_open

EVENT_LABELS = {"planned": "Запланировано", "started": "Началось", "completed": "Завершено", "cancelled": "Отменено", "stopped": "Прервано", "deleted": "Удалено"}
APPLICATION_LABELS = {"draft": "Черновик", "submitted": "Отправлена", "needs_changes": "Требует уточнения",
    "admitted": "Допущена", "rejected": "Отклонена", "withdrawn": "Отказ от участия", "admission_revoked": "Допуск снят"}
MARK_LABELS = {"unmarked": "Не отмечено", "participated": "Участвовала", "not_participated": "Не участвовала"}
HISTORY_LABELS = {"application_submitted": "Заявка подана", "application_admit": "Допуск подтверждён",
    "application_reject": "Заявка отклонена", "application_request_changes": "Запрошено уточнение",
    "application_reconsider": "Возвращена на рассмотрение", "application_corrections": "Исправления получены",
    "withdraw_application": "Зафиксирован отказ", "revoke_admission": "Допуск снят", "application_edited": "Данные исправлены организатором"}


def event_data(event, *, public=False):
    result = {"id": str(event.pk), "title": event.title, "sport": event.sport,
        "participation_format": event.participation_format, "planned_start_at": event.planned_start_at,
        "timezone_name": event.timezone_name, "location": event.location, "description": event.description,
        "state": event.state, "state_label": EVENT_LABELS[event.state], "registration_open": registration_is_open(event),
        "form_revision_id": str(event.current_form_id) if event.current_form_id else None,
        "registration_window_id": str(event.current_window_id) if event.current_window_id else None,
        "registration_deadline_at": event.current_window.deadline_at if event.current_window_id else None}
    if not public:
        result.update({"event_uuid": str(event.stable_id), "revision": event.revision, "responsible_profile_id": str(event.responsible_id),
            "started_at": event.started_at, "finished_at": event.finished_at, "reporting_excluded": event.reporting_excluded,
            "reporting_primary_id": str(event.reporting_primary_id) if event.reporting_primary_id else None,
            "reporting_reason": event.reporting_reason,
            "helpers": [{"profile_id": str(item.profile_id), "name": item.profile.display_name,
                         "access_generation": item.access_generation}
                        for item in event.helper_assignments.filter(ended_at__isnull=True).select_related("profile").order_by("pk")]})
    return result


def application_access(session, event_id, application_id, *, prefer_staff=False):
    event = get_event(event_id)
    application = Application.objects.select_related("event", "form", "owner").filter(event=event, pk=application_id).first()
    if application is None:
        raise AccessError("application_not_found", 404)
    try:
        person = participant_actor(session)
    except AccessError:
        person = None
    if person and application.owner_id == person.pk and not prefer_staff:
        return event, application, "owner", None
    try:
        staff = workspace_actor(session)
    except AccessError:
        raise AccessError("application_not_found", 404) from None
    if application.first_submitted_at is None:
        raise AccessError("application_not_found", 404)
    return event, application, "staff", staff


def application_data(application, *, reviewer=False):
    event = application.event
    result = {"id": str(application.pk), "event_id": str(event.pk), "revision": application.revision,
        "form_revision_id": str(application.form_id), "status": application.status,
        "status_label": APPLICATION_LABELS[application.status], "values": application.values,
        "first_submitted_at": application.first_submitted_at, "participant_explanation": application.participant_explanation,
        "event_started_without_admission": bool(event.started_at and application.status in {"submitted", "needs_changes"})}
    allowed = {"application_submitted", "application_admit", "application_reject", "application_request_changes",
               "application_reconsider", "application_corrections", "withdraw_application", "revoke_admission", "application_edited"}
    history = []
    for entry in EventHistory.objects.filter(application=application, action__in=allowed).order_by("pk"):
        item = {"id": str(entry.pk), "action": entry.action, "label": HISTORY_LABELS[entry.action], "created_at": entry.created_at}
        if entry.payload.get("participant_explanation"):
            item["participant_explanation"] = entry.payload["participant_explanation"]
        if reviewer and entry.payload.get("internal_reason"):
            item["internal_reason"] = entry.payload["internal_reason"]
        history.append(item)
    result["history"] = history
    return result


def participant_requests(person):
    return [{"title": row.event.title, "status_label": APPLICATION_LABELS[row.status],
             "url": reverse("event-application", args=[row.event_id, row.pk])}
            for row in Application.objects.filter(owner=person, event__deleted_at__isnull=True,
                event__deletion_pending=False, event__restoration_verification_required=False).select_related("event").order_by("-updated_at", "-pk")]


def participation_rows(event):
    snapshot = getattr(event, "start_snapshot", None)
    if not snapshot:
        return []
    entries = {entry["application_id"]: entry for entry in snapshot.entries}
    return [{"id": str(mark.pk), "revision": mark.revision, "mark": mark.mark, "mark_label": MARK_LABELS[mark.mark],
             "name": entries[str(mark.application_id)]["name"], "players": entries[str(mark.application_id)]["players"]}
            for mark in snapshot.participation_marks.order_by("pk")]
