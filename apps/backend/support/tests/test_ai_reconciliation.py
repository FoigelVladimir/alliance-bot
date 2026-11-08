from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from io import StringIO
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.management import call_command
from django.db import IntegrityError, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from jobs.models import BackgroundTask as Task
from jobs.services import LeaseLost, claim
from support.ai_delivery import isolate_restored_bot_deliveries
from support.ai_reconciliation import (
    TASK_TYPE, begin_lookup, complete_lookup, recover_expired, run_reconciliation_once,
)
from support.models import (
    AIAnswer, AIAnswerPart, AIPartAttempt, AIPartDelivery, AIPartReconciliation as Check,
    AIPartReconciliationEvidence as Evidence, AIProcessingRun, Dialog, Message, SupportCommand, Ticket,
)
from support.services import grant_support_membership
from support.tests.test_http import signed_in
from vk_channel.models import LongPollCursor


@override_settings(DEBUG=True)
class BotReconciliationTests(TransactionTestCase):
    def setUp(self):
        self.client, self.profile = signed_in("bot-reconcile-one")
        self.other_client, self.other = signed_in("bot-reconcile-two")
        self.cursor = LongPollCursor.objects.create(community_id=17, position="100")
        for profile in (self.profile, self.other):
            grant_support_membership(profile.pk, self.cursor.pk)
        self.dialog = Dialog.objects.create(cursor=self.cursor, vk_peer_id=41)
        now = timezone.now() - timedelta(seconds=2)
        self.answer = AIAnswer.objects.create(dialog=self.dialog, generation_operation_id=uuid4(),
            mode_version_snapshot=1, model_snapshot={}, retrieval_snapshot={}, timings_snapshot={},
            content_sha256="a" * 64, created_at=now)
        self.part = AIAnswerPart.objects.create(answer=self.answer, position=1, text_snapshot="Снимок ответа")
        self.delivery = AIPartDelivery.objects.create(part=self.part, status="unknown_outcome", vk_random_id=123,
            uncertainty_revision=uuid4(), uncertainty_since=now, created_at=now, updated_at=now)
        op = uuid4()
        self.outgoing = Task.objects.create(task_type="send_vk_bot_part", idempotency_key=op,
                                            status=Task.Status.UNKNOWN, admitted_at=now)
        self.attempt = AIPartAttempt.objects.create(delivery=self.delivery, task=self.outgoing, operation_id=op,
            status="unknown_outcome", created_at=now, started_at=now)
        self.outgoing.payload = {"delivery_id": self.delivery.pk, "attempt_id": self.attempt.pk}
        self.outgoing.save(update_fields=["payload"])
        generation = Task.objects.create(task_type="test.generation", idempotency_key=uuid4())
        self.run = AIProcessingRun.objects.create(operation_id=self.answer.generation_operation_id,
            answer=self.answer, dialog=self.dialog, task=generation, mode_version=1, automation_epoch=1,
            status="answer_saved", created_at=now, finished_at=now, processing_deadline_at=now)
        self.url = f"/support/ai-parts/{self.part.pk}/reconcile/"

    def request_check(self, operation=None, client=None):
        return (client or self.client).post(self.url, {"client_operation_id": str(operation or uuid4())},
                                           content_type="application/json")

    def item(self, **changes):
        return {"id": 192, "peer_id": 41, "from_id": -17, "out": 1, "random_id": 123,
                "date": int(timezone.now().timestamp()), "text": self.part.text_snapshot, **changes}

    def test_success_and_replay_create_one_fact_without_new_send(self):
        operation = uuid4()
        self.assertEqual(self.request_check(operation).status_code, 202)
        self.assertEqual(self.request_check(operation).status_code, 202)
        self.assertEqual(Check.objects.count(), 1)
        history = Mock(return_value=[self.item()])
        self.assertTrue(run_reconciliation_once(17, 41, history))
        history.assert_called_once_with(41)
        self.delivery.refresh_from_db()
        self.attempt.refresh_from_db()
        self.outgoing.refresh_from_db()
        self.assertEqual((self.delivery.status, self.attempt.status, self.outgoing.status), ("sent", "succeeded", "done"))
        self.assertEqual(Message.objects.get().text, self.part.text_snapshot)
        self.assertEqual(Evidence.objects.get().facts["message_id"], 192)
        replay = self.request_check(operation)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["operation"]["replayed"])
        self.assertEqual(AIPartAttempt.objects.count(), 1)
        self.assertFalse(run_reconciliation_once(17, 41, history))
        self.assertEqual(self.request_check().json()["error"]["code"], "reconciliation_not_required")

    def test_empty_or_ambiguous_history_remains_unknown_and_requires_new_command(self):
        self.request_check()
        self.assertEqual(self.request_check().json()["error"]["code"], "reconciliation_in_progress")
        run_reconciliation_once(17, 41, Mock(return_value=[]))
        self.assertEqual(Check.objects.get().outcome, "unresolved")
        self.request_check()
        run_reconciliation_once(17, 41, Mock(return_value=[self.item(), self.item(id=193)]))
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "unknown_outcome")
        self.assertFalse(Evidence.objects.exists())
        self.assertFalse(Message.objects.exists())

    def test_local_non_admission_verifies_failure_without_history(self):
        AIPartAttempt.objects.filter(pk=self.attempt.pk).update(started_at=None)
        Task.objects.filter(pk=self.outgoing.pk).update(admitted_at=None)
        self.request_check()
        history = Mock()
        run_reconciliation_once(17, 41, history)
        history.assert_not_called()
        self.assertEqual(Check.objects.get().outcome, "not_sent_verified")
        self.assertEqual(Check.objects.get().reason, "")
        self.delivery.refresh_from_db()
        self.outgoing.refresh_from_db()
        self.assertEqual((self.delivery.status, self.outgoing.status), ("failed", "failed"))
        self.assertEqual(AIPartAttempt.objects.count(), 1)

    def test_restored_missing_attempt_and_key_stays_unknown(self):
        part = AIAnswerPart.objects.create(answer=self.answer, position=2, text_snapshot="Вторая часть")
        now = timezone.now()
        delivery = AIPartDelivery.objects.create(part=part, created_at=now, updated_at=now)
        with override_settings(STAFF_QUARANTINE_INCIDENT="reconcile-restore"):
            isolate_restored_bot_deliveries(uuid4())
        self.url = f"/support/ai-parts/{part.pk}/reconcile/"
        self.request_check()
        history = Mock()
        run_reconciliation_once(17, 41, history)
        history.assert_not_called()
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "unknown_outcome")
        self.assertEqual(Check.objects.get().outcome, "unresolved")
        self.assertFalse(AIPartAttempt.objects.filter(delivery=delivery).exists())

    def test_owner_coordinator_and_current_access_apply_to_replay(self):
        ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)
        self.run.handoff_ticket = ticket
        self.run.save(update_fields=["handoff_ticket"])
        self.assertEqual(self.request_check(client=self.other_client).status_code, 403)
        operation = uuid4()
        self.request_check(operation)
        state = self.client.get(f"/support/tickets/{ticket.pk}/state/").json()
        self.assertEqual(state["bot_deliveries"][0]["last_reconciliation"]["status"], "queued")
        Ticket.objects.filter(pk=ticket.pk).update(status="closed", closed_at=timezone.now())
        self.assertEqual(self.request_check(operation).status_code, 403)
        grant_support_membership(self.profile.pk, self.cursor.pk, coordination=True)
        self.assertEqual(self.request_check(operation).status_code, 202)
        self.profile.access_generation += 1
        self.profile.save(update_fields=["access_generation"])
        self.assertEqual(self.request_check(operation).status_code, 403)

    def test_late_confirmation_attaches_to_original_closed_card(self):
        ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)
        self.run.handoff_ticket = ticket
        self.run.save(update_fields=["handoff_ticket"])
        self.request_check()
        job = claim([TASK_TYPE])
        begin_lookup(job, 17, 41)
        Ticket.objects.filter(pk=ticket.pk).update(status="closed", closed_at=timezone.now())
        newer = Ticket.objects.create(dialog=self.dialog)
        complete_lookup(job, 17, [self.item()])
        self.assertEqual(Message.objects.get().ticket_id, ticket.pk)
        self.assertNotEqual(Message.objects.get().ticket_id, newer.pk)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "closed")

    def test_expired_lease_rolls_back_fact_and_requeues_same_check(self):
        self.request_check()
        job = claim([TASK_TYPE])
        begin_lookup(job, 17, 41)
        Task.objects.filter(pk=job.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(LeaseLost):
            complete_lookup(job, 17, [self.item()])
        self.assertFalse(Evidence.objects.exists())
        self.assertFalse(Message.objects.exists())
        self.assertTrue(recover_expired())
        self.assertEqual(Check.objects.get().status, "queued")
        self.assertEqual(Task.objects.get(pk=job.pk).status, Task.Status.RETRY)
        run_reconciliation_once(17, 41, Mock(return_value=[self.item()]))
        self.assertEqual(Check.objects.get().outcome, "accepted_by_vk")

    def test_restore_cancels_running_check_and_replay_reads_old_result(self):
        operation = uuid4()
        self.request_check(operation)
        job = claim([TASK_TYPE])
        begin_lookup(job, 17, 41)
        old_revision = self.delivery.uncertainty_revision
        with override_settings(STAFF_QUARANTINE_INCIDENT="reconcile-restore"):
            isolate_restored_bot_deliveries(uuid4())
        with self.assertRaises(LeaseLost):
            complete_lookup(job, 17, [self.item()])
        check = Check.objects.get()
        self.assertEqual((check.outcome, check.reason, check.task.status), ("unresolved", "recovered_from_backup", "failed"))
        self.assertEqual(self.request_check(operation).status_code, 200)
        self.delivery.refresh_from_db()
        self.assertNotEqual(old_revision, self.delivery.uncertainty_revision)
        state = self.client.get(f"/support/ai-parts/{self.part.pk}/state/").json()
        self.assertIsNone(state["last_reconciliation"])
        self.assertFalse(Message.objects.exists())

    def test_uuid_mismatch_and_failed_enqueue_roll_back_command(self):
        with patch("support.ai_reconciliation.enqueue", side_effect=RuntimeError("write failure")):
            with self.assertRaises(RuntimeError):
                self.request_check()
        self.assertFalse(Check.objects.exists())
        self.assertFalse(SupportCommand.objects.exists())
        operation = uuid4()
        self.request_check(operation)
        other = AIAnswerPart.objects.create(answer=self.answer, position=2, text_snapshot="Часть")
        now = timezone.now()
        AIPartDelivery.objects.create(part=other, created_at=now, updated_at=now)
        self.url = f"/support/ai-parts/{other.pk}/reconcile/"
        self.assertEqual(self.request_check(operation).json()["error"]["code"], "operation_mismatch")

    def test_completion_failure_rolls_back_all_records(self):
        self.request_check()
        job = claim([TASK_TYPE])
        begin_lookup(job, 17, 41)
        with patch("support.ai_reconciliation.finish", side_effect=RuntimeError("write failure")):
            with self.assertRaises(RuntimeError):
                complete_lookup(job, 17, [self.item()])
        self.assertFalse(Evidence.objects.exists())
        self.assertFalse(Message.objects.exists())
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "unknown_outcome")
        self.assertEqual(Check.objects.get().status, "running")

    def test_quota_defers_without_history_and_finishes_when_budget_is_short(self):
        self.request_check()
        LongPollCursor.objects.filter(pk=self.cursor.pk).update(history_quota_started_at=timezone.now(), history_quota_used=12)
        history = Mock()
        run_reconciliation_once(17, 41, history)
        check = Check.objects.get()
        self.assertEqual((check.status, check.task.status), ("queued", Task.Status.RETRY))
        Check.objects.filter(pk=check.pk).update(lookup_deadline_at=timezone.now() + timedelta(seconds=3))
        Task.objects.filter(pk=check.task_id).update(scheduled_at=timezone.now() - timedelta(seconds=1))
        run_reconciliation_once(17, 41, history)
        check.refresh_from_db()
        self.assertEqual((check.status, check.outcome, check.reason), ("finished", "unresolved", "lookup_budget_exhausted"))
        history.assert_not_called()

    def test_state_and_journal_offer_command_and_require_csrf(self):
        page = self.client.get(f"/support/ai-answers/{self.answer.pk}/")
        self.assertContains(page, "Проверить отправку части")
        self.assertContains(page, self.url)
        client, profile = signed_in("bot-reconcile-csrf", csrf=True)
        grant_support_membership(profile.pk, self.cursor.pk)
        self.assertEqual(self.request_check(client=client).status_code, 403)
        self.request_check()
        state = self.client.get(f"/support/ai-parts/{self.part.pk}/state/").json()
        self.assertEqual(state["last_reconciliation"]["status"], "queued")
        self.assertTrue(state["checking"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            Check.objects.update(status="finished", outcome="accepted_by_vk", finished_at=timezone.now())

    @override_settings(VK_GROUP_ID="17", VK_LONG_POLL_PILOT_PEER_ID="41", VK_HISTORY_CORRELATION_VERIFIED=False,
                       VK_ACCESS_TOKEN="")
    def test_command_with_unverified_channel_finishes_without_network(self):
        self.request_check()
        with patch("support.management.commands.run_vk_bot_reconciliation.read_message_history") as history:
            call_command("run_vk_bot_reconciliation", once=True, stdout=StringIO())
            history.assert_not_called()
        self.assertEqual((Check.objects.get().outcome, Check.objects.get().reason), ("unresolved", "history_unavailable"))
        self.assertFalse(Message.objects.exists())

    def test_parallel_commands_create_one_active_check(self):
        barrier = Barrier(2)

        def request(client):
            try:
                barrier.wait(timeout=5)
                return self.request_check(client=client).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(request, client) for client in (self.client, self.other_client)]
            self.assertCountEqual([task.result(timeout=15) for task in tasks], [202, 409])
        self.assertEqual(Check.objects.count(), 1)

    def test_corrupt_reading_binding_aborts_recovery_without_revoking_foreign_job(self):
        self.request_check()
        check = Check.objects.get()
        Task.objects.filter(pk=check.task_id).update(payload={"delivery_id": self.delivery.pk})
        old_revision = self.delivery.uncertainty_revision
        with override_settings(STAFF_QUARANTINE_INCIDENT="reconcile-restore"):
            with self.assertRaises(ValueError):
                isolate_restored_bot_deliveries(uuid4())
        check.refresh_from_db()
        self.delivery.refresh_from_db()
        self.assertEqual((check.status, check.task.status), ("queued", "pending"))
        self.assertEqual(self.delivery.uncertainty_revision, old_revision)
