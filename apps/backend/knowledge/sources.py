"""Durable private source reads, snapshot-local choices, and explicit refresh."""

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.db import transaction

from accounts.policies import has_work_access, quarantine_incident
from accounts.services import AccessError, lock_profiles
from jobs.services import LeaseLost, enqueue
from storage.cas import ObjectStore
from storage.models import FileObject
from storage.services import database_time
from support.queries import allowed_communities
from support.services import support_actor

from .extractors import FILE_LIMIT, extract_isolated
from .google_sources import read_google, spreadsheet_id
from .indexing import enqueue_index
from .models import MaterialPreparation, MaterialVersion, SourceRead, SourceSnapshot, TableReadSnapshot
from .services import finish_command, locked_material, require_draft_access, reserve_command, supersede_preparation
from .table_sources import SourceError, remap_selection, selected_text

READ_TASK = "knowledge.read_source"
SOURCE_MESSAGES = {
    "google_not_configured": "Чтение Google Таблиц ещё не настроено. Обратитесь к администратору.",
    "google_access_denied": "Нет доступа к Google Таблице. Проверьте ссылку и права сервисного аккаунта.",
    "google_unavailable": "Google Таблицы сейчас недоступны. Повторите обновление позже.",
    "google_credentials_unavailable": "Не удалось подключить сервисный аккаунт. Обратитесь к администратору.",
    "source_access_changed": "Доступ сотрудника изменился. Источник не подготовлен.",
    "source_too_large": "Источник превышает допустимый размер или число ячеек. Уменьшите файл.",
    "table_limits_exceeded": "Таблица превышает допустимые пределы. Уменьшите число листов, строк или колонок.",
    "text_too_large": "Готовый текст превышает 64 КиБ. Уменьшите источник или выберите меньше колонок.",
    "pdf_text_layer_required": "В PDF есть страницы без текста. Загрузите документ с текстовым слоем.",
    "encrypted_source": "Защищённый PDF нельзя прочитать. Загрузите документ без шифрования.",
    "empty_source": "В источнике нет текста для подготовки.",
    "superseded": "Источник был изменён. Подготовьте текущую сохранённую правку.",
    "empty_table_selection": "В выбранных колонках нет обычных значений. Формулы исключены.",
}


def source_message(reason):
    return SOURCE_MESSAGES.get(reason, "Не удалось прочитать источник. Проверьте формат и содержимое и повторите загрузку.") if reason else ""


def raw_access(actor, version):
    require_draft_access(actor, version)
    origin = getattr(version, "support_origin", None)
    if origin and not allowed_communities(actor).filter(cursor_id=origin.ticket.dialog.cursor_id).exists():
        raise AccessError("draft_not_found", 404)


def locked_version(actor, material_id, version_id):
    material = locked_material(material_id)
    version = MaterialVersion.objects.select_for_update().filter(pk=version_id, material=material).first()
    if version is None:
        raise AccessError("draft_not_found", 404)
    raw_access(actor, version)
    return version


def editable(version, revision):
    if type(revision) is not int or not 0 < revision < 2**63:
        raise AccessError("invalid_request", 400)
    if version.sealed_at is not None:
        raise AccessError("draft_sealed", 409)
    if revision != version.draft_revision:
        raise AccessError("draft_revision_conflict", 409, current_draft_revision=version.draft_revision)


def enqueue_read(command, version, actor, previous_snapshot=None, previous_selection=None):
    read_id = uuid4()
    task = enqueue(READ_TASK, read_id, {"read_id": str(read_id)})
    record = SourceRead.objects.create(
        pk=read_id, command=command, version=version, draft_revision=version.draft_revision, task=task,
        source_file=version.source_file, google_id=version.source_google_id,
        access_generation=actor.access_generation, previous_snapshot=previous_snapshot,
        previous_selection=previous_selection or [],
    )
    version.current_source_read = record
    version.current_table_snapshot = None
    version.table_selection = []
    version.current_preparation = None
    version.save()
    finish_command(command, version)
    return record


def upload_source(session, material_id, version_id, operation_id, revision, upload):
    support_actor(session)
    name = Path(upload.name.replace("\\", "/")).name
    kind = Path(name).suffix.lower().lstrip(".")
    if kind not in {"txt", "docx", "pdf", "xlsx"} or len(name) > 200:
        raise AccessError("unsupported_source_type", 422)
    if upload.size > FILE_LIMIT:
        raise AccessError("source_file_too_large", 413)
    store = ObjectStore(settings.CAS_ROOT)
    try:
        with store.stage(upload.chunks(), FILE_LIMIT) as staged, transaction.atomic():
            actor = support_actor(session, lock=True)
            version = locked_version(actor, material_id, version_id)
            command, created = reserve_command(actor, operation_id, "upload_source", material_id, version_id,
                                               {"sha256": staged.digest, "filename": name, "type": kind}, revision)
            if not created:
                return command, True
            editable(version, revision)
            previous, selection = version.current_table_snapshot, version.table_selection
            supersede_preparation(version)
            digest = store.publish(staged)
            file, _ = FileObject.objects.get_or_create(pk=digest, defaults={"size_bytes": staged.size})
            version.source_file, version.source_filename = file, name
            version.source_type, version.source_text, version.source_google_id = kind, "", ""
            version.draft_revision += 1
            version.updated_by, version.updated_at = actor, database_time()
            enqueue_read(command, version, actor, previous, selection)
            return command, False
    except ValueError:
        raise AccessError("source_file_too_large", 413) from None


@transaction.atomic
def connect_google(session, material_id, version_id, operation_id, revision, url):
    actor = support_actor(session, lock=True)
    version = locked_version(actor, material_id, version_id)
    try:
        identifier = spreadsheet_id(url)
    except SourceError as error:
        raise AccessError(error.code, 422) from None
    command, created = reserve_command(actor, operation_id, "connect_google", material_id, version_id,
                                       {"google_id": identifier}, revision)
    if not created:
        return command, True
    editable(version, revision)
    previous, selection = version.current_table_snapshot, version.table_selection
    supersede_preparation(version)
    version.source_type, version.source_text = "google", ""
    version.source_file, version.source_filename, version.source_google_id = None, "", identifier
    version.draft_revision += 1
    version.updated_by, version.updated_at = actor, database_time()
    enqueue_read(command, version, actor, previous, selection)
    return command, False


@transaction.atomic
def refresh_google(session, material_id, version_id, operation_id, publication_revision):
    actor = support_actor(session, lock=True)
    source = locked_version(actor, material_id, version_id)
    if type(publication_revision) is not int or not 0 <= publication_revision < 2**63:
        raise AccessError("invalid_request", 400)
    if source.source_type != "google" or not source.source_google_id:
        raise AccessError("unsupported_source_type", 422)
    command, created = reserve_command(actor, operation_id, "refresh_google", material_id, version_id, {}, publication_revision)
    if not created:
        return command, True
    if source.material.publication_revision != publication_revision:
        raise AccessError("publication_revision_conflict", 409)
    version = MaterialVersion.objects.create(
        material=source.material, created_by=actor, updated_by=actor, title=source.title,
        description=source.description, tags=source.tags, source_type="google", source_google_id=source.source_google_id,
        base_publication_revision=publication_revision,
    )
    enqueue_read(command, version, actor, source.current_table_snapshot, source.table_selection)
    return command, False


@transaction.atomic
def select_table(session, material_id, version_id, operation_id, revision, snapshot_id, selection):
    actor = support_actor(session, lock=True)
    version = locked_version(actor, material_id, version_id)
    command, created = reserve_command(actor, operation_id, "select_table", material_id, version_id,
                                       {"source_snapshot_id": str(snapshot_id), "selection": selection}, revision)
    if not created:
        return command, True
    editable(version, revision)
    if str(version.current_table_snapshot_id) != str(snapshot_id) or not version.current_table_snapshot_id:
        raise AccessError("source_snapshot_changed", 409)
    try:
        selected_text(version.current_table_snapshot.data, selection)
    except SourceError as error:
        raise AccessError(error.code, 422) from None
    supersede_preparation(version)
    version.table_selection = selection
    version.draft_revision += 1
    version.updated_by, version.updated_at = actor, database_time()
    preparation_id = uuid4()
    task = enqueue("knowledge.prepare_text", preparation_id,
                   {"version_id": version.pk, "draft_revision": version.draft_revision, "preparation_id": str(preparation_id)})
    version.current_preparation = MaterialPreparation.objects.create(
        pk=preparation_id, command=command, version=version, draft_revision=version.draft_revision, task=task,
        access_generation=actor.access_generation,
    )
    version.save()
    finish_command(command, version)
    return command, False


def authorized(record, actor):
    if (not actor or not has_work_access(actor) or actor.disabled or quarantine_incident()
            or actor.access_generation != record.access_generation or not actor.bindings.filter(active=True).exists()
            or not allowed_communities(actor).exists()):
        return False
    try:
        raw_access(actor, record.version)
    except AccessError:
        return False
    return True


def read_file(file):
    with ObjectStore(settings.CAS_ROOT).open(file.pk) as stream:
        raw = stream.read(FILE_LIMIT + 1)
    if len(raw) != file.size_bytes or sha256(raw).hexdigest() != file.pk:
        raise SourceError("source_file_corrupt")
    return raw


def snapshot_text(version, preparation, text):
    snapshot = SourceSnapshot.objects.create(
        preparation=preparation, prepared_text=text, source_sha256=sha256(text.encode("utf-8")).hexdigest(),
        original_file=version.source_file, table_snapshot=version.current_table_snapshot,
        selection_snapshot=version.table_selection, created_at=database_time(),
    )
    enqueue_index(snapshot)
    return snapshot


def read_source_task(task):
    record = SourceRead.objects.select_related("command__actor", "version", "source_file", "previous_snapshot").get(task=task)
    actor = record.command.actor
    result, reason = None, ""
    if not authorized(record, actor):
        reason = "source_access_changed"
    else:
        try:
            result = {"table": read_google(record.google_id)} if record.google_id else extract_isolated(
                read_file(record.source_file), record.version.source_type)
        except SourceError as error:
            reason = error.code
        except OSError:
            reason = "source_file_unavailable"

    def save():
        current_actor = lock_profiles(actor.pk).get(actor.pk)
        locked_material(record.version.material_id)
        version = MaterialVersion.objects.select_for_update().get(pk=record.version_id)
        if (version.current_source_read_id != record.pk or version.draft_revision != record.draft_revision
                or version.sealed_at is not None):
            raise LeaseLost(task.pk)
        if not authorized(record, current_actor):
            return {"reason": "source_access_changed"}
        if reason:
            return {"reason": reason}
        if "table" in result:
            data = result["table"]
            selection, issues, hints = [], [], []
            if record.previous_snapshot_id and record.previous_selection:
                selection, issues, hints = remap_selection(record.previous_snapshot.data, data, record.previous_selection)
            raw = TableReadSnapshot.objects.create(read=record, data=data, mapping_issues=issues, mapping_suggestions=hints)
            version.current_table_snapshot, version.table_selection = raw, selection
            version.save(update_fields=["current_table_snapshot", "table_selection"])
            if not selection:
                return {"source_snapshot_id": raw.pk, "selection_required": True}
            try:
                text, excluded = selected_text(data, selection)
            except SourceError as error:
                return {"source_snapshot_id": raw.pk, "selection_required": True, "reason": error.code}
        else:
            text, excluded = result["text"], 0
        preparation = MaterialPreparation.objects.create(
            command=record.command, version=version, draft_revision=version.draft_revision, task=task,
            access_generation=record.access_generation,
        )
        snapshot = snapshot_text(version, preparation, text)
        version.current_preparation = preparation
        version.save(update_fields=["current_preparation"])
        return {"snapshot_id": snapshot.pk, "formula_cells_excluded": excluded}

    return save


def prepare_source_text(version):
    if version.source_type in {"google", "xlsx"}:
        if not version.current_table_snapshot_id:
            raise SourceError("table_selection_required")
        return selected_text(version.current_table_snapshot.data, version.table_selection)[0]
    if version.source_file_id:
        return extract_isolated(read_file(version.source_file), version.source_type)["text"]
    raise SourceError("source_file_unavailable")


def source_state(version):
    if version.current_table_snapshot_id:
        result = version.current_source_read.task.result if version.current_source_read_id else {}
        needs_choice = not version.table_selection or (not version.current_preparation_id and result.get("selection_required"))
        return {"status": "selection_required" if needs_choice else "table_ready",
                "source_snapshot_id": str(version.current_table_snapshot_id), "reason": result.get("reason"),
                "message": source_message(result.get("reason"))}
    if version.current_source_read_id:
        task = version.current_source_read.task
        from jobs.models import BackgroundTask
        from jobs.services import READY
        return {"status": "reading" if task.status in {*READY, BackgroundTask.Status.PROCESSING} else "read_finished",
                "reason": (task.result or {}).get("reason"), "message": source_message((task.result or {}).get("reason"))}
    return {"status": "text" if version.source_type == "text" else "not_read"}
