import os
import re
import time
from contextlib import nullcontext
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, StreamingHttpResponse
from django.utils.http import content_disposition_header

from accounts.policies import has_work_access, quarantine_incident
from accounts.services import AccessError, lock_profiles
from jobs.models import BackgroundTask as Task
from jobs.services import ClockTimestamp, LeaseLost, claim, enqueue, finish, retry
from storage.cas import CorruptObject, ObjectStore, lock_digest
from storage.models import FileObject, FileReadLease
from storage.services import collect_object, database_time

from .attachment_source import MAX_BYTES, PREVIEW_TYPES, SourceError, verified_type
from .models import Attachment, AttachmentPreparation, InboundRoute, Message, Ticket
from .queries import allowed_communities
from .services import reserve_command, support_actor

FETCH = "fetch_vk_support_attachment"
COLLECT = "collect_file_object"
COPY_LIFETIME = timedelta(hours=168)
READ_LIFETIME = timedelta(minutes=5)
RETRYABLE = {"fetch_budget_exhausted", "storage_unavailable"}


def locked_attachment(attachment_id):
    message_id = Attachment.objects.filter(pk=attachment_id).values_list("message_id", flat=True).first()
    message = Message.objects.select_for_update().filter(pk=message_id).first()
    attachment = Attachment.objects.select_for_update().filter(pk=attachment_id, message=message).first()
    if message is None or attachment is None:
        raise AccessError("not_found", 404)
    return message, attachment


def may_read(actor, message):
    communities = allowed_communities(actor)
    if message.ticket_id:
        return Ticket.objects.filter(
            pk=message.ticket_id, dialog_id=message.dialog_id, dialog__cursor_id__in=communities,
        ).exists()
    return InboundRoute.objects.filter(
        message=message, dialog_id=message.dialog_id, dialog__cursor_id__in=communities, status="needs_review",
    ).exists()


def require_visible(actor, message):
    if not may_read(actor, message):
        raise AccessError("not_found", 404)


def state_data(attachment, now):
    state, reason = attachment.state, attachment.reason_code
    expired = state == "ready" and attachment.expires_at <= now
    if expired:
        state, reason = "metadata_only", "cache_expired"
    actions = []
    if state == "ready":
        actions = ["download"]
        if attachment.verified_content_type in PREVIEW_TYPES:
            actions.append("content")
    elif (attachment.source_ref or {}).get("url") and (
        state == "metadata_only" or state == "unavailable" and reason in RETRYABLE
    ):
        actions = ["prepare"]
    elif state == "metadata_only" and not (attachment.source_ref or {}).get("url"):
        state, reason = "unavailable", reason or "source_unavailable"
    return {
        "id": str(attachment.pk), "type": attachment.attachment_type, "state": state, "reason": reason,
        "actions": actions, "expires_at": attachment.expires_at if state == "ready" else None,
        "size_bytes": attachment.file_object.size_bytes if state == "ready" else None,
    }


def drop_copy(attachment):
    digest = attachment.file_object_id
    attachment.state = "metadata_only"
    attachment.reason_code = "cache_expired"
    attachment.file_object_id = attachment.prepared_at = attachment.expires_at = attachment.verified_content_type = None
    attachment.save(update_fields=["state", "reason_code", "file_object", "prepared_at", "expires_at", "verified_content_type"])
    enqueue(COLLECT, uuid4(), {"digest": digest})


@transaction.atomic
def prepare(session, attachment_id, command_id):
    actor = support_actor(session, lock=True)
    message, attachment = locked_attachment(attachment_id)
    require_visible(actor, message)
    command, created = reserve_command(actor, command_id, "prepare_attachment", "attachment", attachment_id, {})
    now = database_time()
    state = state_data(attachment, now)
    if not created:
        return AttachmentPreparation.objects.get(command=command), state, True
    if "prepare" not in state["actions"]:
        code = {"ready": "attachment_already_ready", "preparing": "attachment_preparing"}.get(
            state["state"], "attachment_unavailable",
        )
        raise AccessError(code, 409, attachment=state)
    if attachment.state == "ready":
        drop_copy(attachment)
    preparation = AttachmentPreparation.objects.create(
        attachment=attachment, initiator=actor, command=command, access_generation=actor.access_generation,
    )
    attachment.state, attachment.reason_code = "preparing", None
    attachment.save(update_fields=["state", "reason_code"])
    enqueue(FETCH, command_id, {"preparation_id": preparation.pk, "attachment_id": attachment_id})
    return preparation, state_data(attachment, now), False


def fetch_records(task):
    p = AttachmentPreparation.objects.filter(
        pk=task.payload["preparation_id"], attachment_id=task.payload["attachment_id"],
        command__client_operation_id=task.idempotency_key,
    ).first()
    if p is None:
        raise LeaseLost(task.pk)
    actor = lock_profiles(p.initiator_id).get(p.initiator_id)
    message, attachment = locked_attachment(p.attachment_id)
    p = AttachmentPreparation.objects.select_for_update().get(pk=p.pk)
    if p.state != "active" or attachment.state != "preparing":
        raise LeaseLost(task.pk)
    return actor, message, attachment, p


def valid_initiator(actor, message, preparation):
    return (
        has_work_access(actor) and not actor.disabled and actor.access_generation == preparation.access_generation
        and not quarantine_incident() and actor.bindings.filter(active=True).exists() and may_read(actor, message)
    )


def end_preparation(p, attachment, state, reason, *, attachment_state=None, now=None):
    p.state, p.reason_code, p.finished_at = state, reason, now or database_time()
    p.save(update_fields=["state", "reason_code", "finished_at"])
    if attachment_state:
        attachment.state, attachment.reason_code = attachment_state, reason
        attachment.save(update_fields=["state", "reason_code"])


def lease_row(task):
    row = Task.objects.select_for_update().get(pk=task.pk)
    if row.status != Task.Status.PROCESSING or row.lease_token != task.lease_token or row.locked_until <= database_time():
        raise LeaseLost(task.pk)
    return row


def cancel_fetch(task, preparation, attachment):
    end_preparation(preparation, attachment, "cancelled", "access_revoked", attachment_state="metadata_only")
    finish(task, {"reason": "access_revoked"}, status=Task.Status.FAILED)


def run_fetch(task, loader):
    try:
        with transaction.atomic():
            actor, message, attachment, preparation = fetch_records(task)
            lease_row(task)
            if not valid_initiator(actor, message, preparation):
                cancel_fetch(task, preparation, attachment)
                return
        reason, content_type = None, None
        try:
            data = loader(attachment)
            content_type = verified_type(attachment.attachment_type, data)
        except SourceError as exc:
            reason = str(exc)
        except OSError:
            retry_fetch(task)
            return
        store = ObjectStore(settings.CAS_ROOT)
        staged_file = nullcontext() if reason else store.stage([data], MAX_BYTES)
        with staged_file as staged, transaction.atomic():
            if staged:
                lock_digest(staged.digest)
            actor, message, attachment, preparation = fetch_records(task)
            lease_row(task)
            if not valid_initiator(actor, message, preparation):
                cancel_fetch(task, preparation, attachment)
                return
            if reason:
                state = "unsupported" if reason == "unsupported_format" else "unavailable"
                end_preparation(preparation, attachment, "failed", reason, attachment_state=state)
                finish(task, {"reason": reason}, status=Task.Status.FAILED)
                return
            digest = store.publish(staged)
            obj, _ = FileObject.objects.get_or_create(sha256=digest, defaults={"size_bytes": staged.size})
            if obj.size_bytes != staged.size:
                raise CorruptObject(digest)
            now = database_time()
            attachment.file_object, attachment.state = obj, "ready"
            attachment.verified_content_type = content_type
            attachment.prepared_at, attachment.expires_at = now, now + COPY_LIFETIME
            attachment.save(update_fields=["file_object", "state", "verified_content_type", "prepared_at", "expires_at"])
            end_preparation(preparation, attachment, "succeeded", None, now=now)
            finish(task, {"preparation_id": preparation.pk})
    except (CorruptObject, OSError):
        try:
            retry_fetch(task)
        except LeaseLost:
            pass
    except LeaseLost:
        pass


@transaction.atomic
def retry_fetch(task, *, expired=False):
    actor, message, attachment, preparation = fetch_records(task)
    if expired:
        row = Task.objects.select_for_update().get(pk=task.pk)
        now = database_time()
        if row.status != Task.Status.PROCESSING or row.lease_token != task.lease_token or row.locked_until > now:
            return
        row.status = Task.Status.DEAD if row.attempt_count >= row.max_attempts else Task.Status.RETRY
        row.lease_token = row.locked_until = None
        row.scheduled_at = now + timedelta(seconds=2)
        row.updated_at = now
        if row.status == Task.Status.DEAD:
            row.result = {"reason": "fetch_budget_exhausted"}
        row.save(update_fields=["status", "lease_token", "locked_until", "scheduled_at", "updated_at", "result"])
    else:
        row = lease_row(task)
        if not valid_initiator(actor, message, preparation):
            cancel_fetch(task, preparation, attachment)
            return
        retry(task)
    if row.attempt_count >= row.max_attempts:
        end_preparation(preparation, attachment, "failed", "fetch_budget_exhausted", attachment_state="unavailable")


def run_fetch_once(loader):
    expired = Task.objects.filter(task_type=FETCH, status=Task.Status.PROCESSING,
                                  locked_until__lte=ClockTimestamp()).order_by("locked_until").first()
    if expired:
        try:
            retry_fetch(expired, expired=True)
        except LeaseLost:
            pass
    task = claim([FETCH])
    if task:
        run_fetch(task, loader)
    return bool(expired or task)


def expire_copies(limit=100):
    candidates = Attachment.objects.filter(state="ready", expires_at__lte=ClockTimestamp()).order_by("expires_at")
    snapshots = list(candidates.values_list("pk", "file_object_id", "prepared_at", "expires_at")[:limit])
    for pk, digest, prepared, expires in snapshots:
        with transaction.atomic():
            a = Attachment.objects.select_for_update().filter(
                pk=pk, state="ready", file_object_id=digest, prepared_at=prepared, expires_at=expires,
            ).first()
            if a and a.expires_at <= database_time():
                drop_copy(a)
    return len(snapshots)


def collect_or_reschedule(digest):
    deadline = collect_object(digest)
    if deadline is not None:
        enqueue(COLLECT, uuid4(), {"digest": digest}, scheduled_at=deadline)
    return {}


def collect_task(task):
    return lambda: collect_or_reschedule(task.payload["digest"])


def sweep_orphans():
    for digest in FileObject.objects.values_list("pk", flat=True).iterator():
        collect_object(digest)
    ObjectStore(settings.CAS_ROOT).sweep()


def recover_preparations():
    """Run against a restored pair before starting web or worker processes."""
    with transaction.atomic():
        for preparation in AttachmentPreparation.objects.filter(state="active").iterator():
            _, attachment = locked_attachment(preparation.attachment_id)
            end_preparation(preparation, attachment, "cancelled", "recovered_from_backup",
                            attachment_state="metadata_only")
        Task.objects.filter(task_type=FETCH, status__in=[Task.Status.PENDING, Task.Status.RETRY, Task.Status.PROCESSING]).update(
            status=Task.Status.FAILED, result={"reason": "recovered_from_backup"},
            locked_until=None, lease_token=None, updated_at=ClockTimestamp(),
        )
        FileReadLease.objects.all().delete()
    while expire_copies():
        pass


def read_state(session, attachment_id):
    with transaction.atomic():
        actor = support_actor(session, lock=True)
        message, attachment = locked_attachment(attachment_id)
        require_visible(actor, message)
        return state_data(attachment, database_time())


class AttachmentStream:
    def __init__(self, file, remaining, lease_id, deadline):
        self.file, self.remaining, self.lease_id, self.deadline = file, remaining, lease_id, deadline

    def __iter__(self):
        return self

    def __next__(self):
        if self.remaining <= 0 or time.monotonic() >= self.deadline:
            raise StopIteration
        chunk = self.file.read(min(65536, self.remaining))
        if not chunk:
            raise StopIteration
        self.remaining -= len(chunk)
        return chunk

    def close(self):
        self.file.close()
        FileReadLease.objects.filter(pk=self.lease_id).delete()


def audio_range(header, size):
    match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", header)
    if not match or not any(match.groups()) or not size:
        return None
    first, last = match.groups()
    if not first:
        return (max(0, size - int(last)), size - 1) if int(last) else None
    start, end = int(first), min(int(last), size - 1) if last else size - 1
    return (start, end) if start <= end else None


def attachment_bytes(session, attachment_id, *, head, download, range_header):
    # Acquire the hash before message locks. If publication changed it, retry from scratch.
    while True:
        digest = Attachment.objects.filter(pk=attachment_id).values_list("file_object_id", flat=True).first()
        with transaction.atomic():
            if digest:
                lock_digest(digest)
            actor = support_actor(session, lock=True)
            message, attachment = locked_attachment(attachment_id)
            require_visible(actor, message)
            if digest != attachment.file_object_id:
                continue
            return build_response(attachment, head, download, range_header)


def build_response(attachment, head, download, range_header):
    started = time.monotonic()
    now = database_time()
    state = state_data(attachment, now)
    if state["state"] != "ready":
        code = "attachment_cache_expired" if state["reason"] == "cache_expired" else "attachment_unavailable"
        raise AccessError(code, 409, attachment=state)
    if not download and "content" not in state["actions"]:
        raise AccessError("attachment_preview_unavailable", 409, attachment=state)
    size, content_type = state["size_bytes"], attachment.verified_content_type
    partial = content_type == "audio/ogg" and range_header
    bounds = audio_range(range_header, size) if partial else (0, size - 1)
    if bounds is None:
        response = HttpResponse(status=416, headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
        return response
    start, end = bounds
    length = end - start + 1
    file = None
    try:
        file = ObjectStore(settings.CAS_ROOT).open(attachment.file_object_id)
        if os.fstat(file.fileno()).st_size != size:
            raise CorruptObject
        file.seek(start)
    except (OSError, CorruptObject):
        if file:
            file.close()
        raise AccessError("attachment_storage_unavailable", 503) from None
    status = 206 if partial else 200
    if head:
        file.close()
        response = HttpResponse(status=status, content_type=content_type)
    else:
        try:
            lease = FileReadLease.objects.create(
                id=uuid4(), file_object_id=attachment.file_object_id, expires_at=now + READ_LIFETIME,
            )
            stream = AttachmentStream(file, length, lease.pk, started + READ_LIFETIME.total_seconds())
            response = StreamingHttpResponse(stream, status=status, content_type=content_type)
        except Exception:
            file.close()
            raise
    response["Content-Length"] = str(length)
    if content_type == "audio/ogg":
        response["Accept-Ranges"] = "bytes"
    if partial:
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    if download:
        extension = {"image/jpeg": "jpg", "image/png": "png", "audio/ogg": "ogg", "application/pdf": "pdf"}[content_type]
        response["Content-Disposition"] = content_disposition_header(True, f"attachment-{attachment.pk}.{extension}")
    return response
