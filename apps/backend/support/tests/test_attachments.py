import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.db import connections
from django.test import TransactionTestCase, override_settings

from accounts.models import StaffProfile
from jobs.models import BackgroundTask as Task
from jobs.services import claim
from storage.cas import ObjectStore
from storage.models import FileObject, FileReadLease
from storage.services import collect_object, database_time
from support.attachment_source import SourceError, public_endpoint
from support.attachments import FETCH, expire_copies, recover_preparations, run_fetch, run_fetch_once
from support.models import Attachment, AttachmentPreparation, Dialog, InboundRoute, Message, SupportCommand, Ticket
from support.services import grant_support_membership
from support.tests.test_http import signed_in
from vk_channel.models import LongPollCursor

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
def ogg_page(sequence, flags, packet):
    return (b"OggS\0" + bytes([flags]) + b"\0" * 12 + sequence.to_bytes(4, "little")
            + b"\0" * 4 + bytes([1, len(packet)]) + packet)


OGG = b"".join((
    ogg_page(0, 2, b"OpusHead" + b"\0" * 11),
    ogg_page(1, 0, b"OpusTags" + b"\0" * 8),
    ogg_page(2, 4, b"data"),
))


@override_settings(DEBUG=True)
class AttachmentTests(TransactionTestCase):
    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.enterContext(self.settings(CAS_ROOT=root))
        self.store = ObjectStore(root)
        self.cursor = LongPollCursor.objects.create(community_id=17, position="1")
        self.dialog = Dialog.objects.create(cursor=self.cursor, vk_peer_id=100)
        self.ticket = Ticket.objects.create(dialog=self.dialog)
        self.client, self.profile = signed_in("101")
        grant_support_membership(self.profile.pk, self.cursor.pk)
        self.a = self.attachment()

    def attachment(self, kind="doc", *, ticket=True):
        message = Message.objects.create(dialog=self.dialog, ticket=self.ticket if ticket else None)
        return Attachment.objects.create(
            message=message, source_position=0, state="metadata_only", attachment_type=kind,
            source_ref={"url": "https://example.test/secret"}, meta_info={"title": "../../unsafe.pdf"},
        )

    @staticmethod
    def url(a, action):
        return f"/support/attachments/{a.pk}/{action}/"

    def prepare(self, a=None, command=None, client=None):
        return (client or self.client).post(self.url(a or self.a, "prepare"), {
            "client_operation_id": str(command or uuid4()),
        }, content_type="application/json")

    def ready(self, a=None, data=PDF):
        self.assertEqual(self.prepare(a).status_code, 202)
        self.assertTrue(run_fetch_once(lambda _: data))
        (a or self.a).refresh_from_db()
        self.assertEqual((a or self.a).state, "ready")

    def expire(self, a=None):
        Attachment.objects.filter(pk=(a or self.a).pk).update(expires_at=database_time())

    def test_idempotency_expiration_and_closed_history(self):
        self.assertContains(self.client.get(f"/support/tickets/{self.ticket.pk}/"), "Подготовить файл")
        self.assertEqual(self.client.head(self.url(self.a, "download")).status_code, 409)
        self.assertFalse(Task.objects.exists())
        command = uuid4()
        first = self.prepare(command=command)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(self.prepare(command=command).json()["preparation"], first.json()["preparation"])
        self.assertEqual(self.prepare().json()["error"]["code"], "attachment_preparing")
        self.assertEqual(SupportCommand.objects.count(), 1)
        run_fetch_once(lambda _: PDF)
        self.a.refresh_from_db()
        self.assertEqual(self.a.expires_at - self.a.prepared_at, timedelta(hours=168))
        self.assertEqual(self.prepare(command=command).status_code, 200)
        self.assertEqual(self.prepare().json()["error"]["code"], "attachment_already_ready")
        self.assertEqual(self.client.get(self.url(self.a, "content")).json()["error"]["code"], "attachment_preview_unavailable")
        head = self.client.head(self.url(self.a, "download"))
        self.assertEqual(head.status_code, 200)
        self.assertFalse(FileReadLease.objects.exists())
        download = self.client.get(self.url(self.a, "download"))
        self.assertEqual(b"".join(download.streaming_content), PDF)
        download.close()
        self.assertIn("no-store", download["Cache-Control"])
        self.assertEqual(download["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("unsafe", download["Content-Disposition"])
        self.assertEqual(Attachment.objects.get(pk=self.a.pk).expires_at, self.a.expires_at)

        self.expire()
        self.assertEqual(self.client.head(self.url(self.a, "download")).status_code, 409)
        self.assertContains(self.client.get(f"/support/tickets/{self.ticket.pk}/"), "Срок хранения копии истёк")
        self.ready()  # Explicit re-preparation before the sweeper, reusing the same bytes.
        self.assertFalse(expire_copies())
        self.assertEqual(FileObject.objects.count(), 1)
        self.assertEqual(AttachmentPreparation.objects.count(), 2)
        self.assertEqual(self.prepare(command=command).json()["preparation"]["id"], first.json()["preparation"]["id"])

    def test_original_message_visibility_and_revocation_during_fetch(self):
        unassigned = self.attachment(ticket=False)
        self.assertEqual(self.client.get(self.url(unassigned, "state")).status_code, 404)
        route = InboundRoute.objects.create(
            message=unassigned.message, dialog=self.dialog, sequence=1, mode_version=1,
            automation_epoch=1, status="needs_review",
        )
        self.assertEqual(self.prepare(unassigned).status_code, 202)
        Message.objects.filter(pk=unassigned.message_id).update(ticket=self.ticket)
        route.status = "reviewed"
        route.save()

        def revoke(_):
            StaffProfile.objects.filter(pk=self.profile.pk).update(access_generation=2)
            return PDF

        run_fetch_once(revoke)
        unassigned.refresh_from_db()
        self.assertEqual(unassigned.state, "metadata_only")
        self.assertEqual(AttachmentPreparation.objects.get().state, "cancelled")
        self.assertFalse(FileObject.objects.exists())
        self.assertEqual(self.client.get(self.url(unassigned, "download")).status_code, 403)
        grant_support_membership(self.profile.pk, self.cursor.pk)
        self.ready(unassigned)

    def test_audio_range_and_gc_wait_for_reference_and_active_read(self):
        a, b = self.attachment("audio_message"), self.attachment("audio_message")
        self.ready(a, OGG)
        self.ready(b, OGG)
        self.assertEqual(a.file_object_id, b.file_object_id)
        r = self.client.get(self.url(a, "content"), HTTP_RANGE="bytes=2-7")
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r["Content-Range"], f"bytes 2-7/{len(OGG)}")
        lease = FileReadLease.objects.get()
        self.expire(a)
        self.assertEqual(self.client.get(self.url(a, "content"), HTTP_RANGE="bytes=2-7").status_code, 409)
        expire_copies()
        digest = a.file_object_id
        file = self.store._path(digest)
        old = time.time() - 86410
        os.utime(file, (old, old))
        self.assertIsNone(collect_object(digest))
        self.store.sweep()
        self.assertTrue(file.exists())
        self.expire(b)
        expire_copies()
        self.assertEqual(collect_object(digest), lease.expires_at)
        self.store.sweep()
        self.assertTrue(file.exists())
        self.assertEqual(b"".join(r.streaming_content), OGG[2:8])
        r.close()
        self.assertIsNone(collect_object(digest))
        self.store.sweep()
        self.assertFalse(file.exists())
        self.assertEqual(AttachmentPreparation.objects.count(), 2)

    def test_parallel_prepare_has_one_winner(self):
        client, profile = signed_in("102")
        grant_support_membership(profile.pk, self.cursor.pk)
        gate = Barrier(2)

        def request(c):
            try:
                gate.wait(timeout=5)
                return self.prepare(client=c).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(request, [self.client, client])), [202, 409])
        self.assertEqual(AttachmentPreparation.objects.count(), 1)
        self.assertEqual(Task.objects.filter(task_type=FETCH).count(), 1)

    def test_lost_lease_and_budget_do_not_publish(self):
        self.prepare()
        job = claim([FETCH])
        Task.objects.filter(pk=job.pk).update(locked_until=database_time(), attempt_count=job.max_attempts)
        run_fetch(job, lambda _: self.fail("Stale worker called VK"))
        run_fetch_once(lambda _: self.fail("Exhausted worker called VK"))
        self.a.refresh_from_db()
        self.assertEqual(self.a.reason_code, "fetch_budget_exhausted")
        self.assertEqual(Task.objects.get(pk=job.pk).status, Task.Status.DEAD)
        self.assertEqual(AttachmentPreparation.objects.get().state, "failed")

    def test_recovery_cancels_pending_and_missing_ready_object_returns_503(self):
        self.ready()
        self.store._path(self.a.file_object_id).unlink()
        response = self.client.get(self.url(self.a, "download"))
        self.assertEqual(response.json()["error"]["code"], "attachment_storage_unavailable")
        self.assertEqual(response.status_code, 503)
        other = self.attachment()
        self.prepare(other)
        claim([FETCH])
        self.expire()
        recover_preparations()
        self.a.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((self.a.reason_code, other.reason_code), ("cache_expired", "recovered_from_backup"))
        self.assertEqual(AttachmentPreparation.objects.get(attachment=other).state, "cancelled")
        self.assertIsNone(Task.objects.get(status=Task.Status.FAILED).lease_token)

    @override_settings(VK_ATTACHMENT_HOSTS={"example.test"})
    def test_private_dns_answers_and_active_content_are_rejected(self):
        with patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 443))]):
            with self.assertRaisesRegex(SourceError, "source_disallowed"):
                public_endpoint("https://example.test/file")
        with patch("socket.getaddrinfo") as lookup:
            with self.assertRaisesRegex(SourceError, "source_disallowed"):
                public_endpoint("https://example.test@other.test/file")
            lookup.assert_not_called()
        self.prepare()
        run_fetch_once(lambda _: b"<html><script>alert(1)</script></html>")
        self.a.refresh_from_db()
        self.assertEqual(self.a.state, "unsupported")
        self.assertFalse(FileObject.objects.exists())
