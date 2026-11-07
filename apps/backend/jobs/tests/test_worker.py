from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from time import monotonic, sleep
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from jobs import services
from jobs.models import BackgroundTask as Task
from jobs.worker import run_one


class QueueTests(TransactionTestCase):
    def enqueue(self, **options):
        with transaction.atomic():
            return services.enqueue("example", uuid4(), {"value": 7}, **options)

    def test_enqueue_rolls_back_with_domain_write(self):
        with self.assertRaisesRegex(RuntimeError, "forced"):
            with transaction.atomic():
                get_user_model().objects.create(username="rolled-back")
                services.enqueue("example", uuid4(), {})
                raise RuntimeError("forced")
        self.assertFalse(Task.objects.exists())
        self.assertFalse(get_user_model().objects.filter(username="rolled-back").exists())

    def test_deduplication_preserves_the_original_task(self):
        key = uuid4()
        with transaction.atomic():
            first = services.enqueue("example", key, {"value": 7})
            again = services.enqueue("example", key, {"value": 7})
            with self.assertRaises(ValueError):
                services.enqueue("example", key, {"value": 8})
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Task.objects.count(), 1)

    def test_worker_picks_up_persisted_task_with_a_fresh_connection(self):
        task = self.enqueue()
        connections.close_all()

        def prepare(job):
            def save():
                get_user_model().objects.create(username="result")
                return job.payload
            return save

        self.assertTrue(run_one({"example": prepare}))
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.DONE)
        self.assertEqual(task.result, {"value": 7})
        self.assertTrue(get_user_model().objects.filter(username="result").exists())
        self.assertFalse(run_one({"example": prepare}))

    def test_expired_worker_cannot_finish_or_renew(self):
        task = self.enqueue()
        lease = services.claim(["example"])
        Task.objects.filter(pk=task.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(services.LeaseLost):
            services.renew(lease)
        with self.assertRaises(services.LeaseLost):
            with transaction.atomic():
                get_user_model().objects.create(username="stale")
                services.finish(lease)
        self.assertFalse(get_user_model().objects.filter(username="stale").exists())
        self.assertTrue(services.recover_one(["example"]))
        Task.objects.filter(pk=task.pk).update(scheduled_at=timezone.now())
        replacement = services.claim(["example"])
        self.assertNotEqual(replacement.lease_token, lease.lease_token)
        with self.assertRaises(services.LeaseLost):
            services.finish(lease)
        services.finish(replacement)

    def test_admitted_task_is_never_automatically_reclaimed(self):
        task = self.enqueue()
        lease = services.claim(["example"])
        services.admit_external_call(lease)
        with self.assertRaises(services.LeaseLost):
            services.admit_external_call(lease)
        services.retry(lease)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.UNKNOWN)
        self.assertIsNone(services.claim(["example"]))

    def test_rolled_back_completion_keeps_lease_and_domain_state(self):
        task = self.enqueue()
        lease = services.claim(["example"])
        with self.assertRaisesRegex(RuntimeError, "forced"):
            with transaction.atomic():
                services.finish(lease)
                get_user_model().objects.create(username="partial")
                raise RuntimeError("forced")
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.PROCESSING)
        self.assertEqual(task.lease_token, lease.lease_token)
        self.assertFalse(get_user_model().objects.filter(username="partial").exists())

    def test_exhausted_lease_goes_to_dead_letter(self):
        task = self.enqueue(max_attempts=1)
        services.claim(["example"])
        Task.objects.filter(pk=task.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        self.assertTrue(services.recover_one(["example"]))
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.DEAD)
        self.assertEqual(task.result, {"reason": "attempt_budget_exhausted"})
        self.assertIsNone(services.claim(["example"]))

    def test_lease_expiring_while_waiting_for_row_lock_blocks_completion(self):
        task = self.enqueue()
        lease = services.claim(["example"])
        backend_ids = Queue()

        def finish():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    backend_ids.put(cursor.fetchone()[0])
                with self.assertRaises(services.LeaseLost):
                    services.finish(lease)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                Task.objects.select_for_update().get(pk=task.pk)
                outcome = executor.submit(finish)
                backend_id = backend_ids.get(timeout=5)
                deadline = monotonic() + 5
                with connection.cursor() as cursor:
                    while monotonic() < deadline:
                        cursor.execute(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted)",
                            [backend_id],
                        )
                        if cursor.fetchone()[0]:
                            break
                        sleep(0.01)
                    else:
                        self.fail("The worker did not wait for the task lock")
                Task.objects.filter(pk=task.pk).update(locked_until=services.ClockTimestamp())
            outcome.result(timeout=5)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.PROCESSING)
