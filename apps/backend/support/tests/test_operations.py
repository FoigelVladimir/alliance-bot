from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock
from threading import Barrier
from uuid import uuid4

from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone

from accounts.models import StaffProfile
from accounts.services import AccessError
from jobs.models import BackgroundTask
from jobs.services import LeaseLost, claim
from support.closing import close_ticket
from support.delivery import (
    TASK_TYPE, admit, finish_delivery, recover_expired_delivery, retry_response, run_sender_once, send_response,
)
from support.models import Dialog, Draft, Message, OutgoingAttempt, ReconciliationEvidence, Ticket, TicketEvent
from support.reconciliation import (
    TASK_TYPE as RECONCILE_TASK, begin_lookup, complete_lookup, matching_message, recover_expired_lookup,
    request_reconciliation, run_reconciliation_once,
)
from support.services import change_assignment, grant_support_membership, save_draft, take_ticket
from vk_channel.client import DeliveryRejected, DeliveryUnknown
from vk_channel.models import LongPollCursor

from .test_http import signed_in


class SupportConcurrencyTests(TransactionTestCase):
    def test_two_managers_cannot_take_the_same_version(self):
        cursor = LongPollCursor.objects.create(community_id=17, position="100")
        dialog = Dialog.objects.create(cursor=cursor, vk_peer_id=101)
        ticket = Ticket.objects.create(dialog=dialog)
        participants = [signed_in("201"), signed_in("202")]
        for _, profile in participants:
            grant_support_membership(profile.pk, cursor.pk)
        barrier = Barrier(2)

        def compete(client):
            try:
                session = client.session
                barrier.wait(timeout=5)
                take_ticket(session, ticket.pk, uuid4(), 1)
                return "taken"
            except AccessError as error:
                return error.code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(compete, client) for client, _ in participants]
            self.assertCountEqual(
                [result.result(timeout=10) for result in results], ["taken", "assignment_version_conflict"],
            )
        ticket.refresh_from_db()
        self.assertEqual(ticket.assignment_version, 2)
        self.assertEqual(TicketEvent.objects.get().command.actor_id, ticket.current_owner_id)


class DeliveryTests(TransactionTestCase):
    def setUp(self):
        self.cursor = LongPollCursor.objects.create(community_id=17, position="100")
        self.dialog = Dialog.objects.create(cursor=self.cursor, vk_peer_id=101)
        self.client, self.profile = signed_in("201")
        grant_support_membership(self.profile.pk, self.cursor.pk)
        self.ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)

    def send(self, **kwargs):
        return send_response(self.client.session, self.ticket.pk, uuid4(), 1, "  Ответ 🙂\n", kwargs.get("draft"))[0]

    def test_success_records_one_message_and_clears_only_unchanged_draft(self):
        draft = Draft.objects.create(ticket=self.ticket, author=self.profile, text="  Ответ 🙂\n")
        attempt = self.send(draft=(draft.pk, 1))
        send = Mock(return_value=123)
        self.assertTrue(run_sender_once(17, send))
        send.assert_called_once_with(101, attempt.response.vk_random_id, "  Ответ 🙂\n")
        self.assertFalse(run_sender_once(17, send))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "succeeded")
        self.assertEqual(attempt.response.status, "sent")
        self.assertEqual(attempt.response.provider_message_id, 123)
        self.assertEqual(Message.objects.get().ticket_id, self.ticket.pk)
        draft.refresh_from_db()
        self.assertEqual((draft.text, draft.revision), ("", 2))

    def test_unknown_outcome_is_atomic_and_blocks_retry(self):
        attempt = self.send()
        send = Mock(side_effect=DeliveryUnknown)
        run_sender_once(17, send)
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.response.status, attempt.task.status),
                         ("unknown_outcome", "unknown_outcome", "unknown_outcome"))
        with self.assertRaisesMessage(AccessError, "outcome_unknown"):
            retry_response(self.client.session, attempt.response_id, uuid4(), 1)
        self.assertFalse(run_sender_once(17, send))
        self.assertEqual(OutgoingAttempt.objects.count(), 1)
        send.assert_called_once()

    def test_known_rejection_allows_retry_with_same_vk_random_id(self):
        first = self.send()
        send = Mock(side_effect=[DeliveryRejected(901), 123])
        run_sender_once(17, send)
        retry_id = uuid4()
        second, _ = retry_response(self.client.session, first.response_id, retry_id, 1)
        run_sender_once(17, send)
        repeated, replayed = retry_response(self.client.session, first.response_id, retry_id, 1)
        self.assertEqual(repeated.pk, second.pk)
        self.assertTrue(replayed)
        self.assertEqual(repeated.status, "succeeded")
        self.assertEqual(send.call_args_list[0], send.call_args_list[1])
        self.assertEqual(Message.objects.count(), 1)

    def test_worker_lost_after_admission_cannot_resend_or_overwrite(self):
        attempt = self.send()
        task = claim([TASK_TYPE])
        self.assertIsNotNone(admit(task, 17))
        BackgroundTask.objects.filter(pk=task.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(LeaseLost):
            finish_delivery(task, OutgoingAttempt.Status.SUCCEEDED, provider_id=123)
        self.assertTrue(recover_expired_delivery())
        send = Mock()
        self.assertFalse(run_sender_once(17, send))
        send.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.response.status), ("unknown_outcome", "unknown_outcome"))
        self.assertFalse(Message.objects.exists())

    def test_access_revoked_before_admission_skips_network(self):
        attempt = self.send()
        StaffProfile.objects.filter(pk=self.profile.pk).update(access_state=StaffProfile.Access.REVOKED)
        send = Mock()
        run_sender_once(17, send)
        send.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.response.status), ("failed", "failed"))
        self.assertIsNone(attempt.started_at)

    def queue_reconciliation(self):
        attempt = self.send()
        run_sender_once(17, Mock(side_effect=DeliveryUnknown))
        attempt.refresh_from_db()
        check, replayed = request_reconciliation(self.client.session, attempt.response_id, uuid4())
        self.assertFalse(replayed)
        return attempt, check

    def history_item(self, attempt, **overrides):
        return {
            "random_id": attempt.response.vk_random_id, "id": 192, "from_id": -17,
            "peer_id": 101, "out": 1, "date": int(attempt.started_at.timestamp()), "text": "  Ответ 🙂\n",
            **overrides,
        }

    def test_reconciliation_records_unique_provider_proof_and_updates_original_task(self):
        attempt, check = self.queue_reconciliation()
        history = Mock(return_value=[self.history_item(attempt)])
        self.assertTrue(run_reconciliation_once(17, history))
        history.assert_called_once_with(101)
        attempt.refresh_from_db()
        check.refresh_from_db()
        self.assertEqual((attempt.status, attempt.response.status, attempt.task.status), ("succeeded", "sent", "done"))
        self.assertEqual((check.status, check.outcome, check.task.status), ("finished", "accepted_by_vk", "done"))
        self.assertEqual(check.reconciliationevidence.facts["message_id"], 192)
        self.assertEqual(Message.objects.get().ticket_id, self.ticket.pk)
        self.assertEqual(OutgoingAttempt.objects.count(), 1)
        again, replayed = request_reconciliation(self.client.session, attempt.response_id, check.command.client_operation_id)
        self.assertTrue(replayed)
        self.assertEqual(again.pk, check.pk)

    def test_same_text_and_ambiguous_history_leave_send_blocked(self):
        attempt, check = self.queue_reconciliation()
        with self.assertRaisesMessage(AccessError, "reconciliation_in_progress"):
            request_reconciliation(self.client.session, attempt.response_id, uuid4())
        run_reconciliation_once(17, Mock(return_value=[self.history_item(attempt, random_id=None)]))
        check.refresh_from_db()
        self.assertEqual((check.outcome, check.reason), ("unresolved", "no_unique_match"))
        again, _ = request_reconciliation(self.client.session, attempt.response_id, uuid4())
        run_reconciliation_once(17, Mock(return_value=[self.history_item(attempt), self.history_item(attempt, id=193)]))
        again.refresh_from_db()
        self.assertEqual(again.outcome, "unresolved")
        attempt.refresh_from_db()
        self.assertEqual(attempt.response.status, "unknown_outcome")
        with self.assertRaisesMessage(AccessError, "outcome_unknown"):
            retry_response(self.client.session, attempt.response_id, uuid4(), 1)
        self.assertFalse(Message.objects.exists())
        self.assertFalse(ReconciliationEvidence.objects.exists())

    def test_expired_lookup_rolls_back_evidence_and_requeues_then_exhausts(self):
        attempt, check = self.queue_reconciliation()
        job = claim([RECONCILE_TASK])
        begin_lookup(job, 17)
        expired = timezone.now() - timedelta(seconds=1)
        BackgroundTask.objects.filter(pk=job.pk).update(locked_until=expired)
        evidence = matching_message([self.history_item(attempt)], 17, 101, attempt.response.vk_random_id,
                                    attempt.response.text, attempt.started_at)
        with self.assertRaises(LeaseLost):
            complete_lookup(job, evidence, "")
        self.assertFalse(ReconciliationEvidence.objects.exists())
        self.assertFalse(Message.objects.exists())
        self.assertTrue(recover_expired_lookup())
        check.refresh_from_db()
        self.assertEqual((check.status, check.task.status), ("queued", "retry_scheduled"))
        second = claim([RECONCILE_TASK])
        begin_lookup(second, 17)
        BackgroundTask.objects.filter(pk=second.pk).update(locked_until=expired, max_attempts=2)
        self.assertTrue(recover_expired_lookup())
        check.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual((check.reason, check.task.status), ("lookup_budget_exhausted", "dead_letter"))
        self.assertEqual(attempt.response.status, "unknown_outcome")

    def test_closed_card_requires_coordinator_even_on_replay(self):
        attempt = self.send()
        run_sender_once(17, Mock(side_effect=DeliveryUnknown))
        Ticket.objects.filter(pk=self.ticket.pk).update(status=Ticket.Status.CLOSED, closed_at=timezone.now())
        with self.assertRaisesMessage(AccessError, "permission_denied"):
            request_reconciliation(self.client.session, attempt.response_id, uuid4())
        grant_support_membership(self.profile.pk, self.cursor.pk, coordination=True)
        operation_id = uuid4()
        request_reconciliation(self.client.session, attempt.response_id, operation_id)
        grant_support_membership(self.profile.pk, self.cursor.pk, coordination=False)
        with self.assertRaisesMessage(AccessError, "permission_denied"):
            request_reconciliation(self.client.session, attempt.response_id, operation_id)

    def test_delivered_snapshot_does_not_clear_newer_draft(self):
        draft = Draft.objects.create(ticket=self.ticket, author=self.profile, text="  Ответ 🙂\n")
        self.send(draft=(draft.pk, 1))
        save_draft(self.client.session, self.ticket.pk, 1, 1, "Новый ответ")
        run_sender_once(17, Mock(return_value=123))
        draft.refresh_from_db()
        self.assertEqual((draft.text, draft.revision), ("Новый ответ", 2))

    def test_takeover_cancels_only_not_started_delivery(self):
        other_client, other = signed_in("204")
        grant_support_membership(other.pk, self.cursor.pk)
        started = self.send()
        job = claim([TASK_TYPE])
        admit(job, 17)
        reserved = self.send()
        change_assignment(other_client.session, self.ticket.pk, uuid4(), 1, "takeover")
        reserved.refresh_from_db()
        started.refresh_from_db()
        self.assertEqual((reserved.status, reserved.response.status, reserved.task.status),
                         ("failed", "cancelled_by_takeover", "failed"))
        self.assertEqual(started.status, "in_flight")
        finish_delivery(job, OutgoingAttempt.Status.SUCCEEDED, provider_id=444)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.current_owner_id, other.pk)
        started.refresh_from_db()
        self.assertEqual(started.response.status, "sent")

    def test_close_cancels_reserved_keeps_unknown_and_preserves_draft_on_late_success(self):
        draft = Draft.objects.create(ticket=self.ticket, author=self.profile, text="  Ответ 🙂\n")
        unknown = self.send()
        run_sender_once(17, Mock(side_effect=DeliveryUnknown))
        started = self.send(draft=(draft.pk, 1))
        job = claim([TASK_TYPE])
        admit(job, 17)
        reserved = self.send()
        with self.assertRaises(AccessError) as error:
            close_ticket(self.client.session, self.ticket.pk, uuid4(), 1, False, None)
        warning = error.exception.details["warning"]
        for key in ("has_unknown_outcome", "has_waiting_attempts", "has_started_attempts", "has_uncommitted_draft"):
            self.assertTrue(warning[key])
        close_ticket(self.client.session, self.ticket.pk, uuid4(), 1, True, warning["warning_token"])
        reserved.refresh_from_db()
        unknown.refresh_from_db()
        self.assertEqual((reserved.status, reserved.task.status), ("failed", "failed"))
        self.assertEqual((unknown.status, unknown.response.status), ("unknown_outcome", "unknown_outcome"))
        self.assertIsNone(unknown.finished_at)
        finish_delivery(job, OutgoingAttempt.Status.SUCCEEDED, provider_id=888)
        draft.refresh_from_db()
        self.assertEqual(draft.revision, 1)
        started.refresh_from_db()
        self.assertEqual(started.response.status, "sent")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.Status.CLOSED)
