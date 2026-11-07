from django.conf import settings

from .models import AccessControl, IdentityBinding, StaffProfile


def quarantine_incident():
    return settings.STAFF_QUARANTINE_INCIDENT or (
        AccessControl.objects.filter(pk=1).values_list("quarantine_incident", flat=True).first() or ""
    )


def cleared_for_incident(profile, incident):
    return not incident or profile.cleared_incident == incident


def profile_from_session(session, profile=None):
    profile_id = session.get("staff_profile")
    if profile_id is None:
        return None
    if profile is None:
        profile = StaffProfile.objects.filter(pk=profile_id).first()
    incident = quarantine_incident()
    if (
        profile is None or profile.pk != profile_id or profile.disabled
        or not cleared_for_incident(profile, incident)
        or session.get("staff_incident", "") != incident
        or profile.session_epoch != session.get("staff_epoch")
        or not IdentityBinding.objects.filter(
            pk=session.get("staff_binding"), profile=profile, active=True,
        ).exists()
    ):
        return None
    return profile


def has_work_access(profile):
    return bool(profile and profile.access_state == StaffProfile.Access.GRANTED and not profile.blocked_at)


def review_roles(profile):
    if not has_work_access(profile):
        return ()
    if profile.role == StaffProfile.Role.LEADER or (
        profile.role == StaffProfile.Role.ADMINISTRATOR and profile.can_appoint_administrator
    ):
        return (StaffProfile.Role.MANAGER, StaffProfile.Role.ADMINISTRATOR)
    if profile.role == StaffProfile.Role.ADMINISTRATOR:
        return (StaffProfile.Role.MANAGER,)
    return ()


CHANGE_ACTIONS = {
    "block": "Заблокировать",
    "unblock": "Разблокировать",
    "assign_administrator": "Назначить администратором",
    "remove_administrator": "Снять роль администратора",
    "grant_appoint_administrator": "Выдать право назначать администраторов",
    "revoke_appoint_administrator": "Отозвать право назначать администраторов",
    "revoke_access": "Отозвать рабочий допуск",
    "regrant_access": "Выдать допуск заново",
}


def can_change_access(actor, subject, action):
    roles = review_roles(actor)
    if not roles or not subject or actor.pk == subject.pk or subject.role == StaffProfile.Role.LEADER:
        return False
    leader = actor.role == StaffProfile.Role.LEADER
    if action in ("block", "unblock", "revoke_access"):
        return leader or subject.role == StaffProfile.Role.MANAGER
    if action == "assign_administrator":
        return StaffProfile.Role.ADMINISTRATOR in roles
    return leader and action in CHANGE_ACTIONS
