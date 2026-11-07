from datetime import timedelta
from time import monotonic

from django.db import transaction

from accounts.policies import has_work_access, quarantine_incident
from accounts.services import lock_profiles
from jobs.models import BackgroundTask as Task
from jobs.services import ClockTimestamp, LeaseLost, claim, finish
from support.models import Dialog, SupportMembership

from .client import DeliveryUnknown
from .models import InboundEvent, LongPollCursor, LongPollGap
from .services import GAP_TASK, database_now, open_gap, save_historical_batch, valid_id, valid_text

MAX_DIALOGS = 8
PAGE_SIZE = 50
MAX_CALLS_PER_MINUTE = 12
READ_SECONDS = 35


class HistoryQuotaExceeded(Exception):
    pass


@transaction.atomic
def use_history_quota(community_id):
    cursor = LongPollCursor.objects.select_for_update().filter(community_id=community_id).first()
    if cursor is None:
        raise HistoryQuotaExceeded
    now = database_now()
    if not cursor.history_quota_started_at or now >= cursor.history_quota_started_at + timedelta(minutes=1):
        cursor.history_quota_started_at = now
        cursor.history_quota_used = 0
    if cursor.history_quota_used >= MAX_CALLS_PER_MINUTE:
        raise HistoryQuotaExceeded
    cursor.history_quota_used += 1
    cursor.save(update_fields=["history_quota_started_at", "history_quota_used"])


def inspect_history(gap, conversations, history):
    report = {"reasons": ["non_message_events_uncovered"], "dialogs": {}, "found": 0, "known": 0}
    reasons = report["reasons"]
    updates = []
    if not gap.previous_batch_at or not timedelta(0) <= gap.recorded_at - gap.previous_batch_at <= timedelta(days=1):
        reasons.append("interval_unverified")
        return report, updates

    start, end = gap.previous_batch_at.timestamp(), gap.recorded_at.timestamp()
    deadline = monotonic() + READ_SECONDS
    community = gap.cursor.community_id
    known = Dialog.objects.filter(cursor_id=gap.cursor_id).order_by("pk").values_list("vk_peer_id", flat=True)
    peers = set(known[:MAX_DIALOGS + 1])

    def read(fetch, *args):
        use_history_quota(community)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        response = fetch(*args, timeout=min(10, remaining))
        if monotonic() >= deadline:
            raise TimeoutError
        return response

    try:
        page = read(conversations)
        items = page["items"]
        if len(items) >= PAGE_SIZE or page.get("count") != len(items):
            reasons.append("conversation_page_incomplete")
        for item in items[:PAGE_SIZE]:
            try:
                peer = item["conversation"]["peer"]["id"]
            except (KeyError, TypeError):
                reasons.append("conversation_identity_missing")
                continue
            if valid_id(peer):
                peers.add(peer)
            else:
                reasons.append("unsupported_dialog")
    except HistoryQuotaExceeded:
        reasons.append("history_quota")
        return report, updates
    except TimeoutError:
        reasons.append("time_limit")
        return report, updates
    except (DeliveryUnknown, OSError):
        reasons.append("conversation_discovery_unavailable")

    if len(peers) > MAX_DIALOGS:
        reasons.append("dialog_limit")
    for peer in sorted(peers)[:MAX_DIALOGS]:
        try:
            items = read(history, peer)
        except HistoryQuotaExceeded:
            reasons.append("history_quota")
            break
        except TimeoutError:
            reasons.append("time_limit")
            break
        except (DeliveryUnknown, OSError):
            report["dialogs"][str(peer)] = {"reason": "history_unavailable"}
            continue
        timestamps = []
        count = 0
        malformed = False
        for item in items[:PAGE_SIZE]:
            if not isinstance(item, dict) or type(item.get("date")) is not int or item.get("peer_id") != peer:
                malformed = True
                continue
            stamp = item["date"]
            timestamps.append(stamp)
            if int(start) <= stamp <= end and type(item.get("out")) is int and item["out"] == 0:
                if (
                    not valid_id(item.get("conversation_message_id")) or not valid_id(item.get("from_id"))
                    or not valid_text(item.get("text", "")) or not isinstance(item.get("attachments", []), list)
                ):
                    malformed = True
                    continue
                count += 1
                updates.append({"type": "message_new", "group_id": community, "object": {"message": item}})
        report["dialogs"][str(peer)] = {
            "oldest": min(timestamps, default=None), "newest": max(timestamps, default=None), "found": count,
            "reason": "malformed_history" if malformed else (
                "lower_boundary_unverified" if not timestamps or min(timestamps) >= start else "coverage_unverified"
            ),
        }
    report["reasons"] = sorted(set(reasons))
    return report, updates


@transaction.atomic
def recover_expired_gap():
    task = Task.objects.select_for_update(skip_locked=True).filter(
        task_type=GAP_TASK, status=Task.Status.PROCESSING, locked_until__lte=ClockTimestamp(),
    ).order_by("locked_until").first()
    if task is None:
        return False
    exhausted = task.attempt_count >= task.max_attempts
    task.status = Task.Status.DEAD if exhausted else Task.Status.RETRY
    task.lease_token = task.locked_until = None
    task.updated_at = database_now()
    if exhausted:
        task.result = {"reason": "lookup_budget_exhausted"}
        LongPollGap.objects.filter(pk=task.payload["gap_id"], status="queued").update(
            status="needs_review", coverage={"reasons": ["lookup_budget_exhausted"]},
        )
    task.save(update_fields=["status", "lease_token", "locked_until", "updated_at", "result"])
    return True


def run_gap_once(community, conversations, history):
    while recover_expired_gap():
        pass
    task = claim([GAP_TASK])
    if task is None:
        return False
    gap = LongPollGap.objects.select_related("cursor").get(pk=task.payload["gap_id"])
    if gap.cursor.community_id == community and not quarantine_incident():
        report, updates = inspect_history(gap, conversations, history)
    else:
        report, updates = {"reasons": ["community_unavailable"]}, []
    try:
        with transaction.atomic():
            # Serialize deduplication and the coverage report with the live reader.
            LongPollCursor.objects.select_for_update().get(pk=gap.cursor_id)
            seen = set()
            unique = []
            for update in updates:
                item = update["object"]["message"]
                key = (item["peer_id"], item["conversation_message_id"])
                if key not in seen:
                    seen.add(key)
                    unique.append(update)
            known = sum(InboundEvent.objects.filter(
                cursor_id=gap.cursor_id, peer_id=peer, conversation_message_id=cmid, status="accepted",
            ).exists() for peer, cmid in seen)
            save_historical_batch(gap.pk, unique)
            finish(task, {"gap_id": gap.pk})
            report.update(found=len(unique), known=known)
            LongPollGap.objects.filter(pk=gap.pk, status="queued").update(status="needs_review", coverage=report)
    except LeaseLost:
        pass
    return True


@transaction.atomic
def resume_gap(gap_id, profile_id, period, position, notes):
    if not notes.strip() or len(notes) > 4000:
        raise ValueError("Укажите основание и оставшиеся пропуски.")
    profile = lock_profiles(profile_id).get(profile_id)
    gap = LongPollGap.objects.filter(pk=gap_id).first()
    if not gap or not has_work_access(profile) or profile.disabled or quarantine_incident() or not (
        profile.bindings.filter(active=True).exists() and SupportMembership.objects.filter(
            profile=profile, cursor_id=gap.cursor_id, coordinates_support=True,
            access_generation=profile.access_generation,
        ).exists()
    ):
        raise ValueError("Нет права подтвердить этот разрыв.")
    cursor = LongPollCursor.objects.select_for_update().get(pk=gap.cursor_id)
    gap.refresh_from_db()
    if gap.resumed_at and gap.resumed_by_id == profile_id and gap.resumed_position == position and (
        gap.period + 1 == period and gap.resume_notes == notes
    ):
        return
    if (
        not cursor.automation_paused or period != cursor.period or period != gap.period + 1
        or position != cursor.position or gap.status != "needs_review" or not cursor.last_batch_at
        or cursor.last_batch_at <= gap.recorded_at
    ):
        raise ValueError("Период или подтверждённая позиция изменились либо история ещё проверяется.")
    gap.status, gap.resumed_at = "resumed", database_now()
    gap.resumed_by, gap.resumed_position, gap.resume_notes = profile, position, notes
    gap.save(update_fields=["status", "resumed_at", "resumed_by", "resumed_position", "resume_notes"])
    cursor.automation_paused = False
    cursor.save(update_fields=["automation_paused"])


@transaction.atomic
def mark_restored_gaps():
    incident = quarantine_incident()
    if not incident:
        raise ValueError("Запустите команду в карантине восстановления.")
    for cursor in LongPollCursor.objects.select_for_update().order_by("pk"):
        if not LongPollGap.objects.filter(cursor=cursor, restore_incident=incident).exists():
            open_gap(cursor, cursor.position, "database_restore", incident=incident)
