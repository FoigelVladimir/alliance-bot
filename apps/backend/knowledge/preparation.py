from hashlib import sha256
from uuid import uuid4

from django.db import transaction

from accounts.services import AccessError
from jobs.models import BackgroundTask as Task
from jobs.services import READY, LeaseLost, enqueue
from storage.services import database_time
from support.services import support_actor

from .indexing import enqueue_index
from .models import MaterialPreparation, MaterialVersion, SourceSnapshot
from .services import TEXT_BYTES_LIMIT, finish_command, locked_material, require_draft_access, reserve_command

TASK_TYPE = "knowledge.prepare_text"
ACTIVE = (*READY, Task.Status.PROCESSING)


@transaction.atomic
def request_preparation(session, material_id, version_id, operation_id, revision):
    actor = support_actor(session, lock=True)
    material = locked_material(material_id)
    version = MaterialVersion.objects.select_for_update().filter(pk=version_id, material=material).first()
    if version is None:
        raise AccessError("draft_not_found", 404)
    require_draft_access(actor, version)
    if version.source_type != "text":
        from .sources import raw_access
        raw_access(actor, version)
    if type(revision) is not int or not 0 < revision < 2**63:
        raise AccessError("invalid_request", 400)
    command, created = reserve_command(actor, operation_id, "prepare", material_id, version_id, {}, revision)
    if not created:
        return command.materialpreparation, True
    if version.sealed_at is not None:
        raise AccessError("draft_sealed", 409)
    if version.draft_revision != revision:
        raise AccessError("draft_revision_conflict", 409, current_draft_revision=version.draft_revision)
    if version.current_source_read_id and version.current_source_read.task.status in ACTIVE:
        raise AccessError("preparation_in_progress", 409)
    previous = version.current_preparation
    previous_state = preparation_data(previous)
    if previous_state["status"] == "preparing" or previous_state.get("index_status") == "indexing":
        raise AccessError("preparation_in_progress", 409)
    preparation_id = uuid4()
    task = enqueue(TASK_TYPE, preparation_id, {
        "version_id": version.pk, "draft_revision": revision, "preparation_id": str(preparation_id),
    })
    preparation = MaterialPreparation.objects.create(
        pk=preparation_id, command=command, version=version, draft_revision=revision, task=task,
        access_generation=actor.access_generation,
    )
    version.current_preparation = preparation
    version.save(update_fields=["current_preparation"])
    finish_command(command, version)
    return preparation, False


def prepare_text_task(task):
    payload = task.payload
    version = MaterialVersion.objects.filter(
        pk=payload["version_id"], draft_revision=payload["draft_revision"],
        current_preparation_id=payload["preparation_id"], current_preparation__task_id=task.pk,
        sealed_at__isnull=True,
    ).first()
    if version is None:
        raise LeaseLost(task.pk)
    source_reason = ""
    text = version.source_text
    if version.source_type != "text":
        from .sources import authorized, prepare_source_text
        from .table_sources import SourceError
        record = version.current_preparation
        if not authorized(record, record.command.actor):
            source_reason = "source_access_changed"
        else:
            try:
                text = prepare_source_text(version)
            except SourceError as error:
                source_reason = error.code
            except OSError:
                source_reason = "source_file_unavailable"
    source_bytes = text.encode("utf-8")
    reason = source_reason
    if not reason and not text.strip():
        reason = "empty_source"
    elif len(source_bytes) > TEXT_BYTES_LIMIT:
        reason = "text_too_large"

    def save_snapshot():
        if version.source_type != "text":
            from accounts.services import lock_profiles
            from .sources import authorized
            record = version.current_preparation
            actor = lock_profiles(record.command.actor_id).get(record.command.actor_id)
            if not authorized(record, actor):
                return {"reason": "source_access_changed"}
        locked_material(version.material_id)
        current = MaterialVersion.objects.select_for_update().get(pk=version.pk)
        if (current.draft_revision != version.draft_revision or
                current.current_preparation_id != version.current_preparation_id or current.sealed_at is not None):
            raise LeaseLost(task.pk)
        if reason:
            return {"reason": reason}
        snapshot = SourceSnapshot.objects.create(
            preparation_id=current.current_preparation_id, prepared_text=text,
            source_sha256=sha256(source_bytes).hexdigest(), created_at=database_time(),
            original_file=current.source_file, table_snapshot=current.current_table_snapshot,
            selection_snapshot=current.table_selection,
        )
        enqueue_index(snapshot)
        # The worker checks the task lease before committing the snapshot and indexing task.
        return {"snapshot_id": snapshot.pk}

    return save_snapshot


def preparation_data(preparation):
    if preparation is None:
        return {"status": "not_prepared"}
    task = preparation.task
    result = task.result or {}
    if task.status in ACTIVE:
        status = "preparing"
    elif task.status == Task.Status.DONE and "snapshot_id" in result:
        status = "source_prepared"
    else:
        status = "failed"
    generation = getattr(preparation.snapshot, "generation", None) if status == "source_prepared" else None
    index_status = "not_indexed"
    index_reason = None
    if generation:
        index_task = generation.task
        if index_task.status in ACTIVE:
            index_status = "indexing"
        elif (index_task.status == Task.Status.DONE and generation.fragment_count > 0
              and (index_task.result or {}).get("fragment_count") == generation.fragment_count):
            index_status = "ready"
        else:
            index_status = "failed"
            index_reason = (index_task.result or {}).get("reason")
    return {
        "index_status": index_status, "index_reason": index_reason,
        "id": str(preparation.pk), "draft_revision": preparation.draft_revision, "status": status,
        "reason": result.get("reason"),
        "snapshot_id": str(result["snapshot_id"]) if status == "source_prepared" else None,
    }
