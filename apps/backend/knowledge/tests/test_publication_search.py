"""First 1Г block: draft boundaries. Publication/search are not enabled yet."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import urlencode

from django.db import connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounts.models import StaffProfile
from accounts.services import AccessError
from jobs.models import BackgroundTask
from jobs.services import LeaseLost, claim
from jobs.worker import run_one
from knowledge.indexing import TASK_TYPE as INDEX_TASK_TYPE, index_snapshot_task
from knowledge.models import DocumentFragment, Material, MaterialCommand, MaterialVersion, PublicationEvent, SourceSnapshot, SupportOrigin
from knowledge.publication import isolate_restored_publications
from knowledge.search import SearchHit, rank_fragments, sources_are_current
from knowledge.preparation import TASK_TYPE, prepare_text_task
from knowledge.services import create_material, save_draft
from support.delivery import run_sender_once
from support.models import Dialog, Message, SupportMembership, Ticket
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


@override_settings(DEBUG=True)
class TextPreparationTests(KnowledgeSetup, TransactionTestCase):
    def prepare(self, version, *, revision=1, operation=None):
        return self.client.post(self.url(version) + "prepare/", {
            "client_operation_id": str(operation or uuid4()), "expected_draft_revision": revision,
        }, content_type="application/json")

    def test_prepares_exact_saved_text_and_replays_even_after_a_later_edit(self):
        text = "  <script>исходный текст</script> 🙂\n"
        self.create(content={**self.content, "source_text": text})
        version = MaterialVersion.objects.get()
        operation = uuid4()
        first = self.prepare(version, operation=operation)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(self.prepare(version, operation=operation).json()["preparation"]["id"],
                         first.json()["preparation"]["id"])
        self.assertEqual(self.prepare(version).json()["error"]["code"], "preparation_in_progress")
        self.assertTrue(run_one({TASK_TYPE: prepare_text_task}))
        page = self.client.get(self.url(version))
        self.assertContains(page, "&lt;script&gt;исходный текст&lt;/script&gt;")
        self.assertNotContains(page, "<script>исходный текст</script>")
        self.assertEqual(SourceSnapshot.objects.get().prepared_text, text)

        self.save(version, text="Вторая ревизия")
        replay = self.prepare(version, operation=operation)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["preparation"]["status"], "source_prepared")
        self.assertEqual(replay.json()["draft"]["preparation_status"], "not_prepared")
        self.assertEqual(replay.json()["draft"]["draft_revision"], 2)
        self.assertEqual(Material.objects.get().publication_revision, 0)

    def test_edit_revokes_old_worker_and_empty_source_has_no_snapshot(self):
        self.create()
        version = MaterialVersion.objects.get()
        operation = uuid4()
        self.assertEqual(self.prepare(version, operation=operation).status_code, 202)
        old_task = claim([TASK_TYPE])
        save_snapshot = prepare_text_task(old_task)
        self.assertEqual(self.save(version, text=" \n ").status_code, 200)
        with self.assertRaises(LeaseLost), transaction.atomic():
            save_snapshot()
        old_task.refresh_from_db()
        self.assertIsNone(old_task.lease_token)
        self.assertEqual(old_task.result["reason"], "superseded")
        self.assertEqual(self.prepare(version, operation=operation).json()["preparation"]["reason"], "superseded")
        self.assertEqual(self.prepare(version, revision=2).status_code, 202)
        self.assertTrue(run_one({TASK_TYPE: prepare_text_task}))
        self.assertContains(self.client.get(self.url(version)), "Источник пуст.")
        self.assertFalse(SourceSnapshot.objects.exists())

    def test_expired_last_lease_reports_terminal_failure(self):
        self.create()
        version = MaterialVersion.objects.get()
        self.prepare(version)
        task = claim([TASK_TYPE])
        BackgroundTask.objects.filter(pk=task.pk).update(
            locked_until=timezone.now(), attempt_count=task.max_attempts,
        )
        self.assertFalse(run_one({TASK_TYPE: prepare_text_task}))
        draft = self.client.get(self.url(version), HTTP_ACCEPT="application/json").json()["draft"]
        self.assertEqual(draft["preparation_status"], "failed")
        self.assertEqual(draft["preparation"]["reason"], "attempt_budget_exhausted")
        self.assertFalse(SourceSnapshot.objects.exists())


    def test_index_generation_preserves_text_and_remains_inactive(self):
        text = ("Тренировка по баскетболу. Запись через капитана.\n" * 150) + "Последняя строка."
        self.create(content={**self.content, "source_text": text})
        version = MaterialVersion.objects.get()
        self.prepare(version)
        run_one({TASK_TYPE: prepare_text_task})

        class FakeEmbedder:
            def token_count(self, fragment):
                return len(fragment)

            def passages(self, fragments):
                return [[1.0, *([0.0] * 383)] for _ in fragments]

        with patch("knowledge.indexing.get_embedder", return_value=FakeEmbedder()):
            self.assertTrue(run_one({INDEX_TASK_TYPE: index_snapshot_task}))
        fragments = list(DocumentFragment.objects.order_by("position"))
        self.assertGreater(len(fragments), 1)
        self.assertEqual("".join(fragment.content for fragment in fragments), text)
        self.assertFalse(any(fragment.is_active for fragment in fragments))
        self.assertTrue(all(len(fragment.content) <= 400 for fragment in fragments))
        self.assertTrue(all(fragment.search_vector for fragment in fragments))
        draft = self.client.get(self.url(version), HTTP_ACCEPT="application/json").json()["draft"]
        self.assertEqual(draft["preparation"]["index_status"], "ready")

    def test_edit_revokes_index_lease_before_fragments_can_be_written(self):
        self.create()
        version = MaterialVersion.objects.get()
        self.prepare(version)
        run_one({TASK_TYPE: prepare_text_task})
        task = claim([INDEX_TASK_TYPE])

        class FakeEmbedder:
            def token_count(self, fragment):
                return len(fragment)

            def passages(self, fragments):
                return [[1.0, *([0.0] * 383)] for _ in fragments]

        with patch("knowledge.indexing.get_embedder", return_value=FakeEmbedder()):
            save_generation = index_snapshot_task(task)
        self.assertEqual(self.save(version).status_code, 200)
        with self.assertRaises(LeaseLost), transaction.atomic():
            save_generation()
        task.refresh_from_db()
        self.assertIsNone(task.lease_token)
        self.assertEqual(task.result["reason"], "superseded")
        self.assertFalse(DocumentFragment.objects.exists())

    def test_incomplete_vectors_never_mark_index_ready(self):
        self.create()
        version = MaterialVersion.objects.get()
        self.prepare(version)
        run_one({TASK_TYPE: prepare_text_task})

        class IncompleteEmbedder:
            def token_count(self, fragment):
                return len(fragment)

            def passages(self, fragments):
                return []

        with (patch("knowledge.indexing.get_embedder", return_value=IncompleteEmbedder()),
              self.assertLogs("jobs.worker", level="ERROR")):
            self.assertTrue(run_one({INDEX_TASK_TYPE: index_snapshot_task}))
        draft = self.client.get(self.url(version), HTTP_ACCEPT="application/json").json()["draft"]
        self.assertEqual(draft["preparation"]["index_status"], "failed")
        self.assertFalse(DocumentFragment.objects.exists())


    def test_publication_rebase_replacement_and_old_unpublish_replay(self):
        self.create()
        first = MaterialVersion.objects.get()
        self.other_client.post(f"/knowledge/materials/{first.material_id}/drafts/", {
            "client_operation_id": str(uuid4()), **{**self.content, "source_text": "Новое расписание тренировок"},
        }, content_type="application/json")
        second = MaterialVersion.objects.exclude(pk=first.pk).get()
        first_snapshot = self.build_index(first)
        old_second_snapshot = self.build_index(second)
        vector = [1.0, *([0.0] * 383)]
        self.assertEqual(rank_fragments("текст", vector), [])

        first_publish = self.publication_command(first, "publish", 0, snapshot=first_snapshot.pk)
        self.assertEqual(first_publish.status_code, 200)
        hits = rank_fragments("текст", vector)
        self.assertTrue(hits)
        self.assertEqual({hit.snapshot_id for hit in hits}, {first_snapshot.pk})
        with transaction.atomic():
            self.assertTrue(sources_are_current(hits))
        self.assertContains(self.client.get(self.url(first)), "Снять с публикации")

        stale = self.publication_command(second, "publish", 1, snapshot=old_second_snapshot.pk)
        self.assertEqual(stale.json()["error"]["code"], "draft_base_changed")
        rebase = self.client.post(self.url(second) + "rebase/", {
            "client_operation_id": str(uuid4()), "expected_draft_revision": 1, "expected_publication_revision": 1,
        }, content_type="application/json")
        self.assertEqual(rebase.status_code, 200)
        self.assertEqual(rebase.json()["draft"]["source_text"], "Новое расписание тренировок")
        second_snapshot = self.build_index(second, revision=2)
        wrong = self.publication_command(second, "publish", 1, snapshot=old_second_snapshot.pk)
        self.assertEqual(wrong.json()["error"]["code"], "prepared_snapshot_changed")

        with patch("knowledge.publication.PublicationEvent.objects.create", side_effect=RuntimeError("save failure")):
            with self.assertRaises(RuntimeError):
                self.publication_command(second, "publish", 1, snapshot=second_snapshot.pk)
        self.assertEqual(rank_fragments("текст", vector), hits)
        unpublish_id = uuid4()
        self.assertEqual(self.publication_command(first, "unpublish", 1, operation=unpublish_id).status_code, 200)
        self.assertEqual(rank_fragments("текст", vector), [])
        with transaction.atomic():
            self.assertFalse(sources_are_current(hits))

        # Rebase to the new publication revision and publish the fresh snapshot.
        self.client.post(self.url(second) + "rebase/", {
            "client_operation_id": str(uuid4()), "expected_draft_revision": 2, "expected_publication_revision": 2,
        }, content_type="application/json")
        replacement = self.build_index(second, revision=3)
        self.assertEqual(self.publication_command(second, "publish", 2, snapshot=replacement.pk).status_code, 200)
        replay = self.publication_command(first, "unpublish", 1, operation=unpublish_id).json()
        self.assertTrue(replay["operation"]["replayed"])
        self.assertEqual(replay["material"]["published_version_id"], str(second.pk))
        self.assertEqual({hit.snapshot_id for hit in rank_fragments("расписание", vector)}, {replacement.pk})
        self.assertEqual(PublicationEvent.objects.count(), 3)

    def test_restored_publication_is_hidden_after_quarantine_is_released(self):
        self.create()
        version = MaterialVersion.objects.get()
        snapshot = self.build_index(version)
        self.assertEqual(self.publication_command(version, "publish", 0, snapshot=snapshot.pk).status_code, 200)
        vector = [1.0, *([0.0] * 383)]
        hits = rank_fragments("текст", vector)
        self.assertTrue(hits)
        with self.settings(STAFF_QUARANTINE_INCIDENT="knowledge-restore"):
            self.assertEqual(rank_fragments("текст", vector), [])
            self.assertEqual(isolate_restored_publications(), 1)
        self.assertEqual(rank_fragments("текст", vector), [])
        with transaction.atomic():
            self.assertFalse(sources_are_current(hits))

    def build_index(self, version, *, revision=1):
        self.assertEqual(self.prepare(version, revision=revision).status_code, 202)
        run_one({TASK_TYPE: prepare_text_task})

        class FakeEmbedder:
            def token_count(self, fragment):
                return len(fragment)

            def passages(self, fragments):
                return [[1.0, *([0.0] * 383)] for _ in fragments]

        with patch("knowledge.indexing.get_embedder", return_value=FakeEmbedder()):
            run_one({INDEX_TASK_TYPE: index_snapshot_task})
        version.refresh_from_db()
        return version.current_preparation.snapshot

    def publication_command(self, version, action, revision, *, operation=None, snapshot=None):
        body = {"client_operation_id": str(operation or uuid4()), "version_id": str(version.pk),
                "expected_publication_revision": revision}
        if snapshot is not None:
            body["prepared_snapshot_id"] = str(snapshot)
        return self.client.post(f"/knowledge/materials/{version.material_id}/{action}/", body,
                                content_type="application/json")


    def test_replacing_publication_switches_both_search_branches(self):
        self.create()
        first = MaterialVersion.objects.get()
        first_snapshot = self.build_index(first)
        self.assertEqual(self.publication_command(first, "publish", 0, snapshot=first_snapshot.pk).status_code, 200)
        old_hits = rank_fragments("источник", [1.0, *([0.0] * 383)])
        self.assertTrue(old_hits)
        response = self.client.post(f"/knowledge/materials/{first.material_id}/drafts/", {
            "client_operation_id": str(uuid4()), **{**self.content, "source_text": "Зал и расписание"},
        }, content_type="application/json")
        second = MaterialVersion.objects.get(pk=response.json()["draft"]["id"])
        second_snapshot = self.build_index(second)
        result = self.publication_command(second, "publish", 1, snapshot=second_snapshot.pk)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["event"]["previous_version_id"], str(first.pk))
        new_hits = rank_fragments("источник", [1.0, *([0.0] * 383)])
        self.assertEqual({hit.snapshot_id for hit in new_hits}, {second_snapshot.pk})
        self.assertFalse(DocumentFragment.objects.filter(prepared_snapshot=first_snapshot, is_active=True).exists())
        with transaction.atomic():
            self.assertFalse(sources_are_current(old_hits))
            self.assertTrue(sources_are_current(new_hits))


    def test_origin_draft_is_idempotent_and_private_to_ticket_readers(self):
        dialog = Dialog.objects.create(cursor=self.cursor, vk_peer_id=21)
        ticket = Ticket.objects.create(dialog=dialog, current_owner=self.profile)
        question = Message.objects.create(dialog=dialog, ticket=ticket, text="Частный вопрос студента")
        ticket_url = f"/support/tickets/{ticket.pk}/"
        sent = self.client.post(ticket_url + "send/", {
            "client_operation_id": str(uuid4()), "expected_assignment_version": 1,
            "text": "Ответ сотрудника", "source_draft": None,
        }, content_type="application/json")
        self.assertEqual(sent.status_code, 202)
        self.assertTrue(run_sender_once(17, lambda *_: 667))
        answer = Message.objects.get(ticket=ticket, direction="outbound_manager")
        url = "/knowledge/drafts/from-support/"
        self.assertContains(self.client.get(url, {"ticket_id": ticket.pk}), answer.text)
        body = {"client_operation_id": str(uuid4()), "ticket_id": str(ticket.pk),
                "question_id": str(question.pk), "answer_id": str(answer.pk)}
        invalid = self.client.post(url, {**body, "answer_id": str(question.pk)}, content_type="application/json")
        self.assertEqual(invalid.status_code, 404)
        first = self.client.post(url, body, content_type="application/json")
        replay = self.client.post(url, body, content_type="application/json")
        self.assertEqual((first.status_code, replay.status_code), (201, 200))
        self.assertEqual(first.json()["draft"]["id"], replay.json()["draft"]["id"])
        origin = SupportOrigin.objects.get()
        self.assertEqual((origin.question_id, origin.answer_id), (question.pk, answer.pk))
        self.assertContains(self.client.get(self.url(origin.version)), "Исходное обращение")
        self.assertEqual(rank_fragments(question.text, [1.0, *([0.0] * 383)]), [])
        self.assertEqual(Message.objects.filter(ticket=ticket).count(), 2)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.Status.OPEN)

        other_cursor = LongPollCursor.objects.create(community_id=18, position="100")
        SupportMembership.objects.filter(profile=self.other).delete()
        grant_support_membership(self.other.pk, other_cursor.pk)
        self.assertEqual(self.other_client.get(self.url(origin.version)).status_code, 404)
        self.assertEqual(self.other_client.post(url, body, content_type="application/json").status_code, 404)
        catalog = self.other_client.get("/knowledge/materials/", HTTP_ACCEPT="application/json").json()
        self.assertEqual(catalog["total_count"], 0)
        with patch("support.views.hybrid_search") as search:
            self.assertEqual(self.other_client.get(ticket_url + "knowledge-search/", {"q": "секция"}).status_code, 404)
            search.assert_not_called()
        hit = SearchHit(1, origin.version.material_id, origin.version_id, 1, "Раздел", "Фрагмент", 0.01)
        with patch("support.views.hybrid_search", return_value=[hit]):
            result = self.client.get(ticket_url + "knowledge-search/", {"q": "секция"}).json()
        self.assertEqual(result, {"results": [{"title": "Раздел", "content": "Фрагмент", "source_url": self.url(origin.version)}]})
