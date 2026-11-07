from django.db import transaction

from accounts.policies import quarantine_incident
from accounts.services import AccessError
from storage.services import database_time
from support.services import support_actor

from .models import DocumentFragment, Material, MaterialVersion, PublicationEvent
from .preparation import preparation_data
from .services import finish_command, locked_material, require_draft_access, reserve_command, supersede_preparation


def validate_revision(value, *, minimum=0):
    if type(value) is not int or not minimum <= value < 2**63:
        raise AccessError("invalid_request", 400)


def lock_version(material, version_id):
    version = MaterialVersion.objects.select_for_update().filter(pk=version_id, material=material).first()
    if version is None:
        raise AccessError("draft_not_found", 404)
    return version


def check_publication_revision(material, revision):
    if material.publication_revision != revision:
        current = MaterialVersion.objects.filter(material=material, is_published=True).first()
        raise AccessError("publication_revision_conflict", 409, publication_revision=material.publication_revision,
                          published_version_id=str(current.pk) if current else None)


@transaction.atomic
def change_publication(session, material_id, version_id, operation_id, revision, *, snapshot_id=None):
    actor = support_actor(session, lock=True)
    validate_revision(revision)
    if snapshot_id is not None:
        validate_revision(snapshot_id, minimum=1)
    material = locked_material(material_id)
    version = lock_version(material, version_id)
    require_draft_access(actor, version)
    action = "publish" if snapshot_id is not None else "unpublish"
    command, created = reserve_command(actor, operation_id, action, material_id, version_id,
                                       {"prepared_snapshot_id": snapshot_id}, revision)
    if not created:
        return command.publicationevent, True
    check_publication_revision(material, revision)
    current = MaterialVersion.objects.select_for_update().filter(material=material, is_published=True).first()
    fragments = DocumentFragment.objects.filter(document=material)
    now = database_time()
    if action == "publish":
        if version.sealed_at is not None:
            raise AccessError("draft_sealed", 409)
        if version.base_publication_revision != revision:
            raise AccessError("draft_base_changed", 409, publication_revision=revision)
        preparation = version.current_preparation
        state = preparation_data(preparation)
        if state.get("index_status") != "ready":
            raise AccessError("material_not_ready", 409)
        if state["snapshot_id"] != str(snapshot_id):
            raise AccessError("prepared_snapshot_changed", 409, prepared_snapshot_id=state["snapshot_id"])
        generation = preparation.snapshot.generation
        incoming = fragments.filter(version=version, prepared_snapshot_id=snapshot_id)
        if incoming.count() != generation.fragment_count:
            raise AccessError("material_not_ready", 409)
    else:
        if current is None or current.pk != version.pk:
            raise AccessError("published_version_changed", 409,
                              published_version_id=str(current.pk) if current else None)
        snapshot_id = current.published_prepared_snapshot_id

    fragments.filter(is_active=True).update(is_active=False)
    if current is not None:
        MaterialVersion.objects.filter(pk=current.pk).update(
            is_published=False, published_prepared_snapshot=None, publication_verification_required=False,
        )
    if action == "publish":
        MaterialVersion.objects.filter(pk=version.pk).update(
            is_published=True, published_prepared_snapshot_id=snapshot_id, sealed_at=now,
        )
        incoming.update(is_active=True)
    material.publication_revision += 1
    material.save(update_fields=["publication_revision"])
    finish_command(command, version)
    event = PublicationEvent.objects.create(
        command=command, material=material, previous_version=current,
        next_version=version if action == "publish" else None, snapshot_id=snapshot_id,
        publication_revision=material.publication_revision, created_at=now,
    )
    return event, False


@transaction.atomic
def rebase_draft(session, material_id, version_id, operation_id, draft_revision, publication_revision):
    actor = support_actor(session, lock=True)
    validate_revision(draft_revision, minimum=1)
    validate_revision(publication_revision)
    material = locked_material(material_id)
    version = lock_version(material, version_id)
    require_draft_access(actor, version)
    command, created = reserve_command(actor, operation_id, "rebase", material_id, version_id,
                                       {"publication_revision": publication_revision}, draft_revision)
    if not created:
        return command, True
    if version.sealed_at is not None:
        raise AccessError("draft_sealed", 409)
    check_publication_revision(material, publication_revision)
    if version.draft_revision != draft_revision:
        raise AccessError("draft_revision_conflict", 409, current_draft_revision=version.draft_revision)
    supersede_preparation(version)
    version.base_publication_revision = publication_revision
    version.draft_revision += 1
    version.updated_by, version.updated_at = actor, database_time()
    version.save(update_fields=["base_publication_revision", "draft_revision", "current_preparation", "updated_by", "updated_at"])
    finish_command(command, version)
    return command, False


@transaction.atomic
def isolate_restored_publications():
    if not quarantine_incident():
        raise ValueError("Запустите команду в карантине восстановления.")
    ids = list(Material.objects.select_for_update().order_by("pk").values_list("pk", flat=True))
    return MaterialVersion.objects.filter(material_id__in=ids, is_published=True).update(
        publication_verification_required=True,
    )
