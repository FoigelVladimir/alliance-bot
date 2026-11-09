"""Shared quality review of an immutable answer; no delivery or routing effects."""

import re
from datetime import timezone as datetime_timezone

from django.db import transaction
from django.utils import timezone

from accounts.services import AccessError

from .models import AIAnswer, AIAnswerReview, AIAnswerReviewEvent
from .queries import allowed_communities
from .services import reserve_command, support_actor

MAX_REVISION = 2**63 - 1
ACTION = "save_ai_answer_review"


def parameters(data):
    revision, flag, comment = (data[key] for key in ("expected_review_revision", "problem_flag", "comment"))
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9]+", revision):
        raise AccessError("invalid_request", 400)
    revision = revision.lstrip("0") or "0"
    if (len(revision) > 19 or int(revision) > MAX_REVISION
            or type(flag) is not bool or not isinstance(comment, str)):
        raise AccessError("invalid_request", 400)
    if "\0" in comment:
        raise AccessError("invalid_request", 400)
    try:
        comment.encode("utf-8")
    except UnicodeError:
        raise AccessError("invalid_request", 400) from None
    if len(comment) > 2000:
        raise AccessError("comment_too_long", 422)
    return {"expected_review_revision": str(int(revision)), "problem_flag": flag, "comment": comment}


def visible_answer(actor, answer_id, *, lock=False):
    if not 0 < answer_id <= MAX_REVISION:
        raise AccessError("ai_answer_not_found", 404)
    answers = AIAnswer.objects.filter(
        pk=answer_id, dialog__cursor_id__in=allowed_communities(actor),
        aiprocessingrun__status="answer_saved",
    )
    if lock:
        answers = answers.select_for_update(of=("self",))
    answer = answers.first()
    if answer is None:
        raise AccessError("ai_answer_not_found", 404)
    return answer


def timestamp(value):
    return value.astimezone(datetime_timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def review_data(review):
    if review is None:
        return None
    return {
        "answer_id": str(review.answer_id), "problem_flag": review.problem_flag,
        "comment": review.comment, "revision": str(review.revision),
        "flagged_by_id": str(review.flagged_by_id) if review.flagged_by_id else None,
        "flagged_at": timestamp(review.flagged_at), "updated_by_id": str(review.updated_by_id),
        "updated_at": timestamp(review.updated_at),
    }


def review_state(review):
    return {"review": review_data(review), "review_revision": str(review.revision) if review else "0"}


def read_review(session, answer_id):
    actor = support_actor(session)
    visible_answer(actor, answer_id)
    return {**review_state(AIAnswerReview.objects.filter(answer_id=answer_id).first()), "can_edit": True}


@transaction.atomic
def save_review(session, answer_id, operation_id, values):
    actor = support_actor(session, lock=True)
    answer = visible_answer(actor, answer_id, lock=True)
    _, created = reserve_command(actor, operation_id, ACTION, "ai_answer", answer.pk, values)
    review = AIAnswerReview.objects.filter(answer=answer).first()
    if not created:
        # Access and the global UUID fingerprint were checked before reading the old result.
        event = AIAnswerReviewEvent.objects.get(client_operation_id=operation_id)
        applied = event.resulting_revision
    else:
        before = review_state(review)
        current = review.revision if review else 0
        if current != int(values["expected_review_revision"]):
            raise AccessError("review_revision_conflict", 409, **before)
        if current == MAX_REVISION:
            raise AccessError("review_revision_exhausted", 409, **before)
        now = timezone.now()
        if review is None:
            review = AIAnswerReview(answer=answer)
        if values["problem_flag"] and not review.problem_flag:
            review.flagged_by, review.flagged_at = actor, now
        elif not values["problem_flag"]:
            review.flagged_by, review.flagged_at = None, None
        review.problem_flag, review.comment = values["problem_flag"], values["comment"]
        review.updated_by, review.updated_at, review.revision = actor, now, current + 1
        review.save()
        AIAnswerReviewEvent.objects.create(
            review=review, actor=actor, client_operation_id=operation_id,
            expected_revision=current, resulting_revision=review.revision, request_payload=values,
            before_state=before, after_state=review_state(review), created_at=now,
        )
        applied = review.revision
    return {"operation": {"client_operation_id": str(operation_id), "action": ACTION,
                          "replayed": not created, "applied_revision": str(applied)},
            "review": review_data(review)}
