import hashlib
import json

from django.db import transaction
from django.db.models import Exists, OuterRef, Q

from accounts.services import AccessError
from jobs.models import BackgroundTask as Task
from jobs.services import ClockTimestamp, READY
from storage.services import database_time
from support.queries import allowed_communities
from support.services import support_actor

from .models import FragmentGeneration, Material, MaterialCommand, MaterialVersion, SupportOrigin

TEXT_BYTES_LIMIT = 64 * 1024


def visible_versions(actor):
    return MaterialVersion.objects.filter(
        Q(sealed_at__isnull=False) | Q(support_origin__isnull=True)
        | Q(support_origin__ticket__dialog__cursor_id__in=allowed_communities(actor)),
    )


def visible_materials(actor):
    return Material.objects.filter(Exists(visible_versions(actor).filter(material_id=OuterRef("pk"))))


def require_draft_access(actor, version):
    if not visible_versions(actor).filter(pk=version.pk).exists():
        raise AccessError("draft_not_found", 404)



def validate_content(content, *, source_type="text"):
    expected = {"title", "description", "tags", "source_type", "source_text"}
    if set(content) != expected or any(not isinstance(content[key], str) for key in expected - {"tags"}):
        raise AccessError("invalid_request", 400)
    if content["source_type"] != source_type or (source_type != "text" and content["source_text"]):
        raise AccessError("unsupported_source_type", 422)
    tags = content["tags"]
    try:
        text_bytes = len(content["source_text"].encode("utf-8"))
        content["title"].encode("utf-8")
        content["description"].encode("utf-8")
    except UnicodeEncodeError:
        raise AccessError("invalid_request", 400) from None
    if not content["title"].strip() or len(content["title"]) > 200 or len(content["description"]) > 2000:
        raise AccessError("invalid_content", 422)
    if (not isinstance(tags, list) or len(tags) > 20
            or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 64 for tag in tags)):
        raise AccessError("invalid_tags", 422)
    try:
        for tag in tags:
            tag.encode("utf-8")
    except UnicodeEncodeError:
        raise AccessError("invalid_request", 400) from None
    if any("\0" in value for value in [content["title"], content["description"], content["source_text"], *tags]):
        raise AccessError("invalid_request", 400)
    if text_bytes > TEXT_BYTES_LIMIT:
        raise AccessError("text_too_large", 422)


def reserve_command(actor, operation_id, action, material_id, version_id, content, revision=None):
    fingerprint = hashlib.sha256(json.dumps(
        {"material_id": material_id, "version_id": version_id, "content": content, "revision": revision},
        sort_keys=True, ensure_ascii=True,
    ).encode()).hexdigest()
    command, created = MaterialCommand.objects.get_or_create(
        actor=actor, client_operation_id=operation_id,
        defaults={"action": action, "request_fingerprint": fingerprint},
    )
    if not created and (command.action != action or command.request_fingerprint != fingerprint):
        raise AccessError("operation_mismatch", 409)
    return command, created


def finish_command(command, version):
    command.material = version.material
    command.version = version
    command.saved_draft_revision = version.draft_revision
    command.save(update_fields=["material", "version", "saved_draft_revision"])


def locked_material(material_id):
    material = Material.objects.select_for_update().filter(pk=material_id).first()
    if material is None:
        raise AccessError("material_not_found", 404)
    return material


@transaction.atomic
def create_material(session, operation_id, content):
    actor = support_actor(session, lock=True)
    validate_content(content)
    command, created = reserve_command(actor, operation_id, "create_material", None, None, content)
    if not created:
        return command, True
    material = Material.objects.create(created_by=actor)
    version = MaterialVersion.objects.create(material=material, created_by=actor, updated_by=actor, **content)
    finish_command(command, version)
    return command, False


@transaction.atomic
def create_draft(session, material_id, operation_id, content):
    actor = support_actor(session, lock=True)
    material = locked_material(material_id)
    if not visible_materials(actor).filter(pk=material.pk).exists():
        raise AccessError("material_not_found", 404)
    validate_content(content)
    command, created = reserve_command(actor, operation_id, "create_draft", material.pk, None, content)
    if not created:
        return command, True
    version = MaterialVersion.objects.create(
        material=material, created_by=actor, updated_by=actor,
        base_publication_revision=material.publication_revision, **content,
    )
    finish_command(command, version)
    return command, False


@transaction.atomic
def save_draft(session, material_id, version_id, operation_id, expected_revision, content):
    actor = support_actor(session, lock=True)
    material = locked_material(material_id)
    version = MaterialVersion.objects.select_for_update().filter(pk=version_id, material=material).first()
    if version is None:
        raise AccessError("draft_not_found", 404)
    require_draft_access(actor, version)
    validate_content(content, source_type=version.source_type)
    if type(expected_revision) is not int or not 0 < expected_revision < 2**63:
        raise AccessError("invalid_request", 400)
    command, created = reserve_command(actor, operation_id, "save_draft", material.pk, version.pk, content, expected_revision)
    if not created:
        return command, True
    if version.sealed_at is not None:
        raise AccessError("draft_sealed", 409)
    if version.draft_revision != expected_revision:
        raise AccessError("draft_revision_conflict", 409, current_draft_revision=version.draft_revision)
    supersede_preparation(version)
    for key, value in content.items():
        setattr(version, key, value)
    version.draft_revision += 1
    version.updated_by, version.updated_at = actor, database_time()
    version.save(update_fields=[*content, "draft_revision", "updated_by", "updated_at", "current_preparation"])
    finish_command(command, version)
    return command, False


def supersede_preparation(version):
    if version.current_source_read_id:
        Task.objects.filter(pk=version.current_source_read.task_id, status__in=(*READY, Task.Status.PROCESSING)).update(
            status=Task.Status.FAILED, result={"reason": "superseded"}, lease_token=None, locked_until=None,
            updated_at=ClockTimestamp(),
        )
    if version.current_preparation_id:
        task_ids = [version.current_preparation.task_id]
        task_ids.extend(FragmentGeneration.objects.filter(
            snapshot__preparation_id=version.current_preparation_id,
        ).values_list("task_id", flat=True))
        Task.objects.filter(pk__in=task_ids, status__in=(*READY, Task.Status.PROCESSING)).update(
            status=Task.Status.FAILED, result={"reason": "superseded"}, lease_token=None, locked_until=None,
            updated_at=ClockTimestamp(),
        )
    version.current_preparation = None


def support_ticket(actor, ticket_id):
    from support.models import Ticket

    ticket = Ticket.objects.filter(pk=ticket_id, dialog__cursor_id__in=allowed_communities(actor)).first()
    if ticket is None:
        raise AccessError("ticket_not_found", 404)
    return ticket


def eligible_messages(ticket):
    from support.models import Message, OutgoingResponse

    messages = Message.objects.filter(ticket=ticket, dialog_id=ticket.dialog_id).exclude(text="")
    return (
        messages.filter(direction="inbound"),
        messages.filter(direction="outbound_manager", outgoingresponse__status=OutgoingResponse.Status.SENT,
                        outgoingresponse__ticket=ticket),
    )


@transaction.atomic
def create_from_support(session, operation_id, ticket_id, question_id, answer_id, material_id=None):
    actor = support_actor(session, lock=True)
    ticket = support_ticket(actor, ticket_id)
    material = locked_material(material_id) if material_id else None
    if material is not None and not visible_materials(actor).filter(pk=material.pk).exists():
        raise AccessError("material_not_found", 404)
    questions, answers = eligible_messages(ticket)
    question = questions.filter(pk=question_id).first()
    answer = answers.filter(pk=answer_id).first()
    if question is None or answer is None:
        raise AccessError("support_message_not_found", 404)
    command, created = reserve_command(actor, operation_id, "from_support", material_id, None, {
        "ticket_id": ticket_id, "question_id": question_id, "answer_id": answer_id,
    })
    if not created:
        return command, True
    text = f"Вопрос\n{question.text}\n\nОтвет\n{answer.text}"
    content = {"title": "Черновик из обращения", "description": "", "tags": [],
               "source_type": "text", "source_text": text}
    validate_content(content)
    if material is None:
        material = Material.objects.create(created_by=actor)
    version = MaterialVersion.objects.create(
        material=material, created_by=actor, updated_by=actor,
        base_publication_revision=material.publication_revision, **content,
    )
    SupportOrigin.objects.create(version=version, ticket=ticket, question=question, answer=answer)
    finish_command(command, version)
    return command, False
