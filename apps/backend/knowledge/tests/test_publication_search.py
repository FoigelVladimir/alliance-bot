"""First 1Г block: draft boundaries. Publication/search are not enabled yet."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import urlencode

from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounts.models import StaffProfile
from accounts.services import AccessError
from knowledge.models import Material, MaterialCommand, MaterialVersion
from knowledge.services import create_material, save_draft
from support.services import grant_support_membership
from support.tests.test_http import signed_in
from vk_channel.models import LongPollCursor


class KnowledgeSetup:
    def setUp(self):
        self.client, self.profile = signed_in("knowledge-one")
        self.other_client, self.other = signed_in("knowledge-two")
        self.cursor = LongPollCursor.objects.create(community_id=17, position="100")
        for profile in (self.profile, self.other):
            grant_support_membership(profile.pk, self.cursor.pk)
        self.content = {"title": "Как записаться", "description": "Правила клуба", "tags": ["запись"],
                        "source_type": "text", "source_text": "  Текст источника 🙂\n"}

    def create(self, *, operation=None, content=None, client=None):
        return (client or self.client).post("/knowledge/materials/",
            {"client_operation_id": str(operation or uuid4()), **(content if content is not None else self.content)},
            content_type="application/json")

    def save(self, version, *, text="Новый текст", revision=1, operation=None, client=None):
        return (client or self.client).post(self.url(version), {
            "client_operation_id": str(operation or uuid4()), "expected_draft_revision": revision,
            **{**self.content, "source_text": text},
        }, content_type="application/json")

    @staticmethod
    def url(version):
        return f"/knowledge/materials/{version.material_id}/drafts/{version.pk}/"


@override_settings(DEBUG=True)
class TextDraftTests(KnowledgeSetup, TestCase):
    def test_create_and_repeat_save_only_one_material_and_draft(self):
        operation = uuid4()
        first, again = self.create(operation=operation), self.create(operation=operation)
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertTrue(again.json()["operation"]["replayed"])
        self.assertEqual((Material.objects.count(), MaterialVersion.objects.count(), MaterialCommand.objects.count()), (1, 1, 1))
        self.assertEqual(first.json()["draft"]["source_text"], self.content["source_text"])
        self.assertEqual(first.json()["draft"]["preparation_status"], "not_prepared")
        self.assertEqual(first.json()["material"]["publication_revision"], 0)

    def test_operation_id_is_scoped_to_employee_and_intent(self):
        operation = uuid4()
        self.create(operation=operation)
        mismatch = self.create(operation=operation, content={**self.content, "title": "Другое"})
        self.assertEqual((mismatch.status_code, mismatch.json()["error"]["code"]), (409, "operation_mismatch"))
        self.assertEqual(self.create(operation=operation, client=self.other_client).status_code, 201)
        version = MaterialVersion.objects.first()
        wrong_action = self.save(version, operation=operation)
        self.assertEqual(wrong_action.json()["error"]["code"], "operation_mismatch")
        self.assertEqual(MaterialCommand.objects.count(), 2)

    def test_failed_creation_rolls_back_material_version_and_command(self):
        with patch("knowledge.services.MaterialVersion.objects.create", side_effect=RuntimeError("write failure")):
            with self.assertRaises(RuntimeError):
                create_material(self.client.session, uuid4(), self.content)
        self.assertFalse(Material.objects.exists())
        self.assertFalse(MaterialVersion.objects.exists())
        self.assertFalse(MaterialCommand.objects.exists())

    def test_two_drafts_can_be_saved_independently(self):
        self.create()
        first = MaterialVersion.objects.get()
        url = f"/knowledge/materials/{first.material_id}/drafts/"
        response = self.other_client.post(url, {"client_operation_id": str(uuid4()), **self.content}, content_type="application/json")
        self.assertEqual(response.status_code, 201)
        second = MaterialVersion.objects.get(pk=response.json()["draft"]["id"])
        self.assertEqual(self.save(first, text="Первый").status_code, 200)
        self.assertEqual(self.save(second, text="Второй", client=self.other_client).status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.source_text, second.source_text, first.draft_revision, second.draft_revision), ("Первый", "Второй", 2, 2))
        self.assertEqual(second.created_by_id, self.other.pk)

    def test_replay_of_old_save_returns_saved_revision_and_current_draft(self):
        self.create()
        version = MaterialVersion.objects.get()
        operation = uuid4()
        self.save(version, operation=operation)
        self.save(version, revision=2, text="Поздняя правка", client=self.other_client)
        replay = self.save(version, operation=operation).json()
        self.assertEqual(replay["operation"]["saved_draft_revision"], 2)
        self.assertTrue(replay["operation"]["replayed"])
        self.assertEqual(replay["draft"]["draft_revision"], 3)
        self.assertEqual(replay["draft"]["source_text"], "Поздняя правка")
        self.assertEqual(MaterialCommand.objects.count(), 3)

    def test_conflict_preserves_local_input_and_shows_current_text(self):
        self.create()
        version = MaterialVersion.objects.get()
        self.save(version, text="Сохранено другим", client=self.other_client)
        response = self.save(version)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "draft_revision_conflict"))
        self.assertEqual(response.json()["current_draft_revision"], 2)
        html = self.client.post(self.url(version), urlencode({"client_operation_id": str(uuid4()), "expected_draft_revision": "1",
            "title": "Мой заголовок", "description": "Моё описание", "tags": "моя метка", "source_text": "Мой ввод <script>"}),
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(html.status_code, 409)
        self.assertContains(html, "Мой ввод &lt;script&gt;", status_code=409)
        self.assertContains(html, "Сохранено другим", status_code=409)
        self.assertContains(html, 'value="Мой заголовок"', status_code=409)
        version.refresh_from_db()
        self.assertEqual((version.source_text, version.draft_revision, version.updated_by_id), ("Сохранено другим", 2, self.other.pk))

    def test_wrong_material_cannot_read_or_write_another_draft(self):
        self.create()
        version = MaterialVersion.objects.get()
        other = Material.objects.create(created_by=self.profile)
        wrong = f"/knowledge/materials/{other.pk}/drafts/{version.pk}/"
        self.assertEqual(self.client.get(wrong).status_code, 404)
        self.assertEqual(self.client.post(wrong, {"client_operation_id": str(uuid4()), "expected_draft_revision": 1,
            **self.content}, content_type="application/json").status_code, 404)
        self.assertEqual(self.client.get(f"/knowledge/materials/{2**63}/drafts/{version.pk}/").status_code, 404)

    def test_draft_base_is_not_advanced_by_editing(self):
        self.create()
        version = MaterialVersion.objects.get()
        Material.objects.filter(pk=version.material_id).update(publication_revision=3)
        self.save(version)
        state = self.client.get(self.url(version), HTTP_ACCEPT="application/json").json()["draft"]
        self.assertEqual(state["base_publication_revision"], 0)
        self.assertTrue(state["needs_rebase"])
        response = self.client.post(f"/knowledge/materials/{version.material_id}/drafts/",
            {"client_operation_id": str(uuid4()), **self.content}, content_type="application/json")
        self.assertEqual(response.json()["draft"]["base_publication_revision"], 3)

    def test_sealed_snapshot_cannot_be_edited_but_remains_readable(self):
        self.create()
        version = MaterialVersion.objects.get()
        MaterialVersion.objects.filter(pk=version.pk).update(sealed_at=timezone.now())
        response = self.save(version)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "draft_sealed"))
        state = self.client.get(self.url(version), HTTP_ACCEPT="application/json").json()["draft"]
        self.assertFalse(state["editable"])
        self.assertEqual(state["source_text"], self.content["source_text"])
        self.assertContains(self.client.get(self.url(version)), "Текст источника")

    def test_current_support_access_required_on_every_operation_and_replay(self):
        operation = uuid4()
        self.create(operation=operation)
        version = MaterialVersion.objects.get()
        StaffProfile.objects.filter(pk=self.profile.pk).update(access_generation=2)
        self.assertEqual(self.create(operation=operation).status_code, 403)
        self.assertEqual(self.client.get(self.url(version)).status_code, 403)
        self.assertEqual(self.client.get("/knowledge/materials/").status_code, 403)
        guest = self.client_class()
        self.assertEqual(guest.get("/knowledge/materials/").status_code, 401)
        pending, _ = signed_in("knowledge-pending", allowed=False)
        self.assertEqual(pending.get("/knowledge/materials/").status_code, 403)

    def test_form_creation_and_save_require_csrf_and_use_redirect(self):
        client, profile = signed_in("knowledge-csrf", csrf=True)
        grant_support_membership(profile.pk, self.cursor.pk)
        fields = {"client_operation_id": str(uuid4()), "title": "Название", "source_text": "Введённый текст"}
        self.assertEqual(client.post("/knowledge/materials/", fields).status_code, 403)
        self.assertContains(client.get("/knowledge/materials/"), "Материалов пока нет")
        token = client.cookies["csrftoken"].value
        result = client.post("/knowledge/materials/", urlencode(fields), content_type="application/x-www-form-urlencoded", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(result.status_code, 303)
        self.assertContains(client.get(result["Location"]), "Введённый текст")
        version = MaterialVersion.objects.get()
        save = client.post(self.url(version), urlencode({**fields, "client_operation_id": str(uuid4()),
            "expected_draft_revision": "1", "source_text": "Исправлено"}), content_type="application/x-www-form-urlencoded", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(save.status_code, 303)
        self.assertEqual(MaterialVersion.objects.get().source_text, "Исправлено")

    def test_limits_are_bytes_not_characters_and_empty_text_stays_a_draft(self):
        too_large = self.create(content={**self.content, "source_text": "Я" * 32769})
        self.assertEqual((too_large.status_code, too_large.json()["error"]["code"]), (422, "text_too_large"))
        self.assertEqual(self.create(content={**self.content, "source_text": ""}).status_code, 201)
        self.assertEqual(self.create(content={**self.content, "title": " "}).status_code, 422)
        self.assertEqual(self.create(content={**self.content, "tags": ["метка"] * 21}).status_code, 422)
        self.assertEqual(self.create(content={**self.content, "source_type": "file"}).status_code, 422)
        self.assertEqual(self.create(content={**self.content, "source_text": "x" * 600000}).status_code, 413)
        self.assertEqual(MaterialVersion.objects.count(), 1)

    def test_invalid_unicode_and_null_byte_are_rejected_before_database_write(self):
        for content in ({**self.content, "title": "\ud800"}, {**self.content, "source_text": "x\0x"},
                        {**self.content, "tags": ["\ud800"]}):
            self.assertEqual(self.create(content=content).status_code, 400)
        self.assertFalse(Material.objects.exists())

    def test_catalog_escapes_titles_and_does_not_return_source_text(self):
        self.create(content={**self.content, "title": "<script>alert(1)</script>"})
        page = self.client.get("/knowledge/materials/")
        self.assertContains(page, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(page, "<script>alert(1)</script>")
        self.assertIn("no-store", page["Cache-Control"])
        feed = self.client.get("/knowledge/materials/", HTTP_ACCEPT="application/json").json()
        self.assertNotIn("source_text", feed["materials"][0]["drafts"][0])
        self.assertNotContains(page, "Опубликовать")


class TextDraftConcurrencyTests(KnowledgeSetup, TransactionTestCase):
    def test_two_editors_of_one_revision_have_one_winner(self):
        self.create()
        version = MaterialVersion.objects.get()
        barrier = Barrier(2)

        def write(session, text):
            try:
                barrier.wait(timeout=5)
                save_draft(session, version.material_id, version.pk, uuid4(), 1, {**self.content, "source_text": text})
                return "saved"
            except AccessError as error:
                return error.code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(write, client.session, text) for client, text in
                     ((self.client, "Первая правка"), (self.other_client, "Вторая правка"))]
            self.assertCountEqual([task.result(timeout=15) for task in tasks], ["saved", "draft_revision_conflict"])
        self.assertEqual(MaterialVersion.objects.get().draft_revision, 2)
        self.assertEqual(MaterialCommand.objects.count(), 2)

    def test_parallel_repeated_creation_returns_one_result(self):
        operation, barrier, session = uuid4(), Barrier(2), self.client.session

        def create():
            try:
                barrier.wait(timeout=5)
                command, replayed = create_material(session, operation, self.content)
                return command.material_id, replayed
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(create) for _ in range(2)]
            result = [task.result(timeout=15) for task in tasks]
        self.assertEqual(result[0][0], result[1][0])
        self.assertCountEqual([row[1] for row in result], [True, False])
        self.assertEqual((Material.objects.count(), MaterialVersion.objects.count()), (1, 1))
