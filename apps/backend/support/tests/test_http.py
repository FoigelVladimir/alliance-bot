from uuid import uuid4

from django.test import Client, TestCase, override_settings

from accounts.models import IdentityBinding, StaffProfile
from support.delivery import run_sender_once
from support.reconciliation import run_reconciliation_once
from vk_channel.client import DeliveryUnknown

from support.models import Dialog, InboundRoute, Message, ReviewDecision, SupportCommand, Ticket, TicketEvent
from support.services import grant_support_membership
from vk_channel.models import LongPollCursor


def signed_in(external_id, *, allowed=True, csrf=False):
    profile = StaffProfile.objects.create(
        role=StaffProfile.Role.MANAGER,
        access_state=StaffProfile.Access.GRANTED if allowed else StaffProfile.Access.NOT_GRANTED,
    )
    binding = IdentityBinding.objects.create(profile=profile, external_id=external_id)
    client = Client(enforce_csrf_checks=csrf)
    session = client.session
    session.update({"staff_profile": profile.pk, "staff_binding": binding.pk, "staff_epoch": profile.session_epoch})
    session.save()
    return client, profile


@override_settings(DEBUG=True)
class SupportHttpTests(TestCase):
    def setUp(self):
        self.cursor = LongPollCursor.objects.create(community_id=17, position="100")
        self.dialog = Dialog.objects.create(cursor=self.cursor, vk_peer_id=100)
        self.message = Message.objects.create(dialog=self.dialog, text="<script>test()</script> Когда тренировка?")
        self.route = InboundRoute.objects.create(
            message=self.message, dialog=self.dialog, sequence=1, mode_version=1,
            automation_epoch=1, status="needs_review",
        )
        self.client, self.profile = signed_in("101", csrf=True)
        grant_support_membership(self.profile.pk, self.cursor.pk)
        self.review_url = f"/support/review/{self.route.pk}/"

    def post(self, client, url, data):
        token = client.cookies.get("csrftoken")
        return client.post(url, data, content_type="application/json", HTTP_X_CSRFTOKEN=token.value if token else "")

    def test_review_to_ticket_take_and_replay(self):
        response = self.client.get("/support/")
        self.assertContains(response, "Требует разбора")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>test()</script>")
        self.assertContains(self.client.get("/"), "Поддержка")
        self.assertContains(self.client.get(self.review_url), "Передать в очередь")

        request = {"client_operation_id": str(uuid4()), "expected_ticket_id": None}
        response = self.post(self.client, self.review_url + "route/", request)
        self.assertEqual(response.status_code, 200)
        ticket_id = response.json()["ticket"]["id"]
        replay = self.post(self.client, self.review_url + "route/", request).json()
        self.assertTrue(replay["operation"]["replayed"])
        self.assertEqual(replay["ticket"]["id"], ticket_id)
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(ReviewDecision.objects.count(), 1)
        self.dialog.refresh_from_db()
        self.assertEqual((self.dialog.mode, self.dialog.mode_version), ("human_support", 2))

        url = f"/support/tickets/{ticket_id}/"
        page = self.client.get(url)
        self.assertContains(page, "Взять в работу")
        body = page.content.decode()
        self.assertLess(body.index('type="importmap"'), body.index("support/workspace.js"))
        take = {"client_operation_id": str(uuid4()), "expected_assignment_version": 1}
        first = self.post(self.client, url + "take/", take).json()
        self.assertEqual(first["ticket"]["current_owner_id"], str(self.profile.pk))
        self.assertEqual(first["ticket"]["assignment_version"], 2)
        self.assertTrue(self.post(self.client, url + "take/", take).json()["operation"]["replayed"])
        no_change = self.post(self.client, url + "take/", {
            "client_operation_id": str(uuid4()), "expected_assignment_version": 2,
        }).json()
        self.assertFalse(no_change["operation"]["changed"])
        self.assertEqual(TicketEvent.objects.filter(changed=True).count(), 1)
        self.assertContains(self.client.get("/support/?view=mine"), "Обращение №")
        self.assertEqual(self.client.head(url).content, b"")

    def test_membership_and_csrf_are_checked(self):
        outsider, _ = signed_in("102")
        self.assertEqual(outsider.get("/support/").status_code, 403)
        self.assertEqual(Client().get("/support/").status_code, 401)
        other = LongPollCursor.objects.create(community_id=18, position="100")
        outsider_profile = StaffProfile.objects.get(bindings__external_id="102")
        grant_support_membership(outsider_profile.pk, other.pk)
        self.assertEqual(outsider.get(self.review_url).status_code, 404)
        response = self.post(self.client, self.review_url + "route/", {
            "client_operation_id": str(uuid4()), "expected_ticket_id": None,
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_failed")
        self.assertFalse(SupportCommand.objects.exists())
        StaffProfile.objects.filter(pk=self.profile.pk).update(access_generation=2)
        self.assertEqual(self.client.get("/support/").status_code, 403)

    def test_stale_dialog_does_not_consume_uuid_and_dismissal_keeps_history(self):
        self.client.get(self.review_url)
        ticket = Ticket.objects.create(dialog=self.dialog)
        request = {"client_operation_id": str(uuid4()), "expected_ticket_id": None}
        response = self.post(self.client, self.review_url + "route/", request)
        self.assertEqual(response.json()["error"]["code"], "review_state_changed")
        self.assertFalse(SupportCommand.objects.exists())
        self.assertContains(self.client.get(self.review_url), "Добавить к обращению")
        response = self.post(self.client, self.review_url + "route/", {**request, "expected_ticket_id": str(ticket.pk)})
        self.assertEqual(response.status_code, 200)
        self.message.refresh_from_db()
        self.assertEqual(self.message.ticket_id, ticket.pk)
        conflict = self.post(self.client, f"/support/tickets/{ticket.pk}/take/", {
            "client_operation_id": request["client_operation_id"], "expected_assignment_version": 1,
        })
        self.assertEqual(conflict.json()["error"]["code"], "operation_mismatch")

        second = Message.objects.create(dialog=self.dialog, text="Спасибо")
        route = InboundRoute.objects.create(
            message=second, dialog=self.dialog, sequence=2, mode_version=1,
            automation_epoch=1, status="needs_review",
        )
        result = self.post(self.client, f"/support/review/{route.pk}/dismiss/", {
            "client_operation_id": str(uuid4()), "reason": "  Вопроса нет  ",
        })
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.json()["ticket"])
        self.assertEqual(ReviewDecision.objects.get(route=route).reason, "Вопроса нет")
        second.refresh_from_db()
        self.assertIsNone(second.ticket_id)

    def test_send_http_queues_and_replays_without_duplicate_attempt(self):
        ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)
        url = f"/support/tickets/{ticket.pk}/"
        self.assertContains(self.client.get(url), "Отправить в ВК")
        request = {
            "client_operation_id": str(uuid4()), "expected_assignment_version": 1,
            "text": "   ", "source_draft": None,
        }
        self.assertEqual(self.post(self.client, url + "send/", request).status_code, 422)
        request["text"] = "  Тестовый ответ  "
        first = self.post(self.client, url + "send/", request)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["response"]["status"], "pending")
        self.assertEqual(first.json()["attempt"]["status"], "in_flight")
        replay = self.post(self.client, url + "send/", request)
        self.assertEqual(replay.status_code, 202)
        self.assertTrue(replay.json()["operation"]["replayed"])
        self.assertEqual(first.json()["attempt"], replay.json()["attempt"])
        self.assertEqual(SupportCommand.objects.count(), 1)
        response_id = first.json()["response"]["id"]
        self.assertEqual(self.client.get(f"/support/responses/{response_id}/state/").json()["response"]["status"],
                         "pending")

    def test_unknown_delivery_exposes_reconciliation_action_and_result(self):
        ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)
        url = f"/support/tickets/{ticket.pk}/"
        self.client.get(url)
        request = {"client_operation_id": str(uuid4()), "expected_assignment_version": 1,
                   "text": "Ответ", "source_draft": None}
        response_id = self.post(self.client, url + "send/", request).json()["response"]["id"]

        def lose_confirmation(*parameters):
            raise DeliveryUnknown

        run_sender_once(17, lose_confirmation)
        page = self.client.get(url)
        self.assertContains(page, "Проверить историю ВК")
        self.assertNotContains(page, "Повторить отправку")
        reconcile_url = f"/support/responses/{response_id}/reconcile/"
        command = {"client_operation_id": str(uuid4())}
        queued = self.post(self.client, reconcile_url, command)
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(queued.json()["reconciliation"]["status"], "queued")
        run_reconciliation_once(17, lambda peer_id: [])
        finished = self.post(self.client, reconcile_url, command)
        self.assertEqual(finished.status_code, 200)
        self.assertTrue(finished.json()["operation"]["replayed"])
        state = self.client.get(f"/support/responses/{response_id}/state/").json()
        self.assertEqual(state["response"]["status"], "unknown_outcome")
        self.assertEqual(state["last_reconciliation"]["outcome"], "unresolved")
        self.assertEqual(state["last_attempt"]["status"], "unknown_outcome")
        self.assertIsNone(state["last_attempt"]["finished_at"])
        self.assertFalse(state["can_retry"])
        self.assertContains(self.client.get(url), "Проверка не нашла достаточного подтверждения.")

    def test_draft_versions_privacy_and_read_only_after_reassignment(self):
        ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)
        url = f"/support/tickets/{ticket.pk}/draft/"
        self.client.get(f"/support/tickets/{ticket.pk}/")
        self.assertEqual(self.client.get(url).json()["draft_revision"], "0")
        initial = {"expected_assignment_version": 1, "expected_draft_revision": "0", "text": "\nМой текст 🙂"}
        first = self.post(self.client, url, initial)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["draft"]["revision"], "1")
        self.assertIn("updated_at", first.json()["draft"])
        stale = self.post(self.client, url, {**initial, "text": "Старая вкладка"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["draft"]["text"], initial["text"])
        other, profile = signed_in("107")
        grant_support_membership(profile.pk, self.cursor.pk)
        self.assertIsNone(other.get(url).json()["draft"])
        self.assertEqual(other.get(url + "?author_id=1").status_code, 400)
        self.assertNotContains(other.get(f"/support/tickets/{ticket.pk}/"), "Мой текст")
        Ticket.objects.filter(pk=ticket.pk).update(current_owner=profile, assignment_version=2)
        rejected = self.post(self.client, url, {**initial, "expected_draft_revision": "1"})
        self.assertEqual(rejected.status_code, 409)
        self.assertNotIn("draft", rejected.json())
        own = self.client.get(url).json()
        self.assertFalse(own["editable"])
        self.assertEqual(own["draft"]["text"], initial["text"])
        self.assertContains(self.client.get(f"/support/tickets/{ticket.pk}/"), "Твой сохранённый черновик")

    def test_draft_can_exceed_send_limit_and_clearing_increases_revision(self):
        ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)
        url = f"/support/tickets/{ticket.pk}/draft/"
        self.client.get(f"/support/tickets/{ticket.pk}/")
        body = {"expected_assignment_version": 1, "expected_draft_revision": "0", "text": "🙂" * 10000}
        self.assertEqual(self.post(self.client, url, body).json()["draft"]["revision"], "1")
        self.assertEqual(self.post(self.client, url, {
            **body, "expected_draft_revision": "1", "text": "🙂" * 20001,
        }).status_code, 422)
        cleared = self.post(self.client, url, {**body, "expected_draft_revision": "1", "text": ""}).json()
        self.assertEqual((cleared["draft"]["text"], cleared["draft"]["revision"]), ("", "2"))
        self.assertEqual(self.post(self.client, url, {**body, "expected_draft_revision": "1"}).status_code, 409)

    def test_close_requires_fresh_signed_warning_and_replays_original_result(self):
        ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)
        self.dialog.mode = "human_support"
        self.dialog.save(update_fields=["mode"])
        page = f"/support/tickets/{ticket.pk}/"
        self.assertContains(self.client.get(page), "Закрыть обращение")
        draft_url = page + "draft/"
        draft = {"expected_assignment_version": 1, "expected_draft_revision": "0", "text": "Личный текст"}
        self.assertEqual(self.post(self.client, draft_url, draft).status_code, 200)
        request = {"client_operation_id": str(uuid4()), "expected_assignment_version": 1,
                   "confirm_warning": False, "warning_token": None}
        warning_response = self.post(self.client, page + "close/", request)
        self.assertEqual(warning_response.status_code, 409)
        warning = warning_response.json()["warning"]
        self.assertTrue(warning["has_uncommitted_draft"])
        self.assertNotContains(warning_response, "Личный текст", status_code=409)
        self.assertFalse(SupportCommand.objects.exists())
        confirmed = {**request, "client_operation_id": str(uuid4()), "confirm_warning": True,
                     "warning_token": warning["warning_token"] + "x"}
        self.assertEqual(self.post(self.client, page + "close/", confirmed).json()["error"]["code"], "warning_token_invalid")
        confirmed["warning_token"] = warning["warning_token"]
        self.post(self.client, draft_url, {**draft, "expected_draft_revision": "1", "text": "Новее"})
        stale = self.post(self.client, page + "close/", confirmed).json()
        self.assertEqual(stale["error"]["code"], "close_warning_stale")
        confirmed["warning_token"] = stale["warning"]["warning_token"]
        confirmed["client_operation_id"] = str(uuid4())
        closed = self.post(self.client, page + "close/", confirmed)
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(closed.json()["ticket"]["closed_reason"], "manager_forced_with_warning")
        replay = self.post(self.client, page + "close/", confirmed).json()
        self.assertTrue(replay["operation"]["replayed"])
        self.assertEqual(closed.json()["ticket"], replay["ticket"])
        self.assertEqual(TicketEvent.objects.count(), 1)
        self.assertNotIn("Новее", str(TicketEvent.objects.get().payload))
        self.assertEqual(self.client.get(draft_url).json()["draft"]["text"], "Новее")
        self.dialog.refresh_from_db()
        self.assertEqual((self.dialog.mode, self.dialog.mode_version), ("ai_auto", 2))

    def test_return_and_takeover_have_separate_permissions_and_replay(self):
        ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)
        page = f"/support/tickets/{ticket.pk}/"
        colleague, other = signed_in("109")
        grant_support_membership(other.pk, self.cursor.pk)
        self.assertContains(colleague.get(page), "Забрать себе")
        self.assertNotContains(colleague.get(page), "Вернуть в очередь")
        command = {"client_operation_id": str(uuid4()), "expected_assignment_version": 1}
        self.assertEqual(self.post(colleague, page + "return/", command).status_code, 403)
        taken = self.post(colleague, page + "takeover/", command).json()
        self.assertEqual(taken["ticket"]["current_owner_id"], str(other.pk))
        self.assertEqual(taken["ticket"]["importance"], "normal")
        self.assertTrue(self.post(colleague, page + "takeover/", command).json()["operation"]["replayed"])
        returned = self.post(colleague, page + "return/", {
            "client_operation_id": str(uuid4()), "expected_assignment_version": 2,
        }).json()
        self.assertIsNone(returned["ticket"]["current_owner_id"])
        self.assertEqual(returned["ticket"]["importance"], "high")
        self.assertEqual(self.post(colleague, page + "takeover/", command).json()["error"]["code"], "operation_state_changed")
        self.assertEqual(list(TicketEvent.objects.order_by("pk").values_list("previous_owner_id", "new_owner_id")),
                         [(self.profile.pk, other.pk), (other.pk, None)])

    def test_coordinator_targets_are_rechecked_and_replay_requires_permission(self):
        ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)
        page = f"/support/tickets/{ticket.pk}/"
        coordinator, coord = signed_in("111")
        grant_support_membership(coord.pk, self.cursor.pk, coordination=True)
        _, target = signed_in("112")
        grant_support_membership(target.pk, self.cursor.pk)
        signed_in("113")
        self.assertEqual(self.client.get(page + "reassign-targets/").status_code, 403)
        self.assertEqual(coordinator.get(page + "reassign-targets/").json()["targets"],
                         [{"id": str(target.pk), "display_name": ""}])
        self.assertContains(coordinator.get(page), "Передать другому менеджеру")
        data = {"client_operation_id": str(uuid4()), "expected_assignment_version": 1, "target_owner_id": str(target.pk)}
        StaffProfile.objects.filter(pk=target.pk).update(blocked_at=self.message.created_at)
        self.assertEqual(self.post(coordinator, page + "reassign/", data).json()["error"]["code"],
                         "target_owner_unavailable")
        self.assertFalse(SupportCommand.objects.exists())
        StaffProfile.objects.filter(pk=target.pk).update(blocked_at=None)
        self.assertEqual(self.post(coordinator, page + "reassign/", {
            **data, "target_owner_id": str(coord.pk),
        }).status_code, 422)
        result = self.post(coordinator, page + "reassign/", data).json()
        self.assertEqual(result["ticket"]["current_owner_id"], str(target.pk))
        self.assertTrue(self.post(coordinator, page + "reassign/", data).json()["operation"]["replayed"])
        grant_support_membership(coord.pk, self.cursor.pk, coordination=False)
        self.assertEqual(self.post(coordinator, page + "reassign/", data).status_code, 403)
        self.assertEqual(TicketEvent.objects.count(), 1)

    def test_history_cursor_is_strict_and_bound_to_ticket(self):
        ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)
        other = Ticket.objects.create(
            dialog=self.dialog, status=Ticket.Status.CLOSED, closed_at=self.message.created_at,
        )
        Message.objects.bulk_create([
            Message(dialog=self.dialog, ticket=ticket, text=f"Строка №{number:03}", created_at=self.message.created_at)
            for number in range(52)
        ])
        url = f"/support/tickets/{ticket.pk}/fragments/history/"
        latest = self.client.get(url)
        self.assertContains(latest, 'data-history-key=', count=50)
        self.assertNotContains(latest, "Строка №001")
        self.assertContains(latest, "Строка №051")
        before = latest.context["earlier_history"]
        older = self.client.get(url, {"before": before})
        self.assertContains(older, 'data-history-key=', count=2)
        self.assertContains(older, "Строка №001")
        self.assertNotContains(older, "Строка №002")
        self.assertEqual(self.client.get(url, {"before": before + "x"}).json()["error"]["code"], "invalid_cursor")
        other_url = f"/support/tickets/{other.pk}/fragments/history/"
        self.assertEqual(self.client.get(other_url, {"before": before}).json()["error"]["code"], "invalid_cursor")

    def test_history_renders_confirmed_delivery_once_and_state_matches_access(self):
        ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)
        page = f"/support/tickets/{ticket.pk}/"
        self.client.get(page)
        data = {"client_operation_id": str(uuid4()), "expected_assignment_version": 1,
                "text": "Уникальный ответ", "source_draft": None}
        self.assertEqual(self.post(self.client, page + "send/", data).status_code, 202)
        self.assertContains(self.client.get(page + "fragments/history/"), "Уникальный ответ", count=1)
        run_sender_once(17, lambda *parameters: 666)
        history = self.client.get(page + "fragments/history/")
        self.assertContains(history, "Уникальный ответ", count=1)
        self.assertContains(history, "Приём ВК подтверждён")
        state = self.client.get(page + "state/")
        self.assertIn("no-store", state["Cache-Control"])
        self.assertTrue(state.json()["permissions"]["can_edit_draft"])
        StaffProfile.objects.filter(pk=self.profile.pk).update(access_generation=2)
        self.assertEqual(self.client.get(page + "state/").status_code, 403)
        self.assertEqual(self.client.get(page + "fragments/history/").status_code, 403)

    def test_queue_fragment_search_uses_only_linked_messages_and_deduplicates(self):
        match = Ticket.objects.create(dialog=self.dialog)
        unrelated_dialog = Dialog.objects.create(cursor=self.cursor, vk_peer_id=302)
        unrelated = Ticket.objects.create(dialog=unrelated_dialog)
        Message.objects.bulk_create([
            Message(dialog=self.dialog, ticket=match, text="Тренировка"),
            Message(dialog=self.dialog, ticket=match, text="После тренировки"),
            Message(dialog=unrelated_dialog, text="Тренировка"),
        ])
        url = "/support/fragments/tickets/"
        page = self.client.get(url, {"q": "ТРЕНИРОВК", "view": "unknown", "page": "-5"})
        self.assertContains(page, f"Обращение №{match.pk}", count=1)
        self.assertNotContains(page, f"Обращение №{unrelated.pk}")
        self.assertEqual(page.context["tickets"].number, 1)
        self.assertContains(self.client.get(url, {"q": str(unrelated.pk)}), f"Обращение №{unrelated.pk}")

    def test_poll_fragments_keep_editor_outside_replaceable_history(self):
        ticket = Ticket.objects.create(dialog=self.dialog, current_owner=self.profile)
        page = f"/support/tickets/{ticket.pk}/"
        full = self.client.get(page)
        self.assertContains(full, "data-editor-slot")
        self.assertContains(full, "data-ticket-state-url")
        header = self.client.get(page + "fragments/header/")
        self.assertContains(header, "data-editor-template")
        self.assertNotContains(header, "data-editor-slot")
        history = self.client.get(page + "fragments/history/")
        self.assertContains(history, "data-ticket-history")
        self.assertNotContains(history, "data-send-response")
        queue = self.client.get("/support/fragments/tickets/")
        self.assertContains(queue, "data-ticket-list")
        self.assertContains(queue, "Требует разбора")
        self.assertNotContains(queue, 'id="ticket-query"')
