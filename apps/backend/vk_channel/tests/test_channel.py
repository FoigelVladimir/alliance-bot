import json
from datetime import datetime, timedelta, timezone as utc_timezone
from unittest.mock import Mock, call, patch
from uuid import uuid4

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from jobs.models import BackgroundTask
from jobs.services import claim
from jobs.worker import run_one
from knowledge.search import SearchHit
from support.ai_delivery import run_bot_sender_once, schedule_part
from support.ai_generation import parse_quote
from support.ai_processing import TASK_TYPE as AI_TASK_TYPE, run_lookup_once
from support.delivery import run_sender_once
from support.models import (
    AIAnswer, AIAnswerPart, AIPartAttempt, AIPartDelivery, AIProcessingRun, AIRequest, Attachment, Dialog, InboundRoute, Message, OutgoingResponse, Ticket, VKRandomReservation,
)

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
    def test_explicit_request_creates_one_card_with_following_messages(self):
        reader = claim_reader(17, "001")
        request = incoming(text="  Позовите, пожалуйста, менеджера! ")
        ambiguous = incoming(cmid=6, text="Не надо звать менеджера, у меня другой вопрос")
        followup = incoming(cmid=8, text="Вот детали")
        reader = commit_batch(reader, "002", [ambiguous, request, followup, request])
        # A different worker may already have routed the follow-up before the request runs.
        InboundRoute.objects.filter(message__conversation_message_id=8).update(status="needs_review", reason="manual_support")
        while run_one({TASK_TYPE: route_inbound_task}):
            pass
        card = Ticket.objects.get()
        self.assertEqual(card.handoff_reason, "student_requested_manager")
        self.assertEqual(card.handoff_route.message.conversation_message_id, 7)
        self.assertEqual(card.dialog.mode, "human_support")
        self.assertEqual(set(Message.objects.filter(ticket=card).values_list("conversation_message_id", flat=True)), {7, 8})
        self.assertEqual(InboundRoute.objects.get(message__conversation_message_id=6).status, "needs_review")
        commit_batch(reader, "003", [incoming(cmid=9, text="Позвать менеджера")])
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Message.objects.get(conversation_message_id=9).ticket_id, card.pk)

    def test_historical_request_and_plain_thanks_after_close(self):
        reader = claim_reader(17, "001")
        dialog = Dialog.objects.create(cursor_id=reader.cursor_id, vk_peer_id=41)
        Ticket.objects.create(dialog=dialog, status=Ticket.Status.CLOSED, closed_at=timezone.now())
        reader = commit_batch(reader, "002", [
            incoming(text="Спасибо!"), incoming(cmid=8, text="Спасибо, а когда турнир?"),
        ])
        while run_one({TASK_TYPE: route_inbound_task}):
            pass
        self.assertEqual(InboundRoute.objects.get(message__conversation_message_id=7).status, "ignored")
        self.assertEqual(InboundRoute.objects.get(message__conversation_message_id=8).status, "needs_review")
        gap = record_gap(reader, "010", "position_lost")
        save_historical_batch(gap.pk, [incoming(cmid=9, text="Позвать менеджера")])
        while run_one({TASK_TYPE: route_inbound_task}):
            pass
        self.assertEqual(InboundRoute.objects.get(message__conversation_message_id=9).reason, "historical")
        self.assertFalse(Ticket.objects.filter(status=Ticket.Status.OPEN).exists())

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


@override_settings(SUPPORT_AI_LOOKUP_ENABLED=True)
class AILookupTests(TransactionTestCase):
    def route(self, *messages):
        reader = getattr(self, "reader", None) or claim_reader(17, "001")
        self.reader = commit_batch(reader, str(reader.receipt_version + 2), list(messages))
        while run_one({TASK_TYPE: route_inbound_task}):
            pass

    def test_reply_updates_only_its_question_and_keeps_the_old_snapshot(self):
        self.route(incoming(text="Как записаться?"), incoming(cmid=8, text="Когда турнир?"))
        old = AIProcessingRun.objects.get(inputs__question_text="Как записаться?")
        independent = AIProcessingRun.objects.get(inputs__question_text="Когда турнир?")
        lease = claim([AI_TASK_TYPE])
        self.assertEqual(lease.pk, old.task_id)
        self.route(incoming(cmid=9, text="На баскетбол", reply_message={"conversation_message_id": 7}))
        old.refresh_from_db()
        independent.refresh_from_db()
        self.assertEqual((old.reason, old.inputs.get().question_text), ("superseded", "Как записаться?"))
        self.assertEqual(independent.status, "running")
        self.assertEqual(old.inputs.get().messages.count(), 1)
        self.assertIsNone(BackgroundTask.objects.get(pk=lease.pk).lease_token)
        request = old.inputs.get().request
        request.refresh_from_db()
        self.assertEqual(request.request_version, 2)
        current = AIProcessingRun.objects.get(inputs__request=request, inputs__request_version=2)
        self.assertEqual(current.inputs.get().question_text, "Как записаться?\nНа баскетбол")
        self.assertEqual(current.inputs.get().messages.count(), 2)

        lookup = Mock(return_value=[])
        self.assertTrue(run_lookup_once(lookup))
        self.assertTrue(run_lookup_once(lookup))
        self.assertFalse(run_lookup_once(lookup))
        self.assertEqual(lookup.call_args_list, [call("Когда турнир?"), call("Как записаться?\nНа баскетбол")])
        self.assertEqual(InboundRoute.objects.filter(status="needs_review").count(), 3)
        self.assertEqual(AIProcessingRun.objects.filter(reason="no_reliable_basis").count(), 2)

    def test_handoff_revokes_running_lookup_and_keeps_its_card_link(self):
        self.route(incoming(text="Где зал?"))
        running = AIProcessingRun.objects.get()
        self.assertIsNotNone(claim([AI_TASK_TYPE]))
        self.route(incoming(cmid=8, text="Позвать менеджера"))
        card = Ticket.objects.get()
        running.refresh_from_db()
        self.assertEqual((running.reason, running.handoff_ticket_id), ("superseded", card.pk))
        self.assertEqual(AIRequest.objects.get().status, "escalated_to_human")
        self.assertEqual(Message.objects.filter(ticket=card).count(), 2)
        lookup = Mock()
        self.assertFalse(run_lookup_once(lookup))
        lookup.assert_not_called()

    def test_budget_exhaustion_finishes_task_and_processing_together(self):
        self.route(incoming())
        run = AIProcessingRun.objects.get()
        task = claim([AI_TASK_TYPE])
        BackgroundTask.objects.filter(pk=task.pk).update(
            locked_until=timezone.now() - timedelta(seconds=1), attempt_count=task.max_attempts,
        )
        lookup = Mock()
        self.assertFalse(run_lookup_once(lookup))
        lookup.assert_not_called()
        run.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual((run.status, run.reason), ("no_answer", "processing_budget_exhausted"))
        self.assertEqual(task.status, BackgroundTask.Status.DEAD)
        self.assertEqual(InboundRoute.objects.get().status, "needs_review")


    def test_journal_filters_use_moscow_day_and_hide_other_communities(self):
        self.route(incoming(text="Исходный <script>текст</script>"))
        self.assertTrue(run_lookup_once(Mock(return_value=[])))
        run = AIProcessingRun.objects.get()
        finished = datetime(2026, 11, 7, 22, tzinfo=utc_timezone.utc)
        AIProcessingRun.objects.filter(pk=run.pk).update(finished_at=finished)
        reader = claim_reader(18, "100")
        commit_batch(reader, "101", [incoming(community=18, text="Чужой вопрос")])
        while run_one({TASK_TYPE: route_inbound_task}):
            pass
        self.assertTrue(run_lookup_once(Mock(return_value=[])))
        foreign = AIProcessingRun.objects.exclude(pk=run.pk).get()
        AIProcessingRun.objects.filter(pk=foreign.pk).update(finished_at=finished)
        client, profile = signed_in("111")
        grant_support_membership(profile.pk, run.dialog.cursor_id)
        query = {"from": "2026-11-08", "to": "2026-11-08", "result": "no_answer"}
        full = client.get("/support/ai-journal/", query)
        fragment = client.get("/support/fragments/ai-journal/", query)
        self.assertIn(fragment.content.strip(), full.content)
        self.assertContains(full, "Исходный &lt;script&gt;текст&lt;/script&gt;")
        self.assertNotContains(full, "Чужой вопрос")
        self.assertIn("no-store", full.headers["Cache-Control"])
        self.assertNotContains(client.get("/support/ai-journal/", {"to": "2026-11-07"}), "Исходный")
        self.assertContains(client.get("/support/ai-journal/", {"from": "2026-02-30"}), "Период сброшен")
        self.assertContains(client.get(f"/support/ai-processing-runs/{run.pk}/"), "Исходный")
        self.assertEqual(client.get(f"/support/ai-processing-runs/{foreign.pk}/").status_code, 404)
        self.assertNotContains(client.get("/support/ai-journal/", {"delivery": "sent"}), "Исходный")


    @override_settings(SUPPORT_AI_GENERATION_ENABLED=True)
    def test_quote_is_saved_with_waiting_delivery_and_visible_in_journal(self):
        self.route(incoming(text="Где зал?"))
        hit = SearchHit(1, 2, 3, 4, "Расписание", "Зал находится на втором этаже.", 0.01)
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "fragment_id": hit.fragment_id, "quote": "Зал находится на втором этаже.",
        })}}]}
        quote = parse_quote(payload, [hit], 10)
        invented = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "fragment_id": hit.fragment_id, "quote": "Зал находится на первом этаже.",
        })}}]}
        self.assertIsNone(parse_quote(invented, [hit], 10))
        self.assertIsNotNone(quote)
        with patch("support.ai_processing.sources_are_current", return_value=True):
            self.assertTrue(run_lookup_once(Mock(return_value=[hit]), Mock(return_value=quote)))
        run = AIProcessingRun.objects.get()
        answer = AIAnswer.objects.get()
        self.assertEqual(run.status, "answer_saved")
        self.assertEqual(run.operation_id, answer.generation_operation_id)
        self.assertEqual(answer.inputs.get().sources_snapshot[0]["prepared_snapshot_id"], 4)
        self.assertEqual(answer.parts.get().text_snapshot, quote.text)
        self.assertEqual(AIPartDelivery.objects.get().status, "waiting")
        self.assertIsNone(answer.parts.get().message_id)
        self.assertEqual(Message.objects.count(), 1)
        client, profile = signed_in("112")
        grant_support_membership(profile.pk, run.dialog.cursor_id)
        self.assertContains(client.get("/support/ai-journal/", {"result": "answer", "delivery": "waiting"}), "Открыть ответ")
        self.assertContains(client.get(f"/support/ai-answers/{answer.pk}/"), "Ожидает отправки")

    @override_settings(SUPPORT_AI_GENERATION_ENABLED=True)
    def test_withdrawn_source_discards_generated_quote(self):
        self.route(incoming(text="Где зал?"))
        hit = SearchHit(1, 2, 3, 4, "Расписание", "Второй этаж.", 0.01)
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "fragment_id": hit.fragment_id, "quote": hit.content,
        })}}]}
        generator = Mock(return_value=parse_quote(payload, [hit], 10))
        with patch("support.ai_processing.sources_are_current", return_value=False):
            self.assertTrue(run_lookup_once(Mock(return_value=[hit]), generator))
        generator.assert_called_once()
        self.assertFalse(AIAnswer.objects.exists())
        self.assertEqual(AIProcessingRun.objects.get().reason, "sources_changed")


    @override_settings(SUPPORT_AI_GENERATION_ENABLED=True)
    def test_bot_delivery_stops_after_unknown_outcome(self):
        self.route(incoming(text="Где зал?"))
        hit = SearchHit(1, 2, 3, 4, "Расписание", "Зал на втором этаже.", 0.01)
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "fragment_id": hit.fragment_id, "quote": hit.content,
        })}}]}
        with patch("support.ai_processing.sources_are_current", return_value=True):
            run_lookup_once(Mock(return_value=[hit]), Mock(return_value=parse_quote(payload, [hit], 10)))
        answer = AIAnswer.objects.get()
        now = timezone.now()
        for position in (2, 3):
            part = AIAnswerPart.objects.create(answer=answer, position=position, text_snapshot=f"Часть {position}")
            AIPartDelivery.objects.create(part=part, created_at=now, updated_at=now)
        send = Mock(side_effect=[740, DeliveryUnknown()])
        with patch("support.ai_delivery.sources_are_current", return_value=True):
            self.assertTrue(schedule_part(answer.pk, 17, 41))
            self.assertFalse(schedule_part(answer.pk, 17, 41))
            self.assertTrue(run_bot_sender_once(17, 41, send))
            self.assertTrue(run_bot_sender_once(17, 41, send))
            self.assertFalse(run_bot_sender_once(17, 41, send))
        self.assertEqual(send.call_count, 2)
        self.assertEqual(VKRandomReservation.objects.count(), 2)
        self.assertEqual(list(AIPartDelivery.objects.order_by("part__position").values_list("status", flat=True)),
                         ["sent", "unknown_outcome", "cancelled"])
        self.assertEqual(AIPartAttempt.objects.count(), 2)
        self.assertEqual(Message.objects.filter(direction="outbound_bot").count(), 1)

    @override_settings(SUPPORT_AI_GENERATION_ENABLED=True)
    def test_bot_delivery_rechecks_source_before_admission(self):
        self.route(incoming(text="Где зал?"))
        hit = SearchHit(1, 2, 3, 4, "Расписание", "Второй этаж.", 0.01)
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "fragment_id": hit.fragment_id, "quote": hit.content,
        })}}]}
        with patch("support.ai_processing.sources_are_current", return_value=True):
            run_lookup_once(Mock(return_value=[hit]), Mock(return_value=parse_quote(payload, [hit], 10)))
        with patch("support.ai_delivery.sources_are_current", return_value=True):
            self.assertTrue(schedule_part(AIAnswer.objects.get().pk, 17, 41))
        send = Mock()
        with patch("support.ai_delivery.sources_are_current", return_value=False):
            self.assertTrue(run_bot_sender_once(17, 41, send))
        send.assert_not_called()
        delivery = AIPartDelivery.objects.get()
        self.assertEqual((delivery.status, delivery.reason_code), ("cancelled", "sources_changed"))
        self.assertIsNone(AIPartAttempt.objects.get().started_at)


    def prepare_one_bot_answer(self):
        self.route(incoming(text="Где зал?"))
        hit = SearchHit(1, 2, 3, 4, "Расписание", "Второй этаж.", 0.01)
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "fragment_id": hit.fragment_id, "quote": hit.content,
        })}}]}
        with patch("support.ai_processing.sources_are_current", return_value=True):
            run_lookup_once(Mock(return_value=[hit]), Mock(return_value=parse_quote(payload, [hit], 10)))
        return AIAnswer.objects.get()

    @override_settings(SUPPORT_AI_GENERATION_ENABLED=True)
    def test_late_bot_success_links_only_to_handoff_ticket(self):
        self.prepare_one_bot_answer()

        def send(*_):
            self.route(incoming(cmid=8, text="Позвать менеджера"))
            self.assertEqual(AIPartDelivery.objects.get().status, "pending")
            return 741

        with patch("support.ai_delivery.sources_are_current", return_value=True):
            self.assertTrue(run_bot_sender_once(17, 41, send))
        ticket = Ticket.objects.get()
        self.assertEqual(Message.objects.get(direction="outbound_bot").ticket_id, ticket.pk)
        self.assertEqual(AIPartDelivery.objects.get().status, "sent")
        self.assertEqual(AIProcessingRun.objects.get().handoff_ticket_id, ticket.pk)

    @override_settings(SUPPORT_AI_GENERATION_ENABLED=True)
    def test_restored_waiting_and_pending_parts_become_unknown(self):
        answer = self.prepare_one_bot_answer()
        now = timezone.now()
        part = AIAnswerPart.objects.create(answer=answer, position=2, text_snapshot="Следующая часть")
        AIPartDelivery.objects.create(part=part, created_at=now, updated_at=now)
        with patch("support.ai_delivery.sources_are_current", return_value=True):
            self.assertTrue(schedule_part(answer.pk, 17, 41))
        with self.settings(STAFF_QUARANTINE_INCIDENT="restored-bot"):
            call_command("isolate_restored_bot_deliveries")
            call_command("isolate_restored_bot_deliveries")
        self.assertEqual(set(AIPartDelivery.objects.values_list("status", flat=True)), {"unknown_outcome"})
        self.assertEqual(AIPartAttempt.objects.count(), 1)
        attempt = AIPartAttempt.objects.get()
        self.assertEqual(attempt.task.status, BackgroundTask.Status.UNKNOWN)
        self.assertIsNone(attempt.started_at)
        self.assertIsNone(AIPartDelivery.objects.get(part=part).vk_random_id)
        send = Mock()
        self.assertFalse(run_bot_sender_once(17, 41, send))
        send.assert_not_called()


@override_settings(
    VK_GROUP_ID="207471704", VK_ACCESS_TOKEN="fake-token", VK_LONG_POLL_ENABLED=True,
    VK_LONG_POLL_PILOT_PEER_ID="508028954",
)
class PilotLongPollTests(TransactionTestCase):
    def test_mixed_batch_persists_only_pilot_dialog_and_advances_cursor(self):
        community, peer = 207471704, 508028954
        server = LongPollServer("https://lp.vk.ru", "100", "fake-key")
        updates = [
            incoming(peer=peer, community=community, text="Пилот"),
            incoming(cmid=8, peer=123456, community=community, text="Чужой диалог"),
            incoming(cmid=None, peer=123456, community=community),
            {"type": "message_event", "group_id": community, "object": {
                "peer_id": 123456, "user_id": 123456, "event_id": "outside",
                "payload": {"action_id": str(uuid4())},
            }},
            {"type": "message_new", "group_id": community, "object": {"message": {"text": "Без адресата"}}},
            None,
        ]
        module = "vk_channel.management.commands.run_vk_long_poll"
        with (patch(f"{module}.long_poll_server", return_value=server),
              patch(f"{module}.read_long_poll", return_value={"ts": "101", "updates": updates})):
            call_command("run_vk_long_poll", once=True)
        self.assertEqual(LongPollCursor.objects.get().position, "101")
        self.assertEqual(list(InboundEvent.objects.values_list("peer_id", "status")), [(peer, "accepted")])
        self.assertEqual(list(Message.objects.values_list("text", flat=True)), ["Пилот"])
        self.assertEqual(list(Dialog.objects.values_list("vk_peer_id", flat=True)), [peer])
        self.assertEqual(InboundRoute.objects.count(), 1)
        self.assertEqual(list(BackgroundTask.objects.values_list("task_type", flat=True)), [TASK_TYPE])
        self.assertFalse(StudentClick.objects.exists())

    def test_invalid_peer_fails_before_network(self):
        module = "vk_channel.management.commands.run_vk_long_poll"
        for peer in ("", "0", "-1", "abc", "9223372036854775808"):
            with (self.subTest(peer=peer), self.settings(VK_LONG_POLL_PILOT_PEER_ID=peer),
                  patch(f"{module}.long_poll_server") as discover, patch(f"{module}.read_long_poll") as read):
                with self.assertRaisesMessage(CommandError, "VK_LONG_POLL_PILOT_PEER_ID"):
                    call_command("run_vk_long_poll", once=True)
                discover.assert_not_called()
                read.assert_not_called()
        self.assertFalse(LongPollCursor.objects.exists())

    @override_settings(VK_GAP_HISTORY_VERIFIED=True)
    def test_pilot_gap_never_enqueues_community_backfill(self):
        reader = claim_reader(207471704, "100")
        server = LongPollServer("https://lp.vk.ru", "100", "fake-key")
        result, _ = poll_once(
            reader, server, Mock(return_value={"failed": 1, "ts": "200"}), Mock(),
            pilot_peer_id=508028954,
        )
        self.assertIsNone(result)
        self.assertEqual(LongPollCursor.objects.get().position, "200")
        self.assertEqual(LongPollGap.objects.get().coverage, {"reasons": ["pilot_dialog_scope"]})
        self.assertFalse(BackgroundTask.objects.filter(task_type=GAP_TASK).exists())
        with self.assertRaisesMessage(CommandError, "восполнение истории сообщества выключено"):
            call_command("run_vk_gap_history", once=True)
