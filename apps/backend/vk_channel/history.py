from datetime import timedelta
from hashlib import sha256

from django.db import connection, transaction

from storage.services import database_time

from .models import LongPollCursor

MAX_CALLS_PER_MINUTE = 12
MAX_AUTOMATIC_CALLS_PER_MINUTE = 6


class HistoryQuotaExceeded(Exception):
    def __init__(self, retry_at=None):
        self.retry_at = retry_at
        super().__init__("History read quota unavailable")


@transaction.atomic
def use_history_quota(community_id, *, automatic=False):
    cursor = LongPollCursor.objects.select_for_update().filter(community_id=community_id).first()
    now = database_time()
    if cursor is None:
        raise HistoryQuotaExceeded(now + timedelta(seconds=1))
    if not cursor.history_quota_started_at or now >= cursor.history_quota_started_at + timedelta(minutes=1):
        cursor.history_quota_started_at = now
        cursor.history_quota_used = 0
        cursor.history_auto_quota_used = 0
    if (cursor.history_quota_used >= MAX_CALLS_PER_MINUTE
            or automatic and cursor.history_auto_quota_used >= MAX_AUTOMATIC_CALLS_PER_MINUTE):
        raise HistoryQuotaExceeded(cursor.history_quota_started_at + timedelta(minutes=1))
    cursor.history_quota_used += 1
    if automatic:
        cursor.history_auto_quota_used += 1
    cursor.save(update_fields=["history_quota_started_at", "history_quota_used", "history_auto_quota_used"])


def read_history(community_id, fetch, *args, automatic=False, **kwargs):
    """Hold a session advisory lock only while the external history request runs."""
    lock_key = int.from_bytes(sha256(f"alliance:vk-history:{community_id}".encode()).digest()[:8], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_key])
        acquired = cursor.fetchone()[0]
    if not acquired:
        raise HistoryQuotaExceeded(database_time() + timedelta(seconds=10))
    try:
        use_history_quota(community_id, automatic=automatic)
        return fetch(*args, **kwargs)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_key])
