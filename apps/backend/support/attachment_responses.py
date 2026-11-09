"""Server-written clarification messages for attachments that the bot cannot read."""

import json
from hashlib import sha256

from django.db import connection

from .models import AIAnswer, AIAnswerInput, AIAnswerInputMessage, AIAnswerPart, AIPartDelivery, AIProcessingInput, Message

PROFILE = "attachment_text_request_v1"
MODEL_PROFILE = {"kind": "server_message", "version": 1}
PROMPT_TEXT = (
    "Я могу прочитать текст сообщения, но не содержимое вложений. "
    "Кратко опишите вопрос текстом. Чтобы обратиться к сотруднику вместе с вложением, "
    "напишите «Позвать менеджера»."
)


def save_prompt(run, now, lookup_ms=None):
    if not connection.in_atomic_block:
        raise RuntimeError("Attachment clarification requires the caller's transaction")
    snapshot = run.inputs.get()
    if not snapshot.messages.filter(attachments__isnull=False).exists():
        raise ValueError("Attachment clarification requires an original attachment")
    message_ids = list(snapshot.messages.order_by("pk").values_list("pk", flat=True))
    retrieval = {"profile": PROFILE}
    timings = {"lookup_ms": lookup_ms}
    fingerprint = {
        "dialog_id": run.dialog_id, "operation_id": str(run.operation_id), "mode_version": run.mode_version,
        "request_id": snapshot.request_id, "request_version": snapshot.request_version,
        "question": snapshot.question_text, "message_ids": message_ids, "text": PROMPT_TEXT,
        "model": MODEL_PROFILE, "retrieval": retrieval, "timings": timings,
    }
    answer = AIAnswer.objects.create(
        dialog_id=run.dialog_id, generation_operation_id=run.operation_id, mode_version_snapshot=run.mode_version,
        model_snapshot=MODEL_PROFILE, retrieval_snapshot=retrieval, timings_snapshot=timings,
        content_sha256=sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        created_at=now,
    )
    answer_input = AIAnswerInput.objects.create(
        answer=answer, request=snapshot.request, request_version_snapshot=snapshot.request_version,
        query_snapshot=snapshot.question_text, sources_snapshot=[],
    )
    AIAnswerInputMessage.objects.bulk_create([
        AIAnswerInputMessage(input=answer_input, message_id=message_id) for message_id in message_ids
    ])
    part = AIAnswerPart.objects.create(answer=answer, position=1, text_snapshot=PROMPT_TEXT)
    # This is a request for clarification, not a substantive answer to the question.
    AIPartDelivery.objects.create(part=part, created_at=now, updated_at=now)
    run.status, run.reason, run.answer, run.finished_at = "answer_saved", "", answer, now
    run.save(update_fields=["status", "reason", "answer", "finished_at"])
    return answer


def valid_prompt(answer):
    """Only the fixed server message with its original attachment may omit RAG sources."""
    if answer.model_snapshot != MODEL_PROFILE or answer.retrieval_snapshot != {"profile": PROFILE}:
        return False
    inputs, parts = list(answer.inputs.all()), list(answer.parts.all())
    if len(inputs) != 1 or len(parts) != 1:
        return False
    item, part = inputs[0], parts[0]
    messages = Message.objects.filter(aianswerinputmessage__input=item)
    original = AIProcessingInput.objects.filter(run__answer=answer).first()
    if (original is None or original.request_id != item.request_id
            or original.request_version != item.request_version_snapshot
            or original.question_text != item.query_snapshot
            or set(original.messages.values_list("pk", flat=True)) != set(messages.values_list("pk", flat=True))):
        return False
    if (item.sources_snapshot != [] or part.position != 1 or part.text_snapshot != PROMPT_TEXT
            or part.aianswerpartinput_set.exists()):
        return False
    if (item.request.dialog_id != answer.dialog_id
            or messages.exclude(dialog_id=answer.dialog_id).exists()):
        return False
    return messages.filter(attachments__isnull=False).exists()
