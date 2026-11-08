import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from django.db import connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from accounts.services import AccessError
from jobs.models import BackgroundTask as Task
from jobs.services import LeaseLost, claim
from knowledge.search import SearchHit
from support.ai_delivery import TASK_TYPE as SEND_TASK, admit, complete, schedule_part
from support.ai_generation import GeneratedQuote, generate_quote, save_answer
from support.ai_processing import run_lookup_once
from support.ai_reconciliation import delivery_state, request_reconciliation
from support.handoff_plans import (
    HANDOFF_BUDGET, TASK_TYPE, bind_answer, ensure_handoff, evaluate_plan, lock_dialog,
    refresh_dialog_plans, run_handoffs_once,
)
from support.models import (
    AIAnswer, AIAnswerInput, AIAnswerPart, AIAnswerPartInput, AIPartAttempt, AIPartDelivery,
    AIProcessingInput, AIProcessingInputMessage, AIProcessingRun, AIRequest, AIRequestMessage,
    Dialog, HandoffPlan, HandoffPlanAnswer, InboundRoute, Message, Ticket,
)
from support.tests.test_http import signed_in
from support.services import grant_support_membership
from vk_channel.models import LongPollCursor
from vk_channel.services import commit_batch
from vk_channel.tests import test_channel as channel_fixtures

incoming = channel_fixtures.incoming


@override_settings(DEBUG=True, SUPPORT_AI_LOOKUP_ENABLED=True, SUPPORT_AI_HANDOFF_ENABLED=True)
class HandoffPlanTests(TransactionTestCase):
    route = channel_fixtures.AILookupTests.route

    def setUp(self):
        self.cursor = LongPollCursor.objects.create(community_id=17, position="100")
        self.dialog = Dialog.objects.create(cursor=self.cursor, vk_peer_id=41)
        self.now = timezone.now()

    def request(self, *, started=None, text="Где зал?"):
        sequence = Message.objects.count() + 1
        message = Message.objects.create(dialog=self.dialog, text=text, sender_vk_id=41,
                                          conversation_message_id=sequence)
        InboundRoute.objects.create(dialog=self.dialog, message=message, sequence=sequence,
                                    mode_version=1, automation_epoch=1, status="ai_processing")
        request = AIRequest.objects.create(dialog=self.dialog, mode_version=1, automation_epoch=1,
            request_version_started_at=started or self.now, normalized_query=text, created_at=self.now)
        AIRequestMessage.objects.create(request=request, message=message)
        return request

    def make_run(self, requests):
        operation = uuid4()
        task = Task.objects.create(task_type="support.ai_lookup", idempotency_key=operation)
        run = AIProcessingRun.objects.create(dialog=self.dialog, task=task, operation_id=operation,
            mode_version=1, automation_epoch=1, created_at=self.now,
            processing_deadline_at=self.now + HANDOFF_BUDGET)
        for request in requests:
            snapshot = AIProcessingInput.objects.create(run=run, request=request,
                request_version=request.request_version, question_text=request.normalized_query)
            AIProcessingInputMessage.objects.bulk_create([
                AIProcessingInputMessage(processing_input=snapshot, message_id=pk)
                for pk in request.message_links.values_list("message_id", flat=True)
            ])
        return run

    def answer(self, run, *, count=2):
        with transaction.atomic():
            lock_dialog(run.dialog_id)
            run.refresh_from_db()
            answer = AIAnswer.objects.create(dialog=self.dialog, generation_operation_id=run.operation_id,
                mode_version_snapshot=1, model_snapshot={}, retrieval_snapshot={}, timings_snapshot={},
                content_sha256="a" * 64, created_at=self.now)
            inputs = [AIAnswerInput.objects.create(answer=answer, request_id=item.request_id,
                request_version_snapshot=item.request_version, query_snapshot=item.question_text,
                sources_snapshot=[{"fragment_id": 1, "material_id": 1, "publication_version": 1,
                                   "prepared_snapshot_id": 1}]) for item in run.inputs.all()]
            for position in range(1, count + 1):
                part = AIAnswerPart.objects.create(answer=answer, position=position, text_snapshot=f"Часть {position}")
                for item in inputs:
                    AIAnswerPartInput.objects.create(part=part, input=item)
                AIPartDelivery.objects.create(part=part, created_at=self.now, updated_at=self.now)
            run.status, run.reason, run.answer, run.finished_at = "answer_saved", "", answer, self.now
            run.save(update_fields=["status", "reason", "answer", "finished_at"])
            bind_answer(run)
        return answer

    def send_start(self, answer):
        with patch("support.ai_delivery.sources_are_current", return_value=True):
            self.assertTrue(schedule_part(answer.pk, 17, 41))
            task = claim([SEND_TASK])
            self.assertIsNotNone(admit(task, 17, 41))
        return task

    def test_common_deadline_and_idempotent_snapshot(self):
        first = self.request(started=self.now - timedelta(seconds=10))
        second = self.request(started=self.now - timedelta(seconds=3), text="Когда турнир?")
        run = self.make_run([second, first])
        plan = ensure_handoff(run)
        self.assertEqual(plan.handoff_deadline_at, first.request_version_started_at + HANDOFF_BUDGET)
        self.assertEqual([item["request_id"] for item in plan.inputs_snapshot], [first.pk, second.pk])
        self.assertEqual(ensure_handoff(run).pk, plan.pk)
        self.assertEqual(HandoffPlan.objects.count(), 1)
        self.assertEqual(Task.objects.filter(task_type=TASK_TYPE).count(), 1)
        independent = self.request(text="Как записаться?")
        self.make_run([independent])
        with transaction.atomic():
            cursor, dialog = lock_dialog(self.dialog.pk)
            refresh_dialog_plans(cursor, dialog)
        plan.refresh_from_db()
        self.assertEqual(plan.timer_revision, 1)
        self.assertEqual(len(plan.inputs_snapshot), 2)

    def test_refinement_revises_timer_and_old_revision_does_not_transfer(self):
        first = self.request(started=self.now - timedelta(seconds=15))
        second = self.request(started=self.now - timedelta(seconds=5))
        plan = ensure_handoff(self.make_run([first, second]))
        original = plan.inputs_snapshot
        AIRequest.objects.filter(pk=first.pk).update(request_version=2, request_version_started_at=self.now)
        evaluate_plan(plan.pk, revision=1)
        plan.refresh_from_db()
        self.assertEqual((plan.status, plan.timer_revision), ("pending", 2))
        self.assertEqual(plan.handoff_deadline_at, second.request_version_started_at + HANDOFF_BUDGET)
        self.assertEqual(plan.inputs_snapshot, original)
        with patch("support.handoff_plans.database_time", return_value=plan.handoff_deadline_at):
            self.assertIsNone(evaluate_plan(plan.pk, revision=1))
            self.assertFalse(Ticket.objects.exists())
            ticket = evaluate_plan(plan.pk, revision=2)
        self.assertIsNotNone(ticket)
        self.assertEqual(len(ticket.handoff_plans.get().work_snapshot), 2)

    def test_only_outdated_cause_supersedes_the_plan(self):
        first, second = self.request(), self.request()
        plan = ensure_handoff(self.make_run([first, second]), remaining_work={first.pk: "Уточнить адрес"})
        AIRequest.objects.filter(pk=first.pk).update(request_version=2)
        evaluate_plan(plan.pk)
        plan.refresh_from_db()
        self.assertEqual(plan.status, "superseded")
        self.assertFalse(Ticket.objects.exists())

    def test_real_refinement_refreshes_saved_plan(self):
        self.route(incoming(text="Где зал?"))
        run = AIProcessingRun.objects.get()
        plan = ensure_handoff(run)
        self.route(incoming(cmid=8, text="Для баскетбола", reply_message={"conversation_message_id": 7}))
        plan.refresh_from_db()
        self.assertEqual(plan.status, "superseded")
        self.assertEqual(AIRequest.objects.get().request_version, 2)
        self.assertFalse(Ticket.objects.exists())

    def test_lookup_without_basis_transfers_current_questions_and_is_replayable_after_close(self):
        self.route(incoming(text="Где зал?"), incoming(cmid=8, text="Когда турнир?"))
        self.assertEqual(AIRequest.objects.count(), 2, list(InboundRoute.objects.values("status", "reason")))
        self.assertTrue(run_lookup_once(search=Mock(return_value=[])))
        plan = HandoffPlan.objects.get()
        ticket = Ticket.objects.get()
        self.assertEqual((plan.status, plan.ticket_id), ("transferred", ticket.pk))
        self.assertEqual(ticket.message_set.count(), 2, {
            "requests": list(AIRequest.objects.values("pk", "status", "mode_version", "automation_epoch")),
            "routes": list(InboundRoute.objects.values("status", "reason", "message__ticket_id")),
        })
        self.assertEqual(len(plan.work_snapshot), 2)
        self.assertEqual(AIRequest.objects.filter(status="escalated_to_human").count(), 2)
        self.assertFalse(run_lookup_once(search=Mock()))
        Ticket.objects.filter(pk=ticket.pk).update(status="closed", closed_at=self.now)
        Dialog.objects.filter(pk=self.dialog.pk).update(mode="ai_auto", mode_version=3)
        self.assertEqual(evaluate_plan(plan.pk).pk, ticket.pk)
        self.assertEqual(ensure_handoff(plan.runs.get()).ticket_id, ticket.pk)
        self.assertEqual(Ticket.objects.count(), 1)

    def test_temporary_search_outage_and_disabled_flag_do_not_establish_cause(self):
        self.route(incoming())
        run_lookup_once(search=Mock(side_effect=OSError("offline")))
        self.assertFalse(HandoffPlan.objects.exists())
        self.route(incoming(cmid=8, text="Где зал?"))
        with override_settings(SUPPORT_AI_HANDOFF_ENABLED=False):
            run_lookup_once(search=Mock(return_value=[]))
        self.assertFalse(HandoffPlan.objects.exists())
        self.assertFalse(Ticket.objects.exists())

    @override_settings(SUPPORT_AI_GENERATION_ENABLED=True)
    def test_incomplete_model_response_does_not_establish_cause_but_completed_empty_decision_does(self):
        hit = SearchHit(1, 2, 3, 4, "Зал", "Зал на втором этаже.", 0.01)
        for cmid, finish_reason in ((7, "length"), (8, "stop")):
            self.route(incoming(cmid=cmid, text="Где зал?"))
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "choices": [{"finish_reason": finish_reason, "message": {"content": "{}"}}],
            }).encode()
            opener = Mock()
            opener.open.return_value = response
            with patch("support.ai_generation.local_endpoint", return_value="http://localhost:1234/v1/chat/completions"):
                with patch("support.ai_generation.build_opener", return_value=opener):
                    with patch("support.ai_processing.sources_are_current", return_value=True):
                        run_lookup_once(search=Mock(return_value=[hit]), generate=generate_quote)
            if finish_reason == "length":
                self.assertEqual(AIProcessingRun.objects.get().reason, "model_unavailable")
                self.assertFalse(HandoffPlan.objects.exists())
        self.assertEqual(HandoffPlan.objects.get().status, "transferred")

    def test_already_expired_plan_transfers_in_creation_transaction(self):
        run = self.make_run([self.request(started=self.now - timedelta(seconds=40))])
        plan = ensure_handoff(run)
        self.assertEqual(plan.status, "transferred")
        self.assertIsNotNone(plan.ticket_id)
        self.assertEqual(AIProcessingRun.objects.get(pk=run.pk).reason, "superseded")

    def test_established_absence_at_processing_deadline_transfers_instead_of_losing_the_cause(self):
        self.route(incoming(text="Где зал?"))
        run = AIProcessingRun.objects.get()
        clock = Mock(return_value=self.now)

        def search(question):
            clock.return_value = run.processing_deadline_at + timedelta(seconds=1)
            return []

        with patch("support.ai_processing.database_time", clock):
            with patch("support.handoff_plans.database_time", clock):
                run_lookup_once(search=search)
        plan = HandoffPlan.objects.get()
        self.assertEqual(plan.status, "transferred")
        run.refresh_from_db()
        self.assertEqual(run.reason, "no_reliable_basis")

    def test_answer_parts_finish_before_deadline_and_only_residue_is_handed_off(self):
        run = self.make_run([self.request()])
        plan = ensure_handoff(run)
        answer = self.answer(run)
        first = self.send_start(answer)
        complete(first, "sent", provider_id=100)
        self.assertFalse(Ticket.objects.exists())
        second = self.send_start(answer)
        complete(second, "sent", provider_id=101)
        plan.refresh_from_db()
        self.assertEqual(plan.status, "transferred")
        self.assertEqual([item["kind"] for item in plan.work_snapshot], ["question"])
        self.assertEqual(Message.objects.filter(ticket_id=plan.ticket_id, direction="outbound_bot").count(), 2)
        self.assertEqual(Message.objects.filter(ticket_id=plan.ticket_id, direction="inbound").count(), 1)
        self.assertEqual(AIRequest.objects.get().status, "escalated_to_human")
        self.assertEqual(HandoffPlanAnswer.objects.get().required_part_ids,
                         list(answer.parts.order_by("position").values_list("pk", flat=True)))

    def test_unknown_part_adds_unconfirmed_fragment_and_retains_successful_part(self):
        run = self.make_run([self.request()])
        plan = ensure_handoff(run)
        answer = self.answer(run, count=3)
        complete(self.send_start(answer), "sent", provider_id=100)
        complete(self.send_start(answer), "unknown_outcome", reason="vk_confirmation_lost")
        plan.refresh_from_db()
        self.assertEqual(plan.status, "transferred")
        self.assertEqual(list(AIPartDelivery.objects.order_by("part__position").values_list("status", flat=True)),
                         ["sent", "unknown_outcome", "cancelled"])
        self.assertEqual([item["status"] for item in plan.work_snapshot if item["kind"] == "unconfirmed_part"],
                         ["unknown_outcome", "cancelled"])

    def test_inflight_confirmation_stays_with_original_closed_card(self):
        run = self.make_run([self.request()])
        plan = ensure_handoff(run)
        answer = self.answer(run)
        task = self.send_start(answer)
        with patch("support.handoff_plans.database_time", return_value=plan.handoff_deadline_at):
            ticket = evaluate_plan(plan.pk)
        plan.refresh_from_db()
        frozen = plan.work_snapshot
        Ticket.objects.filter(pk=ticket.pk).update(status="closed", closed_at=self.now)
        Dialog.objects.filter(pk=self.dialog.pk).update(mode="human_support", mode_version=4)
        other = Ticket.objects.create(dialog=self.dialog)
        complete(task, "sent", provider_id=123)
        self.assertEqual(Message.objects.get(direction="outbound_bot").ticket_id, ticket.pk)
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).status, "closed")
        self.assertFalse(Message.objects.filter(ticket=other).exists())
        plan.refresh_from_db()
        self.assertEqual(plan.work_snapshot, frozen)
        self.assertEqual(AIPartDelivery.objects.filter(handoff_ticket=ticket).count(), 2)

    def test_deadline_blocks_admission_and_cancels_next_part(self):
        run = self.make_run([self.request()])
        plan = ensure_handoff(run)
        answer = self.answer(run)
        with patch("support.ai_delivery.sources_are_current", return_value=True):
            schedule_part(answer.pk, 17, 41)
            task = claim([SEND_TASK])
            with patch("support.handoff_plans.database_time", return_value=plan.handoff_deadline_at):
                self.assertIsNone(admit(task, 17, 41))
        self.assertTrue(Ticket.objects.exists())
        self.assertFalse(AIPartAttempt.objects.filter(started_at__isnull=False).exists())
        self.assertEqual(AIPartDelivery.objects.filter(status="cancelled").count(), 2)

    def test_explicit_request_between_scheduling_and_admission_transfers_immediately(self):
        self.route(incoming(text="Где зал?"))
        run = AIProcessingRun.objects.get()
        plan = ensure_handoff(run)
        answer = self.answer(run)
        with patch("support.ai_delivery.sources_are_current", return_value=True):
            schedule_part(answer.pk, 17, 41)
        self.route(incoming(cmid=8, text="Позвать менеджера"))
        plan.refresh_from_db()
        self.assertEqual(plan.status, "transferred")
        self.assertEqual(plan.ticket.message_set.filter(direction="inbound").count(), 2)
        self.assertEqual(plan.ticket.handoff_reason, "student_requested_manager")
        self.assertEqual(Dialog.objects.get(pk=self.dialog.pk).mode_version, 2)
        self.assertIsNone(claim([SEND_TASK]))

    def test_late_answer_is_bound_and_cancelled_without_dispatch(self):
        run = self.make_run([self.request(started=self.now - timedelta(seconds=40))])
        plan = ensure_handoff(run)
        hit = SearchHit(1, 2, 3, 4, "Зал", "Зал на втором этаже.", 0.01)
        with transaction.atomic():
            lock_dialog(run.dialog_id)
            run.refresh_from_db()
            save_answer(run, GeneratedQuote(hit.content, hit.fragment_id, {}, {}), [hit], self.now, 1)
        self.assertEqual(AIPartDelivery.objects.filter(status="cancelled", handoff_ticket_id=plan.ticket_id).count(), 1)
        self.assertFalse(AIPartAttempt.objects.exists())

    def test_handoff_includes_a_persisted_message_waiting_for_routing(self):
        self.route(incoming(text="Где зал?"))
        plan = ensure_handoff(AIProcessingRun.objects.get())
        self.reader = commit_batch(self.reader, str(self.reader.receipt_version + 2), [incoming(cmid=8, text="Когда турнир?")])
        with patch("support.handoff_plans.database_time", return_value=plan.handoff_deadline_at):
            ticket = evaluate_plan(plan.pk)
        self.assertEqual(ticket.message_set.count(), 2)
        self.assertEqual(InboundRoute.objects.get(message__conversation_message_id=8).status, "attached")
        plan.refresh_from_db()
        self.assertEqual(len(plan.work_snapshot), 2)
        self.assertTrue(any(item.get("remaining_work") == "Сообщение ожидает разбора сотрудника"
                            for item in plan.work_snapshot))

    def test_overdue_sweep_covers_lost_timer_and_quarantine(self):
        plan = ensure_handoff(self.make_run([self.request()]))
        Task.objects.filter(task_type=TASK_TYPE).update(status=Task.Status.DEAD)
        HandoffPlan.objects.filter(pk=plan.pk).update(handoff_deadline_at=self.now - timedelta(seconds=1))
        # Keep the immutable inputs and recomputed deadline consistent with the old clock.
        with patch("support.handoff_plans.database_time", return_value=plan.handoff_deadline_at):
            with override_settings(STAFF_QUARANTINE_INCIDENT="handoff-test"):
                self.assertFalse(run_handoffs_once())
            self.assertTrue(run_handoffs_once())
        plan.refresh_from_db()
        self.assertEqual(plan.status, "transferred")

    def test_parallel_evaluators_reuse_one_card(self):
        plan = ensure_handoff(self.make_run([self.request()]))
        barrier = Barrier(2)

        def evaluate():
            try:
                barrier.wait(timeout=5)
                return evaluate_plan(plan.pk).pk
            finally:
                connections.close_all()

        with patch("support.handoff_plans.database_time", return_value=plan.handoff_deadline_at):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(evaluate) for _ in range(2)]
                self.assertEqual([future.result(timeout=15) for future in futures], [Ticket.objects.get().pk] * 2)
        self.assertEqual(Ticket.objects.count(), 1)

    def test_enqueue_failure_rolls_back_plan_and_run_link(self):
        run = self.make_run([self.request()])
        with patch("support.handoff_plans.enqueue", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                ensure_handoff(run)
        self.assertFalse(HandoffPlan.objects.exists())
        run.refresh_from_db()
        self.assertIsNone(run.handoff_plan_id)

    def test_refresh_transfers_multiple_pending_plans_once(self):
        first = ensure_handoff(self.make_run([self.request()]))
        second = ensure_handoff(self.make_run([self.request()]))
        with transaction.atomic():
            cursor, dialog = lock_dialog(self.dialog.pk)
            with patch("support.handoff_plans.database_time", return_value=first.handoff_deadline_at):
                refresh_dialog_plans(cursor, dialog)
        self.assertEqual(HandoffPlan.objects.filter(status="transferred").count(), 2)
        self.assertEqual(Ticket.objects.count(), 1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.ticket_id, second.ticket_id)

    def test_restored_parts_remain_unknown_when_plan_is_transferred(self):
        from support.ai_delivery import isolate_restored_bot_deliveries

        run = self.make_run([self.request()])
        plan = ensure_handoff(run)
        answer = self.answer(run)
        with override_settings(STAFF_QUARANTINE_INCIDENT="handoff-restore"):
            isolate_restored_bot_deliveries(uuid4())
        evaluate_plan(plan.pk)
        plan.refresh_from_db()
        self.assertEqual(plan.status, "transferred")
        self.assertEqual(AIPartDelivery.objects.filter(status="unknown_outcome", handoff_ticket_id=plan.ticket_id).count(), 2)
        self.assertFalse(AIPartAttempt.objects.exists())
        self.assertFalse(schedule_part(answer.pk, 17, 41))

    def test_reconciliation_permissions_follow_the_delivery_card(self):
        run = self.make_run([self.request()])
        plan = ensure_handoff(run)
        answer = self.answer(run, count=1)
        complete(self.send_start(answer), "unknown_outcome", reason="vk_confirmation_lost")
        plan.refresh_from_db()
        client, actor = signed_in("handoff-reviewer")
        _, owner = signed_in("handoff-owner")
        grant_support_membership(actor.pk, self.cursor.pk)
        grant_support_membership(owner.pk, self.cursor.pk)
        Ticket.objects.filter(pk=plan.ticket_id).update(current_owner=owner)
        AIProcessingRun.objects.filter(pk=run.pk).update(handoff_ticket=None)
        delivery = AIPartDelivery.objects.get(part__answer=answer)
        self.assertFalse(delivery_state(actor, delivery)["can_reconcile"])
        with self.assertRaises(AccessError) as caught:
            request_reconciliation(client.session, delivery.part_id, uuid4())
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_lost_timer_lease_rolls_back_transfer(self):
        plan = ensure_handoff(self.make_run([self.request()]))
        Task.objects.filter(task_type=TASK_TYPE).update(scheduled_at=self.now - timedelta(seconds=1))
        with patch("support.handoff_plans.database_time", return_value=plan.handoff_deadline_at):
            with patch("support.handoff_plans.finish", side_effect=LeaseLost("expired")):
                self.assertTrue(run_handoffs_once())
        plan.refresh_from_db()
        self.assertEqual(plan.status, "pending")
        self.assertFalse(Ticket.objects.exists())
        self.assertEqual(Dialog.objects.get(pk=self.dialog.pk).mode, "ai_auto")

    def test_work_snapshot_is_visible_only_to_allowed_staff(self):
        plan = ensure_handoff(self.make_run([self.request(started=self.now - timedelta(seconds=40), text="<script>Вопрос</script>")]))
        client, profile = signed_in("handoff-reader")
        url = f"/support/tickets/{plan.ticket_id}/"
        self.assertEqual(client.get(url).status_code, 403)
        grant_support_membership(profile.pk, self.cursor.pk)
        page = client.get(url)
        self.assertContains(page, "Переданная работа")
        self.assertContains(page, "&lt;script&gt;Вопрос&lt;/script&gt;")
        self.assertNotContains(page, "<script>Вопрос</script>")
