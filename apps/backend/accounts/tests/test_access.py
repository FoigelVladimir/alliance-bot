import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.core.management import CommandError, call_command
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounts import services, vk_id_client
from accounts.management.commands.assign_emergency_staff_leader import EmergencyAuthority
from accounts.models import AccessApplication, AccessControl, AccessEvent, BrowserLogin, IdentityBinding, LeaderRecovery, StaffProfile
from accounts.vk_id_client import VKIdentity


@override_settings(VK_ID_CLIENT_ID="123", VK_ID_REDIRECT_URI="https://testserver/auth/vk/callback/")
class StaffAccessTests(TestCase):
    def start(self, client):
        response = client.post("/auth/vk/start/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("no-store", response["Cache-Control"])
        return parse_qs(urlsplit(response.url).query)["state"][0]

    def callback(self, client, state, external_id="101"):
        with patch("accounts.vk_id_client.exchange", return_value=VKIdentity(external_id, "Анна", "")) as exchange:
            response = client.get("/auth/vk/callback/", {"state": state, "code": "secret", "device_id": "device"})
        self.assertNotIn("secret", response.url)
        return response, exchange

    def login(self, client, external_id="101"):
        state = self.start(client)
        old = client.session.session_key
        response, exchange = self.callback(client, state, external_id)
        exchange.assert_called_once()
        self.assertEqual(response.url, "/")
        self.assertNotEqual(client.session.session_key, old)
        return IdentityBinding.objects.get(external_id=external_id).profile

    def post(self, client, url, **data):
        return client.post(url, json.dumps(data), content_type="application/json")

    def apply(self, client, operation=None, activity=""):
        return self.post(client, "/access/application/", client_operation_id=operation or str(uuid4()), club_activity=activity)

    def make_leader(self):
        client = Client()
        profile = self.login(client, "102")
        return client, profile, services.bootstrap_leader(profile.pk, profile.identity_version)

    def test_login_keeps_only_verified_identity_and_cancels_old_callbacks(self):
        other = Client()
        old = self.start(self.client)
        new = self.start(self.client)
        for client, state in ((self.client, old), (other, new)):
            response, exchange = self.callback(client, state)
            exchange.assert_not_called()
            self.assertIn("cancelled", response.url)
        self.assertFalse(StaffProfile.objects.exists())
        response, exchange = self.callback(self.client, new)
        exchange.assert_called_once()
        self.assertEqual(response.url, "/")
        _, exchange = self.callback(self.client, new)
        exchange.assert_not_called()
        self.assertEqual(StaffProfile.objects.count(), 1)
        self.assertEqual(self.client.get("/staff/access/applications/").status_code, 403)
        self.assertContains(self.client.get("/"), "Подать заявку")

    def test_expired_callback_and_logout_during_exchange_do_not_log_in(self):
        state = self.start(self.client)
        BrowserLogin.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        _, exchange = self.callback(self.client, state)
        exchange.assert_not_called()
        self.assertFalse(BrowserLogin.objects.exists())
        state = self.start(self.client)
        second_tab = Client()
        second_tab.cookies.update(self.client.cookies)

        def finish_exchange(**kwargs):
            second_tab.post("/auth/logout/")
            return VKIdentity("101", "Анна", "")

        with patch("accounts.vk_id_client.exchange", side_effect=finish_exchange):
            response = self.client.get("/auth/vk/callback/", {"state": state, "code": "secret", "device_id": "device"})
        self.assertIn("failed", response.url)
        self.assertFalse(StaffProfile.objects.exists())

    def test_reject_retry_and_idempotency(self):
        profile = self.login(self.client)
        reviewer, _, _ = self.make_leader()
        operation = str(uuid4())
        result = self.apply(self.client, operation)
        self.assertEqual(result.status_code, 201)
        entry_id = result.json()["application"]["id"]
        self.assertEqual(self.apply(self.client).status_code, 409)
        url = f"/staff/access/applications/{entry_id}/reject/"
        data = dict(client_operation_id=str(uuid4()), expected_identity_version=profile.identity_version)
        result = self.post(reviewer, url, applicant_message="  Уточните команду  ", **data)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["application"]["applicant_message"], "Уточните команду")
        self.assertTrue(self.post(reviewer, url, applicant_message="Уточните команду", **data).json()["replayed"])
        self.assertEqual(self.apply(self.client, operation).json()["application"]["status"], "rejected")
        self.assertEqual(self.apply(self.client, operation, "другое").json()["error"]["code"], "operation_mismatch")
        self.assertEqual(self.apply(self.client).status_code, 429)
        AccessApplication.objects.filter(pk=entry_id).update(decided_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(self.apply(self.client).status_code, 201)

    def test_revoke_requires_new_exchange_and_regrant_requires_new_session(self):
        profile = self.login(self.client)
        leader, _, _ = self.make_leader()
        entry_id = self.apply(self.client).json()["application"]["id"]
        result = self.post(leader, f"/staff/access/applications/{entry_id}/approve/",
                           client_operation_id=str(uuid4()), expected_identity_version=1, role="manager")
        self.assertEqual(result.status_code, 200)
        self.assertContains(self.client.get("/"), "Рабочий доступ открыт")
        pending = Client()
        state = self.start(pending)
        change = f"/staff/access/profiles/{profile.pk}/change/"
        revoke = dict(client_operation_id=str(uuid4()), expected_access_version=2, action="revoke_access")
        self.assertEqual(self.post(leader, change, **revoke).status_code, 200)
        self.assertTrue(self.post(leader, change, **revoke).json()["replayed"])
        self.assertEqual(self.client.get("/access/application/").status_code, 401)
        self.callback(pending, state)
        regrant = dict(client_operation_id=str(uuid4()), expected_access_version=3, action="regrant_access")
        self.assertEqual(self.post(leader, change, **regrant).status_code, 409)
        self.login(Client())
        self.assertEqual(self.post(leader, change, **regrant).status_code, 200)
        self.assertEqual(pending.get("/access/application/").status_code, 401)
        profile.refresh_from_db()
        self.assertEqual(profile.access_generation, 2)

    def test_recovery_transfers_only_confirmed_empty_identity(self):
        leader, profile, initial = self.make_leader()
        self.assertEqual(services.bootstrap_leader(profile.pk, 1), initial)
        source_client = Client()
        source = self.login(source_client, "200")
        recovery, code = services.begin_leader_recovery(profile.pk, 1)
        browser = Client()
        start = browser.post("/auth/vk/recovery/", {"recovery_code": code})
        state = parse_qs(urlsplit(start.url).query)["state"][0]
        response, _ = self.callback(browser, state, "200")
        self.assertEqual(response.url, "/auth/vk/recovery/confirmed/")
        self.assertEqual(browser.get("/access/application/").status_code, 401)
        with self.assertRaisesMessage(services.AccessError, "transfer_confirmation_required"):
            services.recover_leader(recovery.pk, "оператор")
        event = services.recover_leader(recovery.pk, "оператор", source.pk)
        self.assertEqual(services.recover_leader(recovery.pk, "оператор", source.pk), event)
        self.assertEqual(leader.get("/access/application/").status_code, 401)
        self.assertEqual(source_client.get("/access/application/").status_code, 401)
        self.assertEqual(self.login(Client(), "200").pk, profile.pk)
        old_browser = Client()
        response, _ = self.callback(old_browser, self.start(old_browser), "102")
        self.assertIn("failed", response.url)
        self.assertEqual(StaffProfile.objects.count(), 2)
        self.assertEqual(AccessEvent.objects.filter(action="recover_leader").count(), 1)

    def test_administrator_permissions_and_revocation_of_delegation(self):
        leader, _, _ = self.make_leader()
        admin = Client()
        admin_profile = self.login(admin, "103")
        manager = self.login(self.client)
        admin_app = self.apply(admin).json()["application"]["id"]
        manager_app = self.apply(self.client).json()["application"]["id"]
        admin_decision = f"/staff/access/applications/{admin_app}/approve/"
        manager_decision = f"/staff/access/applications/{manager_app}/approve/"
        self.assertEqual(self.post(
            leader, admin_decision, client_operation_id=str(uuid4()), expected_identity_version=1,
            role="administrator",
        ).status_code, 200)
        self.assertContains(admin.get("/staff/access/applications/"), "Сотрудники")
        decision = dict(client_operation_id=str(uuid4()), expected_identity_version=1, role="administrator")
        self.assertEqual(self.post(admin, manager_decision, **decision).status_code, 403)
        change = f"/staff/access/profiles/{admin_profile.pk}/change/"
        self.assertEqual(self.post(
            leader, change, client_operation_id=str(uuid4()), expected_access_version=2,
            action="grant_appoint_administrator",
        ).status_code, 200)
        self.assertEqual(self.post(admin, manager_decision, **decision).status_code, 200)
        self.assertEqual(self.post(
            admin, f"/staff/access/profiles/{manager.pk}/change/",
            client_operation_id=str(uuid4()), expected_access_version=2, action="revoke_access",
        ).status_code, 403)
        self.assertEqual(self.post(
            leader, change, client_operation_id=str(uuid4()), expected_access_version=3,
            action="revoke_access",
        ).status_code, 200)
        self.assertEqual(admin.get("/staff/access/applications/").status_code, 403)
        admin_profile.refresh_from_db()
        self.assertFalse(admin_profile.can_appoint_administrator)
        self.assertTrue(AccessEvent.objects.filter(subject=admin_profile, action="revoke_appoint_administrator").exists())

    def test_vk_adapter_uses_server_profile_and_rejects_mismatched_state(self):
        with patch("accounts.vk_id_client.post", side_effect=[
            {"access_token": "token", "state": "expected", "user_id": 999},
            {"user": {"user_id": 101, "first_name": "Анна", "last_name": "Иванова"}},
        ]) as post:
            identity = vk_id_client.exchange("code", "device", "expected", "verifier")
        self.assertEqual(identity.external_id, "101")
        self.assertEqual(identity.display_name, "Анна Иванова")
        self.assertEqual(post.call_args_list[0].args[1]["code_verifier"], "verifier")
        self.assertEqual(post.call_args_list[1].args[2], {"access_token": "token"})
        with patch("accounts.vk_id_client.post", return_value={"access_token": "token", "state": "wrong"}) as post:
            with self.assertRaises(vk_id_client.VKLoginError):
                vk_id_client.exchange("code", "device", "expected", "verifier")
        post.assert_called_once()

    def test_emergency_recovery_requires_external_confirmation_and_opens_only_selected_profile(self):
        former_leader, former_profile, _ = self.make_leader()
        selected_client = Client()
        selected = self.login(selected_client, "201")
        StaffProfile.objects.filter(pk=selected.pk).update(
            access_state=StaffProfile.Access.REVOKED, role=StaffProfile.Role.ADMINISTRATOR,
            can_appoint_administrator=True,
        )
        started_before = Client()
        old_state = self.start(started_before)
        with TemporaryDirectory() as directory, override_settings(STAFF_QUARANTINE_INCIDENT="restore-1"):
            self.assertEqual(former_leader.get("/access/application/").status_code, 401)
            self.assertEqual(selected_client.get("/access/application/").status_code, 401)
            _, exchange = self.callback(started_before, old_state, "300")
            exchange.assert_not_called()
            response, _ = self.callback(started_before, self.start(started_before), "300")
            self.assertIn("failed", response.url)
            self.assertFalse(IdentityBinding.objects.filter(external_id="300").exists())
            with self.assertRaisesMessage(services.AccessError, "invalid_emergency_authority"):
                services.begin_leader_recovery(selected.pk, selected.identity_version)

            authority = Path(directory) / "authority.json"
            authority_data = {
                "incident": "restore-1", "profile_id": selected.pk, "reference": "record-7",
                "expires_at": (timezone.now() + timedelta(hours=1)).isoformat(),
            }
            authority.write_text(json.dumps(authority_data), encoding="utf-8")
            result = StringIO()
            call_command(
                "begin_staff_leader_recovery", profile_id=selected.pk, expected_identity_version=1,
                authority_file=str(authority), stdout=result,
            )
            code = parse_qs(urlsplit(result.getvalue().splitlines()[-1]).query)["code"][0]
            recovery = LeaderRecovery.objects.get(profile=selected)
            browser = Client()
            start = browser.post("/auth/vk/recovery/", {"recovery_code": code})
            state = parse_qs(urlsplit(start.url).query)["state"][0]
            response, _ = self.callback(browser, state, "401")
            self.assertEqual(response.url, "/auth/vk/recovery/confirmed/")
            self.assertEqual(browser.get("/access/application/").status_code, 401)
            with self.assertRaisesMessage(services.AccessError, "invalid_recovery"):
                services.recover_leader(recovery.pk, "оператор")
            options = dict(recovery_id=recovery.pk, authority_file=str(authority), responsible="оператор")
            authority.write_text(json.dumps({**authority_data, "reference": "other"}), encoding="utf-8")
            with self.assertRaisesMessage(CommandError, "invalid_emergency_authority"):
                call_command("assign_emergency_staff_leader", **options)
            authority.write_text(json.dumps(authority_data), encoding="utf-8")
            call_command("assign_emergency_staff_leader", **options, stdout=StringIO())
            call_command("assign_emergency_staff_leader", **options, stdout=StringIO())
            recovered_client = Client()
            profile = self.login(recovered_client, "401")
            self.assertEqual(profile.pk, selected.pk)
            self.assertEqual(profile.role, StaffProfile.Role.LEADER)
            self.assertFalse(profile.can_appoint_administrator)
            self.assertEqual(profile.access_generation, 2)
            self.assertEqual(selected_client.get("/access/application/").status_code, 401)
            self.assertEqual(former_leader.get("/access/application/").status_code, 401)
            self.assertNotContains(recovered_client.get("/staff/access/applications/"), f"профиль {former_profile.pk}")
            event = AccessEvent.objects.get(action="assign_emergency_leader")
            self.assertEqual(event.parameters["authority_reference"], "record-7")
        self.assertEqual(AccessControl.objects.get().quarantine_incident, "restore-1")
        self.assertEqual(former_leader.get("/access/application/").status_code, 401)
        self.assertEqual(recovered_client.get("/access/application/").status_code, 200)

    @override_settings(STAFF_QUARANTINE_INCIDENT="restore-conflict")
    def test_emergency_recovery_does_not_transfer_an_identity_with_history(self):
        selected = StaffProfile.objects.create()
        old = IdentityBinding.objects.create(profile=selected, external_id="500")
        owner = StaffProfile.objects.create()
        binding = IdentityBinding.objects.create(profile=owner, external_id="501")
        AccessApplication.objects.create(profile=owner, client_operation_id=uuid4())
        authority = EmergencyAuthority(
            "restore-conflict", selected.pk, "record-8", timezone.now() + timedelta(hours=1),
        )
        recovery, _ = services.begin_leader_recovery(selected.pk, 1, authority=authority)
        LeaderRecovery.objects.filter(pk=recovery.pk).update(
            status=LeaderRecovery.Status.VERIFIED, external_id=binding.external_id, code_hash=None,
        )
        with self.assertRaisesMessage(services.AccessError, "transfer_profile_not_empty"):
            services.recover_leader(recovery.pk, "оператор", owner.pk, authority=authority)
        self.assertTrue(IdentityBinding.objects.get(pk=old.pk).active)
        self.assertEqual(IdentityBinding.objects.get(pk=binding.pk).profile_id, owner.pk)
        selected.refresh_from_db()
        self.assertEqual(selected.cleared_incident, "")
        self.assertFalse(AccessEvent.objects.filter(action="assign_emergency_leader").exists())

    def test_recovery_expiring_before_final_lock_keeps_original_binding(self):
        _, profile, _ = self.make_leader()
        recovery, _ = services.begin_leader_recovery(profile.pk, profile.identity_version)
        LeaderRecovery.objects.filter(pk=recovery.pk).update(
            status=LeaderRecovery.Status.VERIFIED, external_id="201", code_hash=None,
        )
        with patch("accounts.services.timezone.now", side_effect=[
            recovery.expires_at - timedelta(seconds=1), recovery.expires_at,
        ]), self.assertRaisesMessage(services.AccessError, "invalid_recovery"):
            services.recover_leader(recovery.pk, "оператор")
        self.assertEqual(profile.bindings.get(active=True).external_id, "102")
        self.assertFalse(IdentityBinding.objects.filter(external_id="201").exists())

    def test_scheduled_cleanup_removes_only_expired_login_and_recovery_secrets(self):
        _, leader, initial = self.make_leader()
        self.start(self.client)
        expired_session = self.client.session.session_key
        live_client = Client()
        self.start(live_client)
        now = timezone.now()
        BrowserLogin.objects.filter(pk=expired_session).update(expires_at=now, exchanging=True)
        expired, _ = services.begin_leader_recovery(leader.pk, leader.identity_version)
        live, _ = services.begin_leader_recovery(leader.pk, leader.identity_version)
        completed, _ = services.begin_leader_recovery(leader.pk, leader.identity_version)
        LeaderRecovery.objects.filter(pk=expired.pk).update(
            expires_at=now, status=LeaderRecovery.Status.VERIFIED, external_id="101", code_hash=None,
        )
        LeaderRecovery.objects.filter(pk=completed.pk).update(
            expires_at=now, status=LeaderRecovery.Status.COMPLETED,
            external_id="101", code_hash=None, result=initial,
        )

        with patch("accounts.management.commands.clear_expired_staff_login.timezone.now", return_value=now):
            call_command("clear_expired_staff_login", stdout=StringIO())
            second_run = StringIO()
            call_command("clear_expired_staff_login", stdout=second_run)

        self.assertIn("Удалено попыток входа: 0. Очищено операций восстановления: 0.", second_run.getvalue())
        self.assertFalse(BrowserLogin.objects.filter(pk=expired_session).exists())
        self.assertTrue(BrowserLogin.objects.filter(pk=live_client.session.session_key).exists())
        expired.refresh_from_db()
        live.refresh_from_db()
        completed.refresh_from_db()
        self.assertEqual(expired.external_id, "")
        self.assertIsNone(expired.code_hash)
        self.assertIsNotNone(live.code_hash)
        self.assertEqual(completed.result_id, initial.pk)

    @override_settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"), SESSION_COOKIE_SECURE=True)
    def test_login_start_accepts_same_https_origin_through_proxy(self):
        client = Client(enforce_csrf_checks=True)
        client.get("/", HTTP_X_FORWARDED_PROTO="https", HTTP_HOST="testserver")
        token = client.cookies["csrftoken"].value
        form = {"csrfmiddlewaretoken": token}
        response = client.post(
            "/auth/vk/start/", form, HTTP_ORIGIN="https://testserver", HTTP_X_FORWARDED_PROTO="https", HTTP_HOST="testserver",
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.cookies["sessionid"]["secure"])
        self.assertEqual(
            client.post("/auth/vk/start/", form, HTTP_ORIGIN="https://other.invalid",
                        HTTP_X_FORWARDED_PROTO="https", HTTP_HOST="testserver").status_code, 403,
        )

    def test_start_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post("/auth/vk/start/").status_code, 403)
        self.assertEqual(client.get("/")["Referrer-Policy"], "strict-origin")
        token = client.cookies["csrftoken"].value
        data = {"csrfmiddlewaretoken": token}
        self.assertEqual(client.post("/auth/vk/start/", data, HTTP_ORIGIN="null").status_code, 403)
        response = client.post("/auth/vk/start/", data, HTTP_ORIGIN="http://testserver")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(client.get("/auth/vk/callback/")["Referrer-Policy"], "no-referrer")


class ConcurrentLoginTests(TransactionTestCase):
    def test_first_login_in_two_browsers_uses_one_profile(self):
        barrier = Barrier(2)

        def login():
            try:
                client = Client()
                services.start_login(client.session)
                attempt = BrowserLogin.objects.get(session_key=client.session.session_key)
                BrowserLogin.objects.filter(pk=attempt.pk).update(exchanging=True)
                barrier.wait(timeout=5)
                return services.finish_login(client.session, attempt, VKIdentity("301", "Анна", ""))
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(login) for _ in range(2)]
            self.assertEqual([result.result(timeout=10) for result in results], [True, True])
        self.assertEqual(StaffProfile.objects.count(), 1)
        self.assertEqual(IdentityBinding.objects.count(), 1)
