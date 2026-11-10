"""A fixed clarification prompt tied to the original context decision."""

import json
from hashlib import sha256

from django.db import connection

from .models import (AIAnswer, AIAnswerInput, AIAnswerInputMessage, AIAnswerPart, AIContextResolution,
                     AIPartDelivery, AIProcessingInput, Message)

PROFILE = "context_clarification_v1"
MODEL_PROFILE = {"kind": "server_message", "version": 1}
PROMPT_TEXT = (
    "Уточните, пожалуйста: напишите вопрос целиком — о чём речь и что нужно узнать. "
    "Чтобы обратиться к сотруднику, напишите «Позвать менеджера»."
)


def save_prompt(run, now):
    if not connection.in_atomic_block:
        raise RuntimeError("Context clarification requires the caller's transaction")
    source = run.inputs.get()
    ids = list(source.messages.order_by("pk").values_list("pk", flat=True))
    fingerprint = {"dialog_id": run.dialog_id, "operation_id": str(run.operation_id),
        "mode_version": run.mode_version, "request_id": source.request_id,
        "request_version": source.request_version, "question": source.question_text,
        "message_ids": ids, "text": PROMPT_TEXT, "profile": PROFILE}
    answer = AIAnswer.objects.create(dialog_id=run.dialog_id, generation_operation_id=run.operation_id,
        mode_version_snapshot=run.mode_version, model_snapshot=MODEL_PROFILE,
        retrieval_snapshot={"profile": PROFILE}, timings_snapshot={},
        content_sha256=sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        created_at=now)
    item = AIAnswerInput.objects.create(answer=answer, request_id=source.request_id,
        request_version_snapshot=source.request_version, query_snapshot=source.question_text, sources_snapshot=[])
    AIAnswerInputMessage.objects.bulk_create([AIAnswerInputMessage(input=item, message_id=pk) for pk in ids])
    part = AIAnswerPart.objects.create(answer=answer, position=1, text_snapshot=PROMPT_TEXT)
    AIPartDelivery.objects.create(part=part, created_at=now, updated_at=now)
    run.answer, run.status, run.reason, run.finished_at = answer, "answer_saved", "", now
    from .ai_join import completion_deadline

    run.join_until = completion_deadline(now)
    run.save(update_fields=["answer", "status", "reason", "finished_at", "join_until"])
    return answer


def valid_prompt(answer):
    if answer.model_snapshot != MODEL_PROFILE or answer.retrieval_snapshot != {"profile": PROFILE}:
        return False
    inputs, parts = list(answer.inputs.all()), list(answer.parts.all())
    if len(inputs) != 1 or len(parts) != 1:
        return False
    item, part = inputs[0], parts[0]
    original = AIProcessingInput.objects.filter(run__answer=answer).first()
    resolution = AIContextResolution.objects.filter(run__answer=answer, status="clarification").first()
    if original is None or resolution is None or resolution.decision.get("action") != "clarify":
        return False
    messages = Message.objects.filter(aianswerinputmessage__input=item)
    ids = set(messages.values_list("pk", flat=True))
    return (ids == {resolution.route.message_id} and ids == set(original.messages.values_list("pk", flat=True))
        and resolution.request_id == original.request_id == item.request_id
        and original.request_version == item.request_version_snapshot
        and original.question_text == item.query_snapshot and item.sources_snapshot == []
        and original.question_text == " ".join(resolution.route.message.text.split())
        and item.request.dialog_id == answer.dialog_id == resolution.route.dialog_id
        and not messages.exclude(dialog_id=answer.dialog_id).exists()
        and part.position == 1 and part.text_snapshot == PROMPT_TEXT and not part.aianswerpartinput_set.exists())
