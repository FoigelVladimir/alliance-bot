from datetime import timedelta
from hashlib import sha256
from secrets import token_urlsafe

from django.conf import settings
from django.contrib.sessions.models import Session
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from . import vk_id_client
from .models import AccessApplication, AccessControl, AccessEvent, BrowserLogin, IdentityBinding, LeaderRecovery, StaffProfile
from .policies import can_change_access, cleared_for_incident, profile_from_session, quarantine_incident, review_roles

Access = StaffProfile.Access
Role = StaffProfile.Role
Status = AccessApplication.Status


class AccessError(Exception):
    def __init__(self, code, status, **details):
        self.code = code
        self.status = status
        self.details = details


def lock_session(session):
    return Session.objects.select_for_update().filter(
        session_key=session.session_key, expire_date__gt=timezone.now(),
    ).first()


def state_digest(state):
    return sha256(state.encode()).hexdigest()


@transaction.atomic
def start_login(session, recovery_code=None):
    if session.session_key is None:
        session["vk_browser"] = True
        session.create()
    if not lock_session(session) or profile_from_session(session):
        raise AccessError("login_unavailable", 409)
    AccessControl.objects.select_for_update().get_or_create(pk=1)
    now = timezone.now()
    BrowserLogin.objects.filter(expires_at__lte=now).delete()
    incident = quarantine_incident()
    recovery = None
    if recovery_code is not None:
        recovery = LeaderRecovery.objects.filter(
            quarantine_incident=incident, code_hash=state_digest(recovery_code), status=LeaderRecovery.Status.PENDING, expires_at__gt=now,
        ).first()
        if recovery is None:
            raise AccessError("login_unavailable", 409)
    state, verifier = token_urlsafe(32), token_urlsafe(48)
    BrowserLogin.objects.update_or_create(session_key=session.session_key, defaults={
        "state_hash": state_digest(state),
        "verifier": verifier,
        "started_at": now,
        "expires_at": now + timedelta(minutes=10),
        "exchanging": False,
        "recovery": recovery,
        "quarantine_incident": incident,
    })
    return vk_id_client.authorize_url(state, verifier)


@transaction.atomic
def claim_login(session, state, exchange_requested):
    if not lock_session(session):
        return None
    attempt = BrowserLogin.objects.filter(session_key=session.session_key).first()
    if attempt is None:
        return None
    if attempt.expires_at <= timezone.now() or attempt.quarantine_incident != quarantine_incident():
        attempt.delete()
        return None
    if attempt.exchanging or not constant_time_compare(attempt.state_hash, state_digest(state)):
        return None
    if not exchange_requested:
        attempt.delete()
        return None
    attempt.exchanging = True
    attempt.save(update_fields=["exchanging"])
    return attempt


def resolve_identity(identity, incident):
    key = {"provider": "vk_id", "external_id": identity.external_id}
    binding = IdentityBinding.objects.filter(**key).first()
    if binding is None:
        if incident:
            return None
        try:
            with transaction.atomic():
                profile = StaffProfile.objects.create()
                binding = IdentityBinding.objects.create(profile=profile, **key)
        except IntegrityError:
            binding = IdentityBinding.objects.get(**key)
    profile = StaffProfile.objects.select_for_update().get(pk=binding.profile_id)
    current = IdentityBinding.objects.select_for_update().get(pk=binding.pk)
    if (
        not current.active or current.profile_id != profile.pk or profile.disabled
        or not cleared_for_incident(profile, incident)
    ):
        return None
    return profile, current


@transaction.atomic
def finish_login(session, claimed, identity):
    if not lock_session(session):
        return False
    attempt = BrowserLogin.objects.filter(session_key=session.session_key).first()
    if not attempt or not attempt.exchanging or attempt.state_hash != claimed.state_hash:
        return False
    recovery_id = attempt.recovery_id
    attempt.delete()
    incident = quarantine_incident()
    if attempt.expires_at <= timezone.now() or identity is None or attempt.quarantine_incident != incident:
        return False
    if recovery_id is not None:
        verified = LeaderRecovery.objects.filter(
            pk=recovery_id, quarantine_incident=incident, status=LeaderRecovery.Status.PENDING, expires_at__gt=timezone.now(),
        ).update(status=LeaderRecovery.Status.VERIFIED, external_id=identity.external_id, code_hash=None)
        if verified:
            session["leader_recovery_verified"] = True
            session.save()
        return bool(verified)
    result = resolve_identity(identity, incident)
    if result is None:
        return False
    profile, binding = result
    profile.display_name = identity.display_name
    profile.photo_url = identity.photo_url
    fields = ["display_name", "photo_url"]
    if (
        profile.access_state == Access.REVOKED and profile.last_revocation_id
        and profile.last_revocation.created_at < attempt.started_at
    ):
        profile.regrant_verified_at = timezone.now()
        profile.regrant_identity_version = profile.identity_version
        profile.regrant_revocation = profile.last_revocation
        fields.extend(["regrant_verified_at", "regrant_identity_version", "regrant_revocation"])
    profile.save(update_fields=fields)
    session.cycle_key()
    session.update({
        "staff_profile": profile.pk, "staff_binding": binding.pk,
        "staff_epoch": profile.session_epoch, "staff_incident": incident,
    })
    session.save()
    return True


@transaction.atomic
def end_login(session):
    if lock_session(session):
        BrowserLogin.objects.filter(session_key=session.session_key).delete()
    session.flush()


def lock_profiles(*ids):
    profiles = StaffProfile.objects.select_for_update().filter(pk__in=ids).order_by("pk")
    return {profile.pk: profile for profile in profiles}


def application_data(application):
    if application is None:
        return None
    return {
        "id": application.pk,
        "status": application.status,
        "club_activity": application.club_activity,
        "submitted_at": application.submitted_at,
        "decided_at": application.decided_at,
        "applicant_message": application.applicant_message,
    }


def access_data(profile):
    return {
        "access_state": profile.access_state,
        "role": profile.role or None,
        "blocked_at": profile.blocked_at.isoformat() if profile.blocked_at else None,
        "access_version": profile.access_version,
        "can_appoint_administrator": profile.can_appoint_administrator,
    }


def latest_application(profile):
    return profile.applications.order_by("-submitted_at", "-id").first()


def application_restriction(profile, latest):
    if profile.blocked_at:
        return AccessError("access_blocked", 403)
    if profile.access_state == Access.GRANTED:
        return AccessError("access_already_granted", 409)
    if profile.access_state == Access.REVOKED:
        return AccessError("application_not_available", 409)
    if latest and latest.status == Status.PENDING:
        return AccessError("application_pending", 409, application=application_data(latest))
    if latest and latest.status == Status.REJECTED:
        retry_at = latest.decided_at + timedelta(hours=1)
        if timezone.now() < retry_at:
            return AccessError("application_retry_too_soon", 429, retry_after_at=retry_at)
    return None


@transaction.atomic
def apply_for_access(session, operation_id, activity):
    profile = lock_profiles(session.get("staff_profile")).get(session.get("staff_profile"))
    if profile is None or not profile_from_session(session, profile):
        raise AccessError("authentication_required", 401)
    if profile.blocked_at:
        raise AccessError("access_blocked", 403)
    existing = profile.applications.filter(client_operation_id=operation_id).first()
    if existing:
        if existing.club_activity != activity:
            raise AccessError("operation_mismatch", 409)
        return existing, True
    restriction = application_restriction(profile, latest_application(profile))
    if restriction:
        raise restriction
    return profile.applications.create(client_operation_id=operation_id, club_activity=activity), False


def replay_event(actor, subject, operation_id, action, parameters, application=None):
    event = AccessEvent.objects.filter(actor=actor, operation_id=operation_id).first()
    if event and (
        event.subject_id != subject.pk or event.action != action or event.parameters != parameters
        or event.application_id != (application.pk if application else None)
    ):
        raise AccessError("operation_mismatch", 409)
    return event


@transaction.atomic
def decide_application(session, application_id, operation_id, version, action, value):
    subject_id = AccessApplication.objects.filter(pk=application_id).values_list("profile_id", flat=True).first()
    profiles = lock_profiles(session.get("staff_profile"), subject_id)
    actor = profiles.get(session.get("staff_profile"))
    roles = review_roles(profile_from_session(session, actor))
    if not roles or subject_id == actor.pk or (action == "approve" and value not in roles):
        raise AccessError("forbidden", 403)
    if subject_id is None:
        raise AccessError("not_found", 404)
    subject = profiles[subject_id]
    if not cleared_for_incident(subject, quarantine_incident()):
        raise AccessError("not_found", 404)
    application = AccessApplication.objects.select_for_update().get(pk=application_id)
    parameters = {"identity_version": version, "value": value}
    if replay_event(actor, subject, operation_id, action, parameters, application):
        return application, subject, True
    if application.status != Status.PENDING:
        raise AccessError("application_already_decided", 409)
    if version != subject.identity_version:
        raise AccessError("applicant_identity_changed", 409)
    if subject.access_state != Access.NOT_GRANTED or subject.blocked_at or subject.disabled:
        raise AccessError("applicant_access_changed", 409)
    if not subject.bindings.filter(active=True).exists():
        raise AccessError("applicant_identity_changed", 409)
    before = access_data(subject)
    now = timezone.now()
    if action == "approve":
        subject.access_state = Access.GRANTED
        subject.role = value
        subject.access_version += 1
        subject.save(update_fields=["access_state", "role", "access_version"])
        application.status = Status.APPROVED
    else:
        application.status = Status.REJECTED
        application.applicant_message = value
    application.decided_at = now
    application.decided_by = actor
    application.save(update_fields=["status", "applicant_message", "decided_at", "decided_by"])
    AccessEvent.objects.create(
        actor=actor, subject=subject, application=application, operation_id=operation_id,
        action=action, parameters=parameters, before=before, after=access_data(subject), created_at=now,
    )
    return application, subject, False


@transaction.atomic
def bootstrap_leader(profile_id, version):
    control, _ = AccessControl.objects.select_for_update().get_or_create(pk=1)
    if quarantine_incident():
        raise AccessError("quarantine_active", 409)
    if control.initial_event_id:
        if control.initial_event.subject_id == profile_id:
            return control.initial_event
        raise AccessError("leader_already_initialized", 409)
    profile = lock_profiles(profile_id).get(profile_id)
    if profile is None or profile.disabled or not profile.bindings.filter(active=True).exists():
        raise AccessError("invalid_leader_identity", 409)
    if profile.identity_version != version:
        raise AccessError("applicant_identity_changed", 409)
    if profile.access_state != Access.NOT_GRANTED or profile.blocked_at:
        raise AccessError("applicant_access_changed", 409)
    before = access_data(profile)
    profile.access_state = Access.GRANTED
    profile.role = Role.LEADER
    profile.access_version += 1
    profile.save(update_fields=["access_state", "role", "access_version"])
    application = profile.applications.filter(status=Status.PENDING).first()
    event = AccessEvent.objects.create(
        subject=profile, application=application, action="bootstrap_leader",
        parameters={"identity_version": version}, before=before, after=access_data(profile),
    )
    if application:
        application.status = Status.APPROVED
        application.decided_at = event.created_at
        application.service_decision_event = event
        application.save(update_fields=["status", "decided_at", "service_decision_event"])
    control.initial_event = event
    control.save(update_fields=["initial_event"])
    return event


def applicable_access_change(profile, action):
    if profile.disabled:
        return False
    granted = profile.access_state == Access.GRANTED
    manager = profile.role == Role.MANAGER
    administrator = profile.role == Role.ADMINISTRATOR
    delegated = profile.can_appoint_administrator
    if action == "block":
        return granted and not profile.blocked_at
    if action == "unblock":
        return granted and bool(profile.blocked_at)
    if action == "revoke_access":
        return granted
    if action == "assign_administrator":
        return granted and manager and not profile.blocked_at
    if action == "remove_administrator":
        return granted and administrator and not delegated
    if action == "grant_appoint_administrator":
        return granted and administrator and not delegated and not profile.blocked_at
    if action == "revoke_appoint_administrator":
        return granted and administrator and delegated
    if action == "regrant_access":
        return (
            profile.access_state == Access.REVOKED and profile.regrant_verified_at is not None
            and profile.last_revocation_id is not None and profile.regrant_revocation_id == profile.last_revocation_id
            and profile.regrant_identity_version == profile.identity_version
            and profile.regrant_verified_at + timedelta(hours=24) > timezone.now()
            and profile.bindings.filter(active=True).exists()
        )
    return False


@transaction.atomic
def change_access(session, profile_id, operation_id, version, action):
    if action == "revoke_access":
        AccessControl.objects.select_for_update().get_or_create(pk=1)
    profiles = lock_profiles(session.get("staff_profile"), profile_id)
    actor = profiles.get(session.get("staff_profile"))
    if not actor or not review_roles(profile_from_session(session, actor)):
        raise AccessError("forbidden", 403)
    profile = profiles.get(profile_id)
    if profile is None or not cleared_for_incident(profile, quarantine_incident()):
        raise AccessError("not_found", 404)
    if not can_change_access(actor, profile, action):
        raise AccessError("forbidden", 403)
    parameters = {"access_version": version}
    event = replay_event(actor, profile, operation_id, action, parameters)
    if event:
        return event, profile, True
    if profile.access_version != version:
        raise AccessError("access_version_conflict", 409)
    if not applicable_access_change(profile, action):
        raise AccessError("access_state_conflict", 409)

    before = access_data(profile)
    now = timezone.now()
    if action == "block":
        profile.blocked_at = now
    elif action == "unblock":
        profile.blocked_at = None
    elif action == "assign_administrator":
        profile.role = Role.ADMINISTRATOR
    elif action == "remove_administrator":
        profile.role = Role.MANAGER
    elif action in ("grant_appoint_administrator", "revoke_appoint_administrator"):
        profile.can_appoint_administrator = action == "grant_appoint_administrator"
    elif action == "revoke_access":
        if profile.can_appoint_administrator:
            profile.can_appoint_administrator = False
            AccessEvent.objects.create(
                actor=actor, subject=profile, action="revoke_appoint_administrator",
                before=before, after=access_data(profile), created_at=now,
            )
        profile.access_state = Access.REVOKED
        profile.access_generation += 1
        profile.session_epoch += 1
        profile.regrant_verified_at = None
        profile.regrant_identity_version = None
        profile.regrant_revocation = None
    elif action == "regrant_access":
        profile.access_state = Access.GRANTED
        profile.role = Role.MANAGER
        profile.blocked_at = None
        profile.session_epoch += 1
        profile.regrant_verified_at = None
        profile.regrant_identity_version = None
        profile.regrant_revocation = None

    profile.access_version += 1
    event = AccessEvent.objects.create(
        actor=actor, subject=profile, operation_id=operation_id, action=action,
        parameters=parameters, before=before, after=access_data(profile), created_at=now,
    )
    if action == "revoke_access":
        profile.last_revocation = event
    profile.save()
    return event, profile, False


def active_leader(profile):
    return (
        profile and not profile.disabled and profile.access_state == Access.GRANTED
        and profile.role == Role.LEADER and not profile.blocked_at
        and profile.bindings.filter(active=True).exists()
    )


@transaction.atomic
def begin_leader_recovery(profile_id, version, authority=None):
    control, _ = AccessControl.objects.select_for_update().get_or_create(pk=1)
    incident = quarantine_incident()
    if incident:
        validate_emergency_authority(authority, incident, profile_id)
        if control.quarantine_incident != incident:
            control.quarantine_incident = incident
            control.save(update_fields=["quarantine_incident"])
    elif authority is not None:
        raise AccessError("quarantine_required", 409)
    profile = lock_profiles(profile_id).get(profile_id)
    if profile is None or profile.disabled or profile.identity_version != version or (
        not incident and not active_leader(profile)
    ):
        raise AccessError("invalid_leader_identity", 409)
    code = token_urlsafe(32)
    recovery = LeaderRecovery.objects.create(
        profile=profile, expected_identity_version=version, code_hash=state_digest(code),
        expires_at=timezone.now() + timedelta(minutes=30),
        quarantine_incident=incident, authority_reference=authority.reference if incident else "",
    )
    return recovery, code


@transaction.atomic
def recover_leader(recovery_id, responsible, transfer_profile_id=None, *, authority=None):
    control, _ = AccessControl.objects.select_for_update().get_or_create(pk=1)
    recovery = LeaderRecovery.objects.filter(pk=recovery_id).first()
    if recovery is None:
        raise AccessError("not_found", 404)
    incident = quarantine_incident()
    if recovery.quarantine_incident != incident or bool(incident) != (authority is not None):
        raise AccessError("invalid_recovery", 409)
    if incident:
        validate_emergency_authority(authority, incident, recovery.profile_id, recovery.authority_reference)
    if recovery.status == LeaderRecovery.Status.COMPLETED:
        return recovery.result
    if (
        recovery.status != LeaderRecovery.Status.VERIFIED or recovery.expires_at <= timezone.now()
        or not recovery.external_id
    ):
        raise AccessError("invalid_recovery", 409)
    key = {"provider": "vk_id", "external_id": recovery.external_id}
    owner_id = IdentityBinding.objects.filter(**key).values_list("profile_id", flat=True).first()
    profiles = lock_profiles(recovery.profile_id, owner_id)
    profile = profiles[recovery.profile_id]
    bindings = list(IdentityBinding.objects.select_for_update().filter(profile_id__in=profiles).order_by("id"))
    recovery = LeaderRecovery.objects.select_for_update().get(pk=recovery_id)
    if (
        recovery.status != LeaderRecovery.Status.VERIFIED or recovery.expires_at <= timezone.now()
        or recovery.external_id != key["external_id"]
    ):
        raise AccessError("invalid_recovery", 409)
    if incident:
        validate_emergency_authority(authority, incident, profile.pk, recovery.authority_reference)
    if profile.disabled or profile.identity_version != recovery.expected_identity_version or (
        not incident and not active_leader(profile)
    ):
        raise AccessError("invalid_leader_identity", 409)
    old = next((binding for binding in bindings if binding.profile_id == profile.pk and binding.active), None)
    new = next((binding for binding in bindings if binding.external_id == key["external_id"]
                and binding.provider == key["provider"]), None)
    same_owner = bool(new and new.profile_id == profile.pk)
    if new and ((not incident and same_owner) or (not new.active and not same_owner)):
        raise AccessError("identity_unavailable", 409)
    transfer_owner = owner_id if owner_id != profile.pk else None
    if transfer_profile_id != transfer_owner:
        raise AccessError("transfer_confirmation_required", 409, profile_id=transfer_owner)
    before = access_data(profile)
    if old and old != new:
        old.active = False
        old.save(update_fields=["active"])
    if same_owner:
        new.active = True
        new.save(update_fields=["active"])
    elif new:
        source = profiles[new.profile_id]
        if (
            source.access_state != Access.NOT_GRANTED or source.disabled or source.role or source.blocked_at
            or source.can_appoint_administrator or source.access_version != 1
            or sum(binding.profile_id == source.pk for binding in bindings) != 1
            or source.applications.exists()
            or AccessEvent.objects.filter(subject=source).exists()
        ):
            raise AccessError("transfer_profile_not_empty", 409, profile_id=source.pk)
        new.profile = profile
        new.save(update_fields=["profile"])
        source.disabled = True
        source.identity_version += 1
        source.session_epoch += 1
        source.save(update_fields=["disabled", "identity_version", "session_epoch"])
        AccessEvent.objects.create(
            subject=source, action="transfer_identity",
            parameters={"destination_profile": profile.pk, "binding": new.pk, "responsible": responsible},
        )
    else:
        try:
            with transaction.atomic():
                new = IdentityBinding.objects.create(profile=profile, **key)
        except IntegrityError:
            raise AccessError("identity_unavailable", 409) from None
    profile.identity_version += 1
    profile.access_version += 1
    profile.session_epoch += 1
    fields = ["identity_version", "access_version", "session_epoch"]
    if incident:
        profile.access_state = Access.GRANTED
        profile.role = Role.LEADER
        profile.blocked_at = None
        profile.can_appoint_administrator = False
        profile.access_generation += 1
        profile.regrant_verified_at = None
        profile.regrant_identity_version = None
        profile.regrant_revocation = None
        profile.cleared_incident = incident
        fields.extend([
            "access_state", "role", "blocked_at", "can_appoint_administrator", "access_generation",
            "regrant_verified_at", "regrant_identity_version", "regrant_revocation", "cleared_incident",
        ])
    profile.save(update_fields=fields)
    if not active_leader(profile):
        raise AccessError("invalid_leader_identity", 409)
    event = AccessEvent.objects.create(
        subject=profile, action="assign_emergency_leader" if incident else "recover_leader",
        before=before, after=access_data(profile),
        parameters={"recovery": recovery.pk, "old_binding": old.pk if old else None, "new_binding": new.pk,
                    "responsible": responsible, **(
                        {"incident": incident, "authority_reference": authority.reference} if incident else {}
                    )},
    )
    if incident and control.initial_event_id is None:
        control.initial_event = event
        control.save(update_fields=["initial_event"])
    recovery.status = LeaderRecovery.Status.COMPLETED
    recovery.result = event
    recovery.save(update_fields=["status", "result"])
    return event



def validate_emergency_authority(authority, incident, profile_id, expected_reference=None):
    if (
        not incident or settings.STAFF_QUARANTINE_INCIDENT != incident or authority is None
        or authority.incident != incident or authority.profile_id != profile_id
        or authority.expires_at <= timezone.now()
        or (expected_reference is not None and authority.reference != expected_reference)
    ):
        raise AccessError("invalid_emergency_authority", 409)
