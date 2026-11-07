from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from accounts.services import AccessError
from jobs.models import BackgroundTask as Task
from jobs.services import LeaseLost, claim
from support.closing import close_ticket
from support.delivery import retry_response, run_sender_once, send_response
from support.models import (
    DeliveryRecoveryRun, Dialog, Draft, Message, OutgoingAttempt as Attempt,
    OutgoingResponse as Response, ReconciliationEvidence, ResponseRecoveryGuard as Guard, Ticket,
)
from support.reconciliation import (
    TASK_TYPE, begin_lookup, complete_lookup, matching_message, recover_expired_lookup,
    request_reconciliation, run_reconciliation_once,
)
from support.recovery import RecoveryConflict, recover_support_delivery, register_recovery
from support.services import grant_support_membership
from vk_channel.client import DeliveryRejected, DeliveryUnknown
from vk_channel.models import LongPollCursor

from .test_http import signed_in


@override_settings(DEBUG=True)
class RecoveryTests(TransactionTestCase):
    def setUp(self):
        self.cursor = LongPollCursor.objects.create(community_id=17, position="100")
        self.dialog = Dialog.objects.create(cursor=self.cursor, vk_peer_id=101)
        self.client, self.profile = signed_in("201")
        grant_support_membership(self.profile.pk, self.cursor.pk)
        self.ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)

    def send(self, draft=None):
        return send_response(self.client.session, self.ticket.pk, uuid4(), 1, "Ответ", draft)[0]

    def failed(self, draft=None):
        attempt = self.send(draft)
        run_sender_once(17, Mock(side_effect=DeliveryRejected(901)))
        attempt.refresh_from_db()
        return attempt

    def recover(self, run_id=None):
        with override_settings(STAFF_QUARANTINE_INCIDENT="test-restore"):
            run = register_recovery(run_id or uuid4())
            changed = recover_support_delivery(run.pk)
        return run, changed

    def item(self, response, message_id=192):
        return {"random_id": response.vk_random_id, "id": message_id, "from_id": -17,
                "peer_id": 101, "out": 1, "date": int(timezone.now().timestamp()), "text": response.text}

    def test_cold_recovery_is_idempotent_and_only_blocks_restored_responses(self):
        failed = self.failed()
        pending = self.send()
        run, changed = self.recover()
        self.assertTrue(changed)
        pending.refresh_from_db()
        failed.refresh_from_db()
        self.assertEqual((pending.status, pending.response.status, pending.task.status),
                         ("unknown_outcome", "unknown_outcome", "unknown_outcome"))
        self.assertIsNone(pending.started_at)
        self.assertEqual(pending.failure_reason, "recovered_from_backup")
        self.assertTrue(failed.response.recovery_verification_required)
        with self.assertRaisesMessage(AccessError, "recovery_verification_required"):
            retry_response(self.client.session, failed.response_id, uuid4(), 1)
        self.assertEqual(Attempt.objects.count(), 2)
        newer = self.failed()
        self.assertFalse(self.recover(run.pk)[1])
        retry_response(self.client.session, newer.response_id, uuid4(), 1)
        self.assertEqual(Guard.objects.count(), 1)

    def test_empty_history_never_unlocks_recovery_guard(self):
        attempt = self.failed()
        self.recover()
        check, _ = request_reconciliation(self.client.session, attempt.response_id, uuid4())
        self.assertIsNone(check.target_attempt_id)
        run_reconciliation_once(17, Mock(return_value=[]))
        check.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual((check.outcome, check.reason), ("unresolved", "no_unique_match"))
        self.assertEqual(attempt.status, "failed")
        self.assertTrue(attempt.response.recovery_verification_required)
        self.assertIsNone(Guard.objects.get().resolved_at)
        self.assertEqual(Attempt.objects.count(), 1)
        self.assertFalse(ReconciliationEvidence.objects.exists())

    def test_recovery_acceptance_preserves_old_failure_and_draft(self):
        draft = Draft.objects.create(ticket=self.ticket, author=self.profile, text="Ответ")
        attempt = self.failed((draft.pk, 1))
        run, _ = self.recover()
        check, _ = request_reconciliation(self.client.session, attempt.response_id, uuid4())
        run_reconciliation_once(17, Mock(return_value=[self.item(attempt.response)]))
        attempt.refresh_from_db()
        draft.refresh_from_db()
        guard = Guard.objects.get()
        self.assertEqual((attempt.status, attempt.task.status, attempt.response.status), ("failed", "failed", "sent"))
        self.assertEqual((draft.text, draft.revision), ("Ответ", 1))
        self.assertEqual(guard.resolution, "accepted_by_vk")
        self.assertEqual(guard.evidence_ref.reconciliation_id, check.pk)
        self.assertEqual(guard.evidence_ref.facts["recovery_run_id"], str(run.pk))
        self.assertIsNotNone(attempt.response.confirmed_sent_at)
        self.assertFalse(attempt.response.recovery_verification_required)
        self.assertEqual(Message.objects.get().ticket_id, self.ticket.pk)
        self.assertEqual(Attempt.objects.count(), 1)
        self.assertFalse(self.recover(run.pk)[1])
        repeated, replayed = request_reconciliation(self.client.session, attempt.response_id, check.command.client_operation_id)
        self.assertTrue(replayed)
        self.assertEqual(repeated.pk, check.pk)

    def test_new_recovery_supersedes_guard_and_revokes_running_check(self):
        attempt = self.failed()
        self.recover()
        check, _ = request_reconciliation(self.client.session, attempt.response_id, uuid4())
        job = claim([TASK_TYPE])
        parameters = begin_lookup(job, 17)
        proof = matching_message([self.item(attempt.response)], 17, *parameters)
        old_guard = check.target_recovery_guard
        self.recover()
        old_guard.refresh_from_db()
        check.refresh_from_db()
        self.assertEqual(old_guard.resolution, "superseded")
        self.assertEqual((check.outcome, check.reason, check.task.status),
                         ("unresolved", "recovered_from_backup", "failed"))
        with self.assertRaises(LeaseLost):
            complete_lookup(job, proof, "")
        self.assertEqual(Guard.objects.filter(resolved_at__isnull=True).count(), 1)
        self.assertFalse(Message.objects.exists())
        self.assertFalse(ReconciliationEvidence.objects.exists())

    def test_restored_null_start_cannot_prove_non_admission_but_can_match_success(self):
        attempt = self.send()
        self.recover()
        check, _ = request_reconciliation(self.client.session, attempt.response_id, uuid4())
        history = Mock(return_value=[])
        run_reconciliation_once(17, history)
        history.assert_called_once()
        check.refresh_from_db()
        self.assertEqual(check.outcome, "unresolved")
        check, _ = request_reconciliation(self.client.session, attempt.response_id, uuid4())
        run_reconciliation_once(17, Mock(return_value=[self.item(attempt.response)]))
        attempt.refresh_from_db()
        self.assertEqual(attempt.response.status, "sent")
        self.assertIsNone(attempt.started_at)

    def test_actual_database_non_admission_allows_only_explicit_retry(self):
        attempt = self.send()
        Attempt.objects.filter(pk=attempt.pk).update(status=Attempt.Status.UNKNOWN)
        Response.objects.filter(pk=attempt.response_id).update(status=Response.Status.UNKNOWN)
        Task.objects.filter(pk=attempt.task_id).update(status=Task.Status.UNKNOWN)
        check, _ = request_reconciliation(self.client.session, attempt.response_id, uuid4())
        history = Mock()
        run_reconciliation_once(17, history)
        history.assert_not_called()
        check.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual((check.outcome, attempt.status, attempt.response.status, attempt.task.status),
                         ("not_sent_verified", "failed", "failed", "failed"))
        self.assertTrue(check.reconciliationevidence.facts["no_external_admission"])
        self.assertEqual(Attempt.objects.count(), 1)
        retry_response(self.client.session, attempt.response_id, uuid4(), 1)
        self.assertEqual(Attempt.objects.count(), 2)

    def test_admitted_call_prevents_negative_proof(self):
        attempt = self.send()
        run_sender_once(17, Mock(side_effect=DeliveryUnknown))
        check, _ = request_reconciliation(self.client.session, attempt.response_id, uuid4())
        run_reconciliation_once(17)
        check.refresh_from_db()
        self.assertEqual((check.outcome, check.reason), ("unresolved", "history_unavailable"))
        self.assertFalse(ReconciliationEvidence.objects.exists())

    def test_invalid_binding_rolls_back_whole_pass_and_keeps_run_incomplete(self):
        first = self.send()
        second = self.send()
        Task.objects.filter(pk=second.task_id).update(payload={"response_id": first.response_id})
        with override_settings(STAFF_QUARANTINE_INCIDENT="test-restore"):
            run = register_recovery(uuid4())
            with self.assertRaises(RecoveryConflict):
                recover_support_delivery(run.pk)
        first.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual((first.status, first.task.status, first.response.status), ("in_flight", "pending", "pending"))
        self.assertIsNone(run.completed_at)
        self.assertFalse(Guard.objects.exists())

    def test_guard_is_preserved_on_expired_lease_and_completion_rollback(self):
        attempt = self.failed()
        self.recover()
        check, _ = request_reconciliation(self.client.session, attempt.response_id, uuid4())
        job = claim([TASK_TYPE])
        parameters = begin_lookup(job, 17)
        proof = matching_message([self.item(attempt.response)], 17, *parameters)
        with patch("support.reconciliation.finish", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                complete_lookup(job, proof, "")
        self.assertFalse(ReconciliationEvidence.objects.exists())
        self.assertFalse(Message.objects.exists())
        self.assertIsNone(Guard.objects.get().resolved_at)
        Task.objects.filter(pk=job.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(LeaseLost):
            complete_lookup(job, proof, "")
        self.assertTrue(recover_expired_lookup())
        check.refresh_from_db()
        self.assertEqual(check.status, "queued")
        self.assertIsNone(Guard.objects.get().resolved_at)

    def test_new_guard_invalidates_close_warning_and_closing_preserves_it(self):
        attempt = self.failed()
        self.recover()
        with self.assertRaises(AccessError) as warning:
            close_ticket(self.client.session, self.ticket.pk, uuid4(), 1, False, None)
        self.assertFalse(warning.exception.details["warning"]["has_failed_responses"])
        self.assertTrue(warning.exception.details["warning"]["has_unknown_outcome"])
        token = warning.exception.details["warning"]["warning_token"]
        self.recover()
        with self.assertRaisesMessage(AccessError, "close_warning_stale") as stale:
            close_ticket(self.client.session, self.ticket.pk, uuid4(), 1, True, token)
        token = stale.exception.details["warning"]["warning_token"]
        close_ticket(self.client.session, self.ticket.pk, uuid4(), 1, True, token)
        self.assertEqual(Guard.objects.filter(response_id=attempt.response_id, resolved_at__isnull=True).count(), 1)

    def test_http_shows_guard_blocks_retry_and_queues_check(self):
        attempt = self.failed()
        self.recover()
        url = f"/support/responses/{attempt.response_id}/"
        state = self.client.get(url + "state/").json()
        self.assertFalse(state["can_retry"])
        self.assertTrue(state["response"]["recovery_verification_required"])
        page = self.client.get(f"/support/tickets/{self.ticket.pk}/")
        self.assertContains(page, "После восстановления требуется сверка отправки")
        self.assertContains(page, url + "reconcile/")
        response = self.client.post(url + "retry/", {"client_operation_id": str(uuid4()),
                                   "expected_assignment_version": 1}, content_type="application/json")
        self.assertEqual(response.status_code, 409)
        response = self.client.post(url + "reconcile/", {"client_operation_id": str(uuid4())}, content_type="application/json")
        self.assertEqual(response.status_code, 202)

    def test_command_requires_quarantine_and_does_not_enable_network(self):
        with self.assertRaises(RecoveryConflict):
            register_recovery(uuid4())
        attempt = self.failed()
        with override_settings(STAFF_QUARANTINE_INCIDENT="test-restore"):
            call_command("recover_support_delivery", recovery_run_id=uuid4(), stdout=StringIO())
        check, _ = request_reconciliation(self.client.session, attempt.response_id, uuid4())
        with override_settings(VK_GROUP_ID="17", VK_HISTORY_CORRELATION_VERIFIED=False, VK_ACCESS_TOKEN=""):
            with patch("support.management.commands.run_support_sender.read_message_history") as history:
                call_command("run_support_sender", reconcile_only=True, once=True, stdout=StringIO())
                history.assert_not_called()
        check.refresh_from_db()
        self.assertEqual(check.reason, "history_unavailable")

    def test_guard_constraints_reject_duplicate_or_incomplete_resolution(self):
        attempt = self.failed()
        self.recover()
        guard = Guard.objects.get()
        other = DeliveryRecoveryRun.objects.create(id=uuid4(), incident="test-restore")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Guard.objects.create(response_id=attempt.response_id, recovery_run=other)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Guard.objects.filter(pk=guard.pk).update(resolved_at=timezone.now(), resolution="accepted_by_vk")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Guard.objects.filter(pk=guard.pk).update(resolved_at=timezone.now())
