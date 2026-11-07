from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock, patch
from uuid import uuid4

from django.db import DatabaseError, connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from accounts.models import StaffProfile
from support.models import Dialog, SupportCommand, SupportMembership, Ticket, TicketEvent
from support.services import change_assignment, grant_support_membership, take_ticket
from support.tests.test_http import signed_in
from vk_channel.models import LongPollCursor
from workspace import queries
from workspace.models import Notification
from workspace.services import notify_assignment, read_notification


class WorkspaceTests(SimpleTestCase):
    @override_settings(DEBUG=True)
    def test_home_renders_static_shell(self):
        response = self.client.get("/")
        self.assertContains(response, "Альянс")
        self.assertContains(response, 'type="importmap"')
        self.assertContains(response, "alliance/api")
        self.assertContains(response, "/static/css/app")


class WorkspaceSetup:
    def setUp(self):
        self.client, self.profile = signed_in("workspace-one")
        self.other_client, self.other = signed_in("workspace-two")
        self.cursor = LongPollCursor.objects.create(community_id=17, position="100")
        grant_support_membership(self.profile.pk, self.cursor.pk)
        grant_support_membership(self.other.pk, self.cursor.pk)
        self.dialog = Dialog.objects.create(cursor=self.cursor, vk_peer_id=101)
        self.ticket = Ticket.objects.create(dialog=self.dialog)

    def take(self, client=None, ticket=None):
        client = client or self.client
        ticket = ticket or self.ticket
        operation_id = uuid4()
        result = take_ticket(client.session, ticket.pk, operation_id, ticket.assignment_version)
        return result, operation_id


@override_settings(DEBUG=True)
class WorkspaceFlowTests(WorkspaceSetup, TestCase):
    def test_assignment_and_exact_repeat_create_one_personal_notification(self):
        _, operation = self.take()
        take_ticket(self.client.session, self.ticket.pk, operation, 1)
        item = Notification.objects.get()
        self.assertEqual(item.recipient_id, self.profile.pk)
        self.assertEqual(item.source_event.ticket_id, self.ticket.pk)
        self.assertEqual(TicketEvent.objects.count(), 1)
        feed = self.client.get("/staff/notifications/", HTTP_ACCEPT="application/json").json()
        self.assertEqual((feed["unread_count"], len(feed["notifications"])), (1, 1))
        self.assertEqual(feed["notifications"][0]["source_event_id"], str(item.source_event_id))
        self.assertIsNone(feed["notifications"][0]["read_at"])
        self.assertEqual(self.other_client.get("/staff/notifications/", HTTP_ACCEPT="application/json").json()["notifications"], [])

    def test_read_is_idempotent_and_open_work_remains(self):
        self.take()
        item = Notification.objects.get()
        url = f"/staff/notifications/{item.pk}/read/"
        first = self.client.post(url, {}, content_type="application/json", HTTP_ACCEPT="application/json")
        again = self.client.post(url, {}, content_type="application/json", HTTP_ACCEPT="application/json")
        self.assertTrue(first.json()["changed"])
        self.assertFalse(again.json()["changed"])
        self.assertEqual(first.json()["notification"]["read_at"], again.json()["notification"]["read_at"])
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.status, self.ticket.current_owner_id), ("open", self.profile.pk))
        home = self.client.get("/")
        self.assertContains(home, f"Обращение №{self.ticket.pk}")
        self.assertContains(home, "Мои обращения (1)")
        self.assertEqual(self.client.get("/staff/notifications/", HTTP_ACCEPT="application/json").json()["unread_count"], 0)

    def test_foreign_notification_cannot_be_read_and_get_has_no_side_effect(self):
        self.take()
        item = Notification.objects.get()
        url = f"/staff/notifications/{item.pk}/read/"
        self.assertEqual(self.other_client.post(url).status_code, 404)
        self.assertEqual(self.client.get(url).status_code, 405)
        item.refresh_from_db()
        self.assertIsNone(item.read_at)
        self.assertEqual(self.other_client.post(f"/staff/notifications/{2**63}/read/").status_code, 404)

    def test_open_without_javascript_uses_post_and_safe_source_redirect(self):
        self.take()
        item = Notification.objects.get()
        page = self.client.get("/staff/notifications/")
        self.assertContains(page, f'/staff/notifications/{item.pk}/read/')
        item.refresh_from_db()
        self.assertIsNone(item.read_at)
        result = self.client.post(f"/staff/notifications/{item.pk}/read/?next=https://example.org/")
        self.assertEqual((result.status_code, result["Location"]), (303, f"/support/tickets/{self.ticket.pk}/"))

    def test_source_access_is_checked_again_after_notification_is_read(self):
        self.take()
        item = Notification.objects.get()
        SupportMembership.objects.filter(profile=self.profile).delete()
        result = self.client.post(f"/staff/notifications/{item.pk}/read/", HTTP_ACCEPT="application/json")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.get(result.json()["notification"]["target_url"]).status_code, 403)
        self.assertEqual(self.client.get("/staff/notifications/", HTTP_ACCEPT="application/json").json()["total_count"], 1)

    def test_revoked_or_unapproved_staff_and_quarantine_cannot_read_feed(self):
        self.take()
        item = Notification.objects.get()
        url = f"/staff/notifications/{item.pk}/read/"
        StaffProfile.objects.filter(pk=self.profile.pk).update(access_state="revoked")
        self.assertEqual(self.client.get("/staff/notifications/").status_code, 403)
        self.assertEqual(self.client.post(url).status_code, 403)
        home = self.client.get("/")
        self.assertContains(home, "Рабочий доступ отозван")
        self.assertNotContains(home, "Требует внимания")
        item.refresh_from_db()
        self.assertIsNone(item.read_at)
        StaffProfile.objects.filter(pk=self.profile.pk).update(access_state="granted", cleared_incident="test-incident")
        session = self.client.session
        session["staff_incident"] = "test-incident"
        session.save()
        with override_settings(STAFF_QUARANTINE_INCIDENT="test-incident"):
            self.assertEqual(self.client.get("/staff/notifications/").status_code, 403)
            self.assertNotContains(self.client.get("/"), "Требует внимания")
        guest = self.client_class()
        self.assertEqual(guest.get("/staff/notifications/").status_code, 401)

    def test_notification_post_requires_csrf(self):
        client, profile = signed_in("workspace-csrf", csrf=True)
        grant_support_membership(profile.pk, self.cursor.pk)
        self.take(client)
        item = Notification.objects.get()
        url = f"/staff/notifications/{item.pk}/read/"
        self.assertEqual(client.post(url).status_code, 403)
        client.get("/staff/notifications/")
        self.assertEqual(client.post(url, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value).status_code, 303)

    def test_free_queue_and_summary_follow_source_records_with_access_scope(self):
        mine = Ticket.objects.create(dialog=Dialog.objects.create(cursor=self.cursor, vk_peer_id=102))
        self.take(ticket=mine)
        inaccessible_cursor = LongPollCursor.objects.create(community_id=99, position="100")
        hidden = Ticket.objects.create(dialog=Dialog.objects.create(cursor=inaccessible_cursor, vk_peer_id=103))
        home = self.client.get("/")
        self.assertContains(home, "Мои обращения (1)")
        self.assertContains(home, "Общая свободная очередь (1)")
        self.assertContains(home, f'/support/tickets/{self.ticket.pk}/')
        self.assertNotContains(home, f'/support/tickets/{hidden.pk}/')
        self.assertNotContains(home, "Ближайшее")
        self.assertNotContains(home, "Мои мероприятия")
        self.assertEqual(home.context["summary"]["data"]["open_count"], 2)
        self.assertEqual(home.context["summary"]["data"]["assigned_count"], 1)
        self.assertTrue(home["Cache-Control"].find("no-store") >= 0)

    def test_empty_state_is_distinct_from_partial_load_failure(self):
        empty = self.client.get("/")
        self.assertContains(empty, "У вас нет открытых обращений")
        with patch.dict(queries.SECTIONS, {"summary": Mock(side_effect=DatabaseError("test outage"))}):
            page = self.client.get("/")
            self.assertContains(page, "Не удалось загрузить сводку поддержки")
            self.assertContains(page, "Общая свободная очередь (1)")
            self.assertNotContains(page, "Назначено менеджерам")
            self.assertNotContains(page, "test outage")
        with patch.dict(queries.SECTIONS, {"attention": Mock(side_effect=DatabaseError())}):
            page = self.client.get("/")
            self.assertContains(page, "Не удалось загрузить текущую работу")
            self.assertContains(page, "Открыто обращений")
        self.assertContains(self.client.get("/staff/home/fragments/attention/"), "Общая свободная очередь (1)")
        self.assertEqual(self.client.get("/staff/home/fragments/missing/").status_code, 404)

    def test_staff_without_support_sees_no_invented_figures(self):
        SupportMembership.objects.filter(profile=self.profile).delete()
        home = self.client.get("/")
        self.assertContains(home, "Доступ к поддержке пока не назначен")
        self.assertNotContains(home, "Открыто обращений")
        self.assertContains(self.client.get("/staff/notifications/"), "Личных уведомлений пока нет")

    def test_counter_failure_preserves_home_work_and_shows_no_false_zero(self):
        with patch("workspace.queries.Notification.objects.filter") as rows:
            rows.return_value.count.side_effect = DatabaseError("counter unavailable")
            home = self.client.get("/")
        self.assertContains(home, "Счётчик недоступен")
        self.assertContains(home, "Общая свободная очередь (1)")

    def test_transfer_notifies_new_owner_and_return_creates_no_personal_notice(self):
        self.take()
        change_assignment(self.other_client.session, self.ticket.pk, uuid4(), 2, "takeover")
        self.assertEqual(Notification.objects.filter(recipient=self.other).count(), 1)
        change_assignment(self.other_client.session, self.ticket.pk, uuid4(), 3, "return_to_queue")
        self.assertEqual(Notification.objects.count(), 2)
        self.assertContains(self.client.get("/"), "Общая свободная очередь (1)")

    def test_coordinator_assignment_notifies_recipient_once(self):
        coordinator_client, coordinator = signed_in("workspace-coordinator")
        grant_support_membership(coordinator.pk, self.cursor.pk, coordination=True)
        self.take()
        operation_id = uuid4()
        for _ in range(2):
            change_assignment(coordinator_client.session, self.ticket.pk, operation_id, 2, "reassign_coordinator", self.other.pk)
        self.assertEqual(Notification.objects.filter(recipient=self.other).count(), 1)

    def test_failure_to_create_notification_rolls_back_assignment_and_event(self):
        with patch("workspace.services.Notification.objects.get_or_create", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                self.take()
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.current_owner_id)
        self.assertFalse(TicketEvent.objects.exists())
        self.assertFalse(SupportCommand.objects.exists())
        self.assertFalse(Notification.objects.exists())

    def test_pagination_counts_all_unread_and_rejects_invalid_pages(self):
        self.take()
        original = Notification.objects.get()
        for index in range(30):
            command = SupportCommand.objects.create(client_operation_id=uuid4(), actor=self.profile,
                action="take", target_type="ticket", target_id=self.ticket.pk, request_fingerprint="x" * 64)
            event = TicketEvent.objects.create(ticket=self.ticket, command=command, assignment_version=index + 3,
                previous_owner_id=self.other.pk, new_owner_id=self.profile.pk)
            notify_assignment(event)
        first = self.client.get("/staff/notifications/", HTTP_ACCEPT="application/json").json()
        second = self.client.get("/staff/notifications/?page=2", HTTP_ACCEPT="application/json").json()
        self.assertEqual((len(first["notifications"]), first["unread_count"], first["next_page"]), (30, 31, 2))
        self.assertEqual(len(second["notifications"]), 1)
        self.assertEqual(second["notifications"][0]["id"], str(original.pk))
        for query in ("page=0", "page=no", "page=-1", "page=1&page=2", "page=999999999999"):
            self.assertEqual(self.client.get("/staff/notifications/?" + query).status_code, 400)


@override_settings(DEBUG=True)
class WorkspaceConcurrencyTests(WorkspaceSetup, TransactionTestCase):
    def test_parallel_reads_store_one_timestamp(self):
        self.take()
        item = Notification.objects.get()
        barrier = Barrier(2)
        session = self.client.session

        def read():
            try:
                barrier.wait(timeout=5)
                result, changed = read_notification(session, item.pk)
                return result.read_at, changed
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(read) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(results[0][0], results[1][0])
        self.assertCountEqual([result[1] for result in results], [True, False])
