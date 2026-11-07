from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.exceptions import PermissionDenied
from django.test import TransactionTestCase

from storage.cas import CorruptObject, ObjectStore
from storage.services import download


class ObjectStoreTests(TransactionTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "cas"
        self.store = ObjectStore(self.root)

    def test_deduplicates_and_checks_access_before_opening(self):
        key = self.store.put([b"hel", b"lo"], max_bytes=5)
        self.assertEqual(self.store.put([b"hello"], max_bytes=5), key)
        self.assertEqual(len(list((self.root / "files").glob("*/*/*"))), 1)
        with self.assertRaises(PermissionDenied):
            download(self.store, "invalid path", allowed=False, filename="file.txt")
        response = download(self.store, key, allowed=True, filename="file.txt")
        try:
            self.assertEqual(b"".join(response.streaming_content), b"hello")
            self.assertEqual(response["Content-Disposition"], 'attachment; filename="file.txt"')
        finally:
            response.close()

    def test_limits_and_rejects_corrupt_existing_object(self):
        with self.assertRaises(ValueError):
            self.store.put([b"too large"], max_bytes=1)
        self.assertFalse(list((self.root / "tmp").iterdir()))
        key = self.store.put([b"hello"], max_bytes=5)
        next((self.root / "files").glob("*/*/*")).write_bytes(b"wrong")
        with self.assertRaises(CorruptObject):
            self.store.put([b"hello"], max_bytes=5)
        with self.assertRaises(ValueError):
            self.store.open("../../etc/passwd")
