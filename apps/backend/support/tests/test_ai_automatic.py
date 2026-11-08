from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg
from django.db import connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from jobs.models import BackgroundTask as Task
from jobs.services import CHANNEL, claim
from support.ai_delivery import isolate_restored_bot_deliveries, set_outcome
from support.ai_reconciliation import TASK_TYPE, begin_lookup, recover_expired, run_reconciliation_once, schedule_automatic
from support.models import AIAnswer, AIAnswerPart, AIPartAttempt, AIPartDelivery, AIPartReconciliation as Check, AIProcessingRun, Dialog, SupportCommand
from support.tests import test_ai_reconciliation as fixtures


@override_settings(DEBUG=True, VK_BOT_RECONCILIATION_AUTO_ENABLED=True)
class AutomaticReconciliationTests(TransactionTestCase):
    setUp = fixtures.BotReconciliationTests.setUp
    request_check = fixtures.BotReconciliationTests.request_check
    item = fixtures.BotReconciliationTests.item

    def candidate(self, position, *, local=False, answer=None):
        now = timezone.now()
        part = AIAnswerPart.objects.create(answer=answer or self.answer, position=position, text_snapshot="Часть")
        delivery = AIPartDelivery.objects.create(part=part, status="unknown_outcome", uncertainty_revision=uuid4(),
            uncertainty_since=now, created_at=now, updated_at=now)
        if local:
            operation = uuid4()
            task = Task.objects.create(task_type="send_vk_bot_part", idempotency_key=operation, status=Task.Status.UNKNOWN)
            attempt = AIPartAttempt.objects.create(delivery=delivery, task=task, operation_id=operation,
                                                   status="unknown_outcome", created_at=now)
            task.payload = {"delivery_id": delivery.pk, "attempt_id": attempt.pk}
            task.save(update_fields=["payload"])
        return delivery

    def test_verified_source_required_and_unresolved_does_not_repeat(self):
        self.assertEqual(schedule_automatic(17, 41), 0)
        self.assertEqual(schedule_automatic(17, 41, history_available=True), 1)
        check = Check.objects.get()
        self.assertEqual(check.trigger, "automatic")
        self.assertIsNone(check.actor_id)
        self.assertIsNone(check.client_operation_id)
        self.assertIsNone(check.command_id)
        self.assertEqual(check.task.idempotency_key, check.task_key)
        self.assertFalse(SupportCommand.objects.exists())
        self.assertEqual(schedule_automatic(17, 41, history_available=True), 0)
        history = Mock(return_value=[])
        run_reconciliation_once(17, 41, history)
        self.assertEqual(Check.objects.get().outcome, "unresolved")
        self.assertEqual(schedule_automatic(17, 41, history_available=True), 0)
        self.assertEqual(self.request_check().status_code, 202)

    def test_local_proof_is_available_without_history_transport(self):
        AIPartAttempt.objects.filter(pk=self.attempt.pk).update(started_at=None)
        Task.objects.filter(pk=self.outgoing.pk).update(admitted_at=None)
        self.assertTrue(run_reconciliation_once(17, 41))
        self.assertEqual(Check.objects.get().outcome, "not_sent_verified")
        self.assertEqual(Check.objects.count(), 1)

    def test_cursor_advances_past_missing_proofs_to_later_candidate(self):
        for position in range(2, 27):
            self.candidate(position)
        later = self.candidate(27, local=True)
        self.assertEqual(schedule_automatic(17, 41), 0)
        self.cursor.refresh_from_db()
        self.assertIsNotNone(self.cursor.reconciliation_scan_id)
        self.assertEqual(schedule_automatic(17, 41), 1)
        self.assertEqual(Check.objects.get().delivery_id, later.pk)
        self.cursor.refresh_from_db()
        self.assertIsNone(self.cursor.reconciliation_scan_id)

    def test_manual_check_suppresses_automatic_for_same_revision(self):
        self.request_check()
        run_reconciliation_once(17, 41)
        self.assertEqual(Check.objects.get().trigger, "manual")
        self.assertEqual(schedule_automatic(17, 41, history_available=True), 0)

    def test_quarantine_and_disabled_feature_do_not_schedule(self):
        with override_settings(STAFF_QUARANTINE_INCIDENT="automatic-test"):
            self.assertEqual(schedule_automatic(17, 41, history_available=True), 0)
        with override_settings(VK_BOT_RECONCILIATION_AUTO_ENABLED=False):
            self.assertEqual(schedule_automatic(17, 41, history_available=True), 0)
        self.assertFalse(Check.objects.exists())

    def test_disabling_automation_finishes_queued_check_without_reading(self):
        schedule_automatic(17, 41, history_available=True)
        history = Mock()
        with override_settings(VK_BOT_RECONCILIATION_AUTO_ENABLED=False):
            run_reconciliation_once(17, 41, history)
        history.assert_not_called()
        check = Check.objects.get()
        self.assertEqual((check.outcome, check.reason), ("unresolved", "automation_disabled"))
        self.assertEqual(self.request_check().status_code, 202)

    def test_parallel_schedulers_create_one_check(self):
        barrier = Barrier(2)

        def schedule():
            try:
                barrier.wait(timeout=5)
                return schedule_automatic(17, 41, history_available=True)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(schedule) for _ in range(2)]
            self.assertCountEqual([task.result(timeout=15) for task in tasks], [0, 1])
        self.assertEqual(Check.objects.count(), 1)

    def test_expired_lease_reuses_automatic_task_and_then_finishes(self):
        schedule_automatic(17, 41, history_available=True)
        check = Check.objects.get()
        job = claim([TASK_TYPE])
        begin_lookup(job, 17, 41)
        Task.objects.filter(pk=job.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        self.assertTrue(recover_expired())
        run_reconciliation_once(17, 41, Mock(return_value=[]))
        check.refresh_from_db()
        self.assertEqual(check.outcome, "unresolved")
        self.assertEqual(Check.objects.count(), 1)
        self.assertEqual(check.task_id, job.pk)

    def test_restore_cancels_old_automatic_and_new_revision_can_be_checked(self):
        schedule_automatic(17, 41, history_available=True)
        old = Check.objects.get()
        with override_settings(STAFF_QUARANTINE_INCIDENT="automatic-test"):
            isolate_restored_bot_deliveries(uuid4())
        old.refresh_from_db()
        self.assertEqual((old.status, old.reason), ("finished", "recovered_from_backup"))
        self.assertEqual(schedule_automatic(17, 41, history_available=True), 1)
        current = Check.objects.latest("pk")
        self.assertNotEqual(current.target_revision, old.target_revision)
        self.assertNotEqual(current.task_key, old.task_key)

    def test_manual_tasks_have_priority_over_automatic_tasks(self):
        schedule_automatic(17, 41, history_available=True)
        other = self.candidate(2, local=True)
        self.url = f"/support/ai-parts/{other.part_id}/reconcile/"
        self.request_check()
        first = claim([TASK_TYPE])
        self.assertEqual(Check.objects.get(task=first).trigger, "manual")

    def test_failed_enqueue_rolls_back_cursor_and_check(self):
        with patch("support.ai_reconciliation.enqueue", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                schedule_automatic(17, 41, history_available=True)
        self.assertFalse(Check.objects.exists())
        self.cursor.refresh_from_db()
        self.assertIsNone(self.cursor.reconciliation_scan_id)

    def test_worker_leaves_another_pilot_dialog_task_queued(self):
        now = timezone.now()
        dialog = Dialog.objects.create(cursor=self.cursor, vk_peer_id=99)
        answer = AIAnswer.objects.create(dialog=dialog, generation_operation_id=uuid4(), mode_version_snapshot=1,
            model_snapshot={}, retrieval_snapshot={}, timings_snapshot={}, content_sha256="b" * 64, created_at=now)
        generation = Task.objects.create(task_type="test.generation", idempotency_key=uuid4())
        AIProcessingRun.objects.create(answer=answer, dialog=dialog, operation_id=answer.generation_operation_id,
            task=generation, mode_version=1, automation_epoch=1, status="answer_saved", created_at=now,
            finished_at=now, processing_deadline_at=now)
        other = self.candidate(1, local=True, answer=answer)
        self.url = f"/support/ai-parts/{other.part_id}/reconcile/"
        self.request_check()
        other_check = Check.objects.get()
        history = Mock(return_value=[])
        run_reconciliation_once(17, 41, history)
        history.assert_called_once_with(41)
        other_check.refresh_from_db()
        self.assertEqual((other_check.status, other_check.task.status), ("queued", "pending"))

    def test_unknown_notification_is_delivered_after_commit_and_rolled_back_on_failure(self):
        params = connections["default"].get_connection_params()
        with psycopg.connect(**params, autocommit=True) as listener:
            listener.execute(f"LISTEN {CHANNEL}")
            AIPartDelivery.objects.filter(pk=self.delivery.pk).update(status="pending")
            self.delivery.refresh_from_db()
            with transaction.atomic():
                set_outcome(self.answer, self.delivery, self.attempt, "unknown_outcome", timezone.now())
                self.assertIsNone(next(listener.notifies(timeout=0.05, stop_after=1), None))
            self.assertIsNotNone(next(listener.notifies(timeout=2, stop_after=1), None))
            AIPartDelivery.objects.filter(pk=self.delivery.pk).update(status="pending")
            self.delivery.refresh_from_db()
            with self.assertRaises(RuntimeError), transaction.atomic():
                set_outcome(self.answer, self.delivery, self.attempt, "unknown_outcome", timezone.now())
                raise RuntimeError("rollback test")
            self.assertIsNone(next(listener.notifies(timeout=0.05, stop_after=1), None))
