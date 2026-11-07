import json
from datetime import timedelta
from unittest.mock import Mock, call, patch
from uuid import uuid4

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from jobs.models import BackgroundTask
from jobs.services import claim
from jobs.worker import run_one
from support.delivery import run_sender_once
from support.models import Attachment, Dialog, InboundRoute, Message, OutgoingResponse, Ticket

from support.services import grant_support_membership
from support.tests.test_http import signed_in
from vk_channel import services
from vk_channel.backfill import MAX_CALLS_PER_MINUTE, mark_restored_gaps, resume_gap, run_gap_once
from vk_channel.client import DeliveryUnknown, LongPollServer, long_poll_server
from vk_channel.models import InboundEvent, LongPollCursor, LongPollGap, StudentClick
from vk_channel.student_actions import (
    ANSWER_CLICK, HANDLE_CLICK, handle_click_task, issue_ticket_action, run_click_answer_once,
)
from vk_channel.services import (
    GAP_TASK, TASK_TYPE, InvalidBatch, ReaderLeaseLost, claim_reader, commit_batch, record_gap,
    poll_once, route_inbound_task, save_historical_batch,
)


def incoming(cmid=7, peer=41, community=17, **fields):
    return {
        "type": "message_new", "group_id": community,
        "object": {"message": {
            "peer_id": peer, "conversation_message_id": cmid, "from_id": peer,
            "text": "Когда тренировка?", **fields,
        }},
    }


class InboundChannelTests(TransactionTestCase):
    def test_replay_is_unique_per_community_and_dialog(self):
        reader = claim_reader(17, "001")
        reader = commit_batch(reader, "002", [incoming(), incoming(peer=42)])
        commit_batch(reader, "003", [incoming(text="Повтор с другим текстом")])
        other = claim_reader(18, "200")
        commit_batch(other, "201", [incoming(community=18)])
        self.assertEqual(Message.objects.count(), 3)
        self.assertEqual(BackgroundTask.objects.count(), 3)
        self.assertEqual(InboundEvent.objects.count(), 3)
        self.assertEqual(Message.objects.first().text, "Когда тренировка?")
        self.assertEqual(LongPollCursor.objects.get(community_id=17).position, "003")

    def test_save_failure_rolls_back_events_messages_tasks_and_position(self):
        reader = claim_reader(17, "001")
        real_enqueue = services.enqueue

        def fail_second(task_type, key, payload):
            if Message.objects.count() == 2:
                raise RuntimeError("simulated queue failure")
            return real_enqueue(task_type, key, payload)

        with patch.object(services, "enqueue", side_effect=fail_second):
            with self.assertRaisesMessage(RuntimeError, "simulated queue failure"):
                commit_batch(reader, "002", [incoming(), incoming(cmid=8)])
        self.assertEqual(LongPollCursor.objects.get().position, "001")
        self.assertFalse(InboundEvent.objects.exists())
        self.assertFalse(Message.objects.exists())
        self.assertFalse(BackgroundTask.objects.exists())
        commit_batch(reader, "002", [incoming(), incoming(cmid=8)])
        self.assertEqual(Message.objects.count(), 2)

    def test_reader_takeover_and_replayed_confirmation_are_fenced(self):
        old = claim_reader(17, "001")
        self.assertIsNone(claim_reader(17, "999"))
        LongPollCursor.objects.update(leased_until=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(ReaderLeaseLost):
            commit_batch(old, "002", [incoming()])
        current = claim_reader(17, "999")
        self.assertEqual(current.position, "001")
        with self.assertRaises(ReaderLeaseLost):
            commit_batch(old, "002", [incoming()])
        next_reader = commit_batch(current, "001", [])
        with self.assertRaises(ReaderLeaseLost):
            commit_batch(current, "002", [incoming()])
        commit_batch(next_reader, "002", [incoming()])
        self.assertEqual(Message.objects.count(), 1)

    def test_quarantine_advances_cursor_but_unrecognizable_batch_does_not(self):
        reader = claim_reader(17, "001")
        with self.assertRaises(InvalidBatch):
            commit_batch(reader, "002", [incoming(), None])
        self.assertFalse(InboundEvent.objects.exists())
        commit_batch(reader, "002", [
            incoming(cmid=None), incoming(community=99), {"type": "unknown_event", "secret": "ignore"},
        ])
        self.assertEqual(list(InboundEvent.objects.order_by("pk").values_list("status", "reason")), [
            ("quarantined", "invalid_message_identity"),
            ("quarantined", "community_mismatch"),
            ("unsupported", "unsupported_type"),
        ])
        self.assertFalse(BackgroundTask.objects.exists())
        self.assertEqual(LongPollCursor.objects.get().position, "002")

    def test_history_and_live_messages_keep_their_original_ticket_links(self):
        reader = claim_reader(17, "001")
        dialog = Dialog.objects.create(cursor_id=reader.cursor_id, vk_peer_id=41)
        ticket = Ticket.objects.create(dialog=dialog)
        reader = commit_batch(reader, "002", [incoming(attachments=[
            {"type": "doc", "doc": {"title": "schedule.pdf", "size": 512, "url": "https://private.invalid"}},
        ])])
        gap = record_gap(reader, "900", "position_lost")
        with self.assertRaises(ReaderLeaseLost):
            commit_batch(reader, "003", [])
        save_historical_batch(gap.pk, [incoming(), incoming(cmid=8)])
        next_reader = claim_reader(17, "999")
        commit_batch(next_reader, "901", [incoming(cmid=8), incoming(cmid=9)])
        self.assertEqual(list(Message.objects.order_by("conversation_message_id").values_list("ticket_id", flat=True)),
                         [ticket.pk, None, None])
        metadata = Attachment.objects.get()
        self.assertEqual(metadata.meta_info, {"size": 512, "title": "schedule.pdf"})
        self.assertEqual(metadata.source_ref, {"peer_id": 41, "cmid": 7, "url": "https://private.invalid"})
        self.assertEqual(metadata.state, "metadata_only")
        while run_one({TASK_TYPE: route_inbound_task}):
            pass
        routes = list(InboundRoute.objects.order_by("sequence").values_list("status", "reason", "automation_epoch"))
        self.assertEqual(routes, [("attached", "", 1), ("needs_review", "historical", 2), ("needs_review", "channel_paused", 2)])
        self.assertTrue(LongPollCursor.objects.get().automation_paused)
        self.assertEqual(BackgroundTask.objects.filter(status=BackgroundTask.Status.DONE).count(), 3)

    def test_long_poll_refreshes_expired_key_from_confirmed_position(self):
        reader = claim_reader(17, "001")
        original = LongPollServer("https://lp.vk.com/poll", "001", "old")
        replacement = LongPollServer("https://lp.vk.com/poll", "500", "new")
        read = Mock(side_effect=[{"failed": 2}, {"ts": "002", "updates": [incoming()]}])
        discover = Mock(return_value=replacement)
        next_reader, next_server = poll_once(reader, original, read, discover)
        self.assertEqual(read.call_args_list, [call(original, "001"), call(replacement, "001")])
        self.assertEqual(next_reader.position, "002")
        self.assertEqual(next_server, replacement)
        self.assertFalse(LongPollGap.objects.exists())
        self.assertEqual(Message.objects.count(), 1)

    def test_long_poll_gap_does_not_confirm_an_unread_interval(self):
        reader = claim_reader(17, "001")
        original = LongPollServer("https://lp.vk.com/poll", "001", "old")
        replacement = LongPollServer("https://lp.vk.com/poll", "700", "new")
        read = Mock(side_effect=[{"failed": 2}, {"failed": 1, "ts": "501"}])
        next_reader, _ = poll_once(reader, original, read, Mock(return_value=replacement))
        self.assertIsNone(next_reader)
        gap = LongPollGap.objects.get()
        self.assertEqual((gap.previous_position, gap.next_position, gap.reason),
                         ("001", "501", "key_recovery_failed"))
        self.assertTrue(LongPollCursor.objects.get().automation_paused)
        self.assertFalse(Message.objects.exists())

    def test_long_poll_failure_leaves_confirmed_position_and_rejects_untrusted_server(self):
        reader = claim_reader(17, "001")
        server = LongPollServer("https://lp.vk.com/poll", "001", "test")
        with self.assertRaises(InvalidBatch):
            poll_once(reader, server, Mock(return_value={"failed": 99, "ts": "500"}), Mock())
        self.assertEqual(LongPollCursor.objects.get().position, "001")
        self.assertFalse(LongPollGap.objects.exists())
        for url in ("https://vk.com.attacker.invalid/poll", "https://lp.vk.com:444/poll", "http://lp.vk.com/poll"):
            with patch("vk_channel.client.call_vk_method", return_value={
                "response": {"server": url, "key": "test", "ts": "001"},
            }), self.assertRaises(DeliveryUnknown):
                long_poll_server("token", 17)


@override_settings(VK_GAP_HISTORY_VERIFIED=True)
class GapBackfillTests(TransactionTestCase):
    def open_gap(self):
        reader = commit_batch(claim_reader(17, "001"), "002", [incoming()])
        return record_gap(reader, "010", "position_lost")

    def test_history_keeps_known_message_and_requires_review_before_resume(self):
        gap = self.open_gap()
        stamp = int(gap.previous_batch_at.timestamp())
        known = incoming(date=stamp, out=0)["object"]["message"]
        recovered = incoming(cmid=8, peer=42, date=stamp, out=0)["object"]["message"]
        conversations = Mock(return_value={"count": 1, "items": [{"conversation": {"peer": {"id": 42}}}]})
        history = Mock(side_effect=lambda peer, **_: [known] if peer == 41 else [recovered])
        self.assertTrue(run_gap_once(17, conversations, history))
        gap.refresh_from_db()
        self.assertEqual(gap.status, "needs_review")
        self.assertEqual((gap.coverage["found"], gap.coverage["known"]), (2, 1))
        self.assertIn("non_message_events_uncovered", gap.coverage["reasons"])
        self.assertIsNone(InboundEvent.objects.get(conversation_message_id=7).gap_id)
        self.assertEqual(InboundEvent.objects.get(conversation_message_id=8).gap_id, gap.pk)

        _, profile = signed_in("101")
        grant_support_membership(profile.pk, gap.cursor_id, coordination=True)
        with self.assertRaises(ValueError):
            resume_gap(gap.pk, profile.pk, 2, "010", "Граница ещё не подтверждена.")
        reader = commit_batch(claim_reader(17, "010"), "011", [incoming(cmid=8, peer=42), incoming(cmid=9)])
        self.assertEqual(Message.objects.filter(conversation_message_id=8).count(), 1)
        self.assertTrue(InboundRoute.objects.get(message__conversation_message_id=9).automation_paused)
        notes = "Старые действия кнопок не покрыты."
        resume_gap(gap.pk, profile.pk, 2, "011", notes)
        self.assertFalse(LongPollCursor.objects.get().automation_paused)
        later = record_gap(reader, "020", "position_lost")
        resume_gap(gap.pk, profile.pk, 2, "011", notes)
        self.assertTrue(LongPollCursor.objects.get().automation_paused)
        self.assertNotEqual(later.pk, gap.pk)

    def test_quota_and_exhausted_lease_keep_pause(self):
        gap = self.open_gap()
        LongPollCursor.objects.update(history_quota_started_at=timezone.now(), history_quota_used=MAX_CALLS_PER_MINUTE)
        discovery = Mock()
        self.assertTrue(run_gap_once(17, discovery, Mock()))
        discovery.assert_not_called()
        gap.refresh_from_db()
        self.assertIn("history_quota", gap.coverage["reasons"])
        next_gap = record_gap(claim_reader(17, "010"), "020", "position_lost")
        task = claim([GAP_TASK])
        BackgroundTask.objects.filter(pk=task.pk).update(
            locked_until=timezone.now() - timedelta(seconds=1), attempt_count=task.max_attempts,
        )
        self.assertFalse(run_gap_once(17, discovery, Mock()))
        next_gap.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundTask.Status.DEAD)
        self.assertEqual(next_gap.status, "needs_review")
        self.assertIn("lookup_budget_exhausted", next_gap.coverage["reasons"])
        self.assertTrue(LongPollCursor.objects.get().automation_paused)


    @override_settings(STAFF_QUARANTINE_INCIDENT="restore-1")
    def test_restored_cursor_loses_old_lease_and_stays_paused(self):
        reader = commit_batch(claim_reader(17, "001"), "002", [incoming()])
        mark_restored_gaps()
        mark_restored_gaps()
        with self.assertRaises(ReaderLeaseLost):
            commit_batch(reader, "003", [])
        gap = LongPollGap.objects.get()
        self.assertEqual(gap.reason, "database_restore")
        self.assertEqual(gap.previous_position, "002")
        self.assertTrue(LongPollCursor.objects.get().automation_paused)
        self.assertEqual(BackgroundTask.objects.filter(task_type=GAP_TASK).count(), 1)


class StudentClickTests(TransactionTestCase):
    def setUp(self):
        self.reader = claim_reader(17, "001")
        self.dialog = Dialog.objects.create(cursor_id=self.reader.cursor_id, vk_peer_id=41, mode="human_support")
        self.ticket = Ticket.objects.create(dialog=self.dialog)

    def token(self, action):
        data = issue_ticket_action(self.ticket.pk, 41, action, timezone.now() + timedelta(hours=1))
        return json.loads(data["payload"])

    def click(self, payload, event_id, *, user=41):
        return {"type": "message_event", "group_id": 17, "object": {
            "peer_id": 41, "user_id": user, "event_id": event_id, "payload": payload,
        }}

    def receive(self, *clicks):
        self.reader = commit_batch(self.reader, str(self.reader.receipt_version + 2), list(clicks))
        while run_one({HANDLE_CLICK: handle_click_task}):
            pass

    def test_transport_replay_wrong_user_and_unknown_ack(self):
        action = self.token("resolve")
        click = self.click(action, "right")
        self.receive(self.click(action, "wrong", user=42), click, click, self.click(action, "again"))
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.closed_reason, self.ticket.closed_by_vk_id), ("student_resolved", 41))
        self.assertEqual(StudentClick.objects.count(), 3)
        self.assertEqual(StudentClick.objects.get(provider_event_id="wrong").result["status"], "stale")
        self.assertTrue(StudentClick.objects.get(provider_event_id="again").result["replayed"])
        self.assertFalse(Message.objects.exists())
        send = Mock(side_effect=DeliveryUnknown)
        while run_click_answer_once(17, send):
            pass
        self.assertEqual(send.call_count, 3)
        self.receive(click)
        self.assertFalse(run_click_answer_once(17, send))
        self.assertEqual(BackgroundTask.objects.filter(task_type=ANSWER_CLICK, status="unknown_outcome").count(), 3)

    def test_continuation_is_single_and_expired_answer_leaves_result(self):
        self.receive(self.click(self.token("resolve"), "resolve"))
        first, second = self.token("continue"), self.token("continue")
        self.receive(self.click(first, "continue-1"), self.click(second, "continue-2"))
        child = Ticket.objects.get(parent_ticket=self.ticket)
        self.assertEqual(Ticket.objects.count(), 2)
        for click in StudentClick.objects.filter(provider_event_id__startswith="continue"):
            self.assertEqual(click.result["ticket_id"], str(child.pk))
            self.assertEqual(click.answer_text, "Менеджер скоро подключится.")
        StudentClick.objects.update(answer_deadline=timezone.now() - timedelta(seconds=1))
        send = Mock()
        while run_click_answer_once(17, send):
            pass
        send.assert_not_called()
        self.assertEqual(BackgroundTask.objects.filter(task_type=ANSWER_CLICK, result__reason="answer_expired").count(), 3)
        self.assertTrue(Ticket.objects.filter(pk=child.pk, status="open").exists())


    @override_settings(VK_CLICK_ANSWERS_VERIFIED=True)
    def test_manager_reply_issues_button_and_click_cancels_waiting_reply(self):
        client, profile = signed_in("102")
        grant_support_membership(profile.pk, self.reader.cursor_id)
        self.ticket.current_owner = profile
        self.ticket.save(update_fields=["current_owner"])
        url = f"/support/tickets/{self.ticket.pk}/send/"

        def queue_reply():
            response = client.post(url, {
                "client_operation_id": str(uuid4()), "expected_assignment_version": 1,
                "text": "Ответ менеджера", "source_draft": None,
            }, content_type="application/json")
            self.assertEqual(response.status_code, 202)

        queue_reply()
        sender = Mock(return_value=666)
        self.assertTrue(run_sender_once(17, sender))
        keyboard = sender.call_args.args[3]
        payload = keyboard["buttons"][0][0]["action"]["payload"]
        self.assertEqual(OutgoingResponse.objects.get().vk_keyboard, keyboard)
        queue_reply()
        self.receive(self.click(payload, "from_reply"))
        self.assertFalse(run_sender_once(17, sender))
        self.assertEqual(sender.call_count, 1)
        self.assertTrue(OutgoingResponse.objects.filter(status="failed", failure_reason="ticket_closed").exists())
        self.assertTrue(OutgoingResponse.objects.filter(status="sent", provider_message_id=666).exists())
