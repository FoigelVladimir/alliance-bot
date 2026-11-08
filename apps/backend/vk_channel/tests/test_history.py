from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import Mock
from uuid import uuid4
from types import SimpleNamespace

from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone

from support import reconciliation
from support.delivery import run_sender_once
from support.tests import test_operations as fixtures
from vk_channel.client import DeliveryUnknown
from vk_channel.backfill import inspect_history
from vk_channel.history import HistoryQuotaExceeded, read_history, use_history_quota
from vk_channel.models import LongPollCursor


class SharedHistoryTests(TransactionTestCase):
    def setUp(self):
        self.cursor = LongPollCursor.objects.create(community_id=17, position="100")

    def test_automatic_half_preserves_six_manual_reads(self):
        for _ in range(6):
            use_history_quota(17, automatic=True)
        with self.assertRaises(HistoryQuotaExceeded):
            use_history_quota(17, automatic=True)
        for _ in range(6):
            use_history_quota(17)
        with self.assertRaises(HistoryQuotaExceeded):
            use_history_quota(17)
        self.cursor.refresh_from_db()
        self.assertEqual((self.cursor.history_quota_used, self.cursor.history_auto_quota_used), (12, 6))

    def test_new_window_resets_both_counters(self):
        LongPollCursor.objects.filter(pk=self.cursor.pk).update(history_quota_started_at=timezone.now() - timedelta(minutes=2),
            history_quota_used=12, history_auto_quota_used=6)
        use_history_quota(17, automatic=True)
        self.cursor.refresh_from_db()
        self.assertEqual((self.cursor.history_quota_used, self.cursor.history_auto_quota_used), (1, 1))

    def test_background_backfill_uses_automatic_share(self):
        now = timezone.now()
        LongPollCursor.objects.filter(pk=self.cursor.pk).update(history_quota_started_at=now,
            history_quota_used=6, history_auto_quota_used=6)
        gap = SimpleNamespace(cursor=self.cursor, cursor_id=self.cursor.pk, previous_batch_at=now, recorded_at=now)
        discovery = Mock()
        _, updates = inspect_history(gap, discovery, Mock())
        discovery.assert_not_called()
        self.assertEqual(updates, [])
        self.assertEqual(read_history(17, lambda: "manual"), "manual")

    def test_parallel_read_is_blocked_without_consuming_quota(self):
        started, release = Event(), Event()

        def fetch():
            started.set()
            if not release.wait(timeout=10):
                raise RuntimeError("Test did not release the reader")
            return "first"

        def read():
            try:
                return read_history(17, fetch, automatic=True)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(read)
            try:
                self.assertTrue(started.wait(timeout=5))
                second = Mock()
                with self.assertRaises(HistoryQuotaExceeded):
                    read_history(17, second)
                second.assert_not_called()
                self.cursor.refresh_from_db()
                self.assertEqual(self.cursor.history_quota_used, 1)
                LongPollCursor.objects.create(community_id=99, position="100")
                self.assertEqual(read_history(99, lambda: "other community"), "other community")
            finally:
                release.set()
            self.assertEqual(future.result(timeout=10), "first")
        self.assertEqual(read_history(17, lambda: "manual"), "manual")

    def test_transport_exception_and_quota_rejection_release_lock(self):
        with self.assertRaises(OSError):
            read_history(17, Mock(side_effect=OSError("unavailable")))
        LongPollCursor.objects.filter(pk=self.cursor.pk).update(history_quota_used=6, history_auto_quota_used=6)
        with self.assertRaises(HistoryQuotaExceeded):
            read_history(17, Mock(), automatic=True)

        def other_connection():
            try:
                return read_history(17, lambda: 42)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(other_connection).result(timeout=10), 42)


class ManagerHistoryQuotaTests(TransactionTestCase):
    setUp = fixtures.DeliveryTests.setUp
    send = fixtures.DeliveryTests.send

    def test_manager_reconciliation_defers_same_task_when_quota_exhausted(self):
        attempt = self.send()
        run_sender_once(17, Mock(side_effect=DeliveryUnknown))
        check, _ = reconciliation.request_reconciliation(self.client.session, attempt.response_id, uuid4())
        LongPollCursor.objects.filter(pk=self.cursor.pk).update(history_quota_started_at=timezone.now(), history_quota_used=12)
        history = Mock()
        reconciliation.run_reconciliation_once(17, history)
        history.assert_not_called()
        check.refresh_from_db()
        self.assertEqual((check.status, check.task.status), ("queued", "retry_scheduled"))
        self.assertIsNone(check.finished_at)
