from datetime import timedelta
from uuid import uuid4

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from jobs.models import BackgroundTask as Task
from support.ai_delivery import TASK_TYPE, isolate_restored_bot_deliveries, recover_expired, set_outcome
from support.models import (
    AIAnswer, AIAnswerPart, AIPartAttempt, AIPartDelivery, BotDeliveryRecoveryRun, Dialog,
)
from vk_channel.models import LongPollCursor


class BotUncertaintyTests(TestCase):
    def setUp(self):
        now = timezone.now()
        cursor = LongPollCursor.objects.create(community_id=17, position="100")
        dialog = Dialog.objects.create(cursor=cursor, vk_peer_id=41)
        self.answer = AIAnswer.objects.create(
            dialog=dialog, generation_operation_id=uuid4(), mode_version_snapshot=1,
            model_snapshot={}, retrieval_snapshot={}, timings_snapshot={}, content_sha256="a" * 64, created_at=now,
        )
        self.delivery = self.add_part(1)

    def add_part(self, position):
        part = AIAnswerPart.objects.create(answer=self.answer, position=position, text_snapshot="Сохранённая часть")
        return AIPartDelivery.objects.create(part=part, created_at=timezone.now(), updated_at=timezone.now())

    def reserve(self):
        operation_id = uuid4()
        task = Task.objects.create(task_type=TASK_TYPE, idempotency_key=operation_id)
        attempt = AIPartAttempt.objects.create(delivery=self.delivery, task=task, operation_id=operation_id,
                                               created_at=timezone.now())
        task.payload = {"delivery_id": self.delivery.pk, "attempt_id": attempt.pk}
        task.save(update_fields=["payload"])
        self.delivery.status, self.delivery.vk_random_id = "pending", 123
        self.delivery.save(update_fields=["status", "vk_random_id"])
        return attempt, task

    def test_unknown_records_revision_once_and_resolution_preserves_it(self):
        attempt, _ = self.reserve()
        following = self.add_part(2)
        now = timezone.now()
        set_outcome(self.answer, self.delivery, attempt, "unknown_outcome", now)
        revision = self.delivery.uncertainty_revision
        self.assertIsNotNone(revision)
        self.assertEqual(self.delivery.uncertainty_since, now)
        set_outcome(self.answer, self.delivery, attempt, "unknown_outcome", now + timedelta(seconds=1))
        self.assertEqual(self.delivery.uncertainty_revision, revision)
        self.assertEqual(self.delivery.uncertainty_since, now)
        set_outcome(self.answer, self.delivery, attempt, "failed", now + timedelta(seconds=2))
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.uncertainty_revision, revision)
        following.refresh_from_db()
        self.assertEqual(following.status, "cancelled")

    def test_worker_expiry_records_unknown_in_task_attempt_and_delivery(self):
        attempt, task = self.reserve()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.PROCESSING, lease_token=uuid4(),
            locked_until=timezone.now() - timedelta(seconds=1), admitted_at=timezone.now())
        attempt.started_at = timezone.now()
        attempt.save(update_fields=["started_at"])
        self.assertTrue(recover_expired())
        self.delivery.refresh_from_db()
        attempt.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual((self.delivery.status, attempt.status, task.status), ("unknown_outcome",) * 3)
        self.assertIsNotNone(self.delivery.uncertainty_revision)
        self.assertIsNone(self.delivery.recovery_run_id)
        self.assertIsNone(task.lease_token)

    @override_settings(STAFF_QUARANTINE_INCIDENT="uncertainty-test")
    def test_same_run_preserves_revision_new_run_replaces_it(self):
        attempt, _ = self.reserve()
        keyless = self.add_part(2)
        run_id = uuid4()
        call_command("isolate_restored_bot_deliveries", recovery_run_id=run_id)
        self.delivery.refresh_from_db()
        first_revision, first_since = self.delivery.uncertainty_revision, self.delivery.uncertainty_since
        self.assertFalse(isolate_restored_bot_deliveries(run_id))
        self.delivery.refresh_from_db()
        self.assertEqual((self.delivery.uncertainty_revision, self.delivery.uncertainty_since), (first_revision, first_since))
        next_run = uuid4()
        self.assertTrue(isolate_restored_bot_deliveries(next_run))
        self.delivery.refresh_from_db()
        self.assertNotEqual(self.delivery.uncertainty_revision, first_revision)
        self.assertEqual(self.delivery.recovery_run_id, next_run)
        keyless.refresh_from_db()
        self.assertIsNone(keyless.vk_random_id)
        self.assertFalse(AIPartAttempt.objects.filter(delivery=keyless).exists())
        attempt.refresh_from_db()
        self.assertIsNone(attempt.started_at)

    @override_settings(STAFF_QUARANTINE_INCIDENT="uncertainty-test")
    def test_legacy_invocation_is_stable_and_does_not_reprocess_new_parts(self):
        self.assertTrue(isolate_restored_bot_deliveries())
        revision = AIPartDelivery.objects.get().uncertainty_revision
        fresh = self.add_part(2)
        self.assertFalse(isolate_restored_bot_deliveries())
        self.delivery.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(self.delivery.uncertainty_revision, revision)
        self.assertEqual(fresh.status, "waiting")
        self.assertEqual(BotDeliveryRecoveryRun.objects.count(), 1)

    @override_settings(STAFF_QUARANTINE_INCIDENT="uncertainty-test")
    def test_failed_pass_rolls_back_deliveries_and_keeps_incomplete_run(self):
        invalid = self.add_part(2)
        invalid.status = "pending"
        invalid.save(update_fields=["status"])
        run_id = uuid4()
        with self.assertRaises(ValueError):
            isolate_restored_bot_deliveries(run_id)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "waiting")
        self.assertIsNone(self.delivery.uncertainty_revision)
        self.assertIsNone(BotDeliveryRecoveryRun.objects.get(pk=run_id).completed_at)
        invalid.status = "waiting"
        invalid.save(update_fields=["status"])
        self.assertTrue(isolate_restored_bot_deliveries(run_id))
        with override_settings(STAFF_QUARANTINE_INCIDENT="another-incident"):
            with self.assertRaises(ValueError):
                isolate_restored_bot_deliveries(run_id)

    def test_unknown_without_revision_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AIPartDelivery.objects.filter(pk=self.delivery.pk).update(status="unknown_outcome")
        with self.assertRaises(ValueError):
            isolate_restored_bot_deliveries(uuid4())

    @override_settings(STAFF_QUARANTINE_INCIDENT="uncertainty-test")
    def test_confirmed_outcomes_in_inconsistent_groups_are_preserved(self):
        attempt, task = self.reserve()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.DONE)
        AIPartAttempt.objects.filter(pk=attempt.pk).update(status="succeeded", finished_at=timezone.now())
        run_id = uuid4()
        with self.assertRaises(ValueError):
            isolate_restored_bot_deliveries(run_id)
        attempt.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual((attempt.status, task.status), ("succeeded", Task.Status.DONE))
        self.assertIsNone(BotDeliveryRecoveryRun.objects.get(pk=run_id).completed_at)
