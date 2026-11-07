import fcntl
import hashlib
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.db import connection, transaction


def lock_digest(digest):
    if not connection.in_atomic_block:
        raise RuntimeError("CAS lock requires a transaction")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [int.from_bytes(bytes.fromhex(digest[:16]), "big", signed=True)])


class CorruptObject(Exception):
    pass


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class StagedObject:
    path: Path
    digest: str
    size: int


class ObjectStore:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def _path(self, digest):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid SHA-256 digest")
        return self.root / "files" / digest[:2] / digest[2:4] / digest

    @contextmanager
    def stage(self, chunks, max_bytes):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_dir = self.root / "tmp"
        temporary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = None
        digest = hashlib.sha256()
        size = 0
        try:
            with NamedTemporaryFile(dir=temporary_dir, delete=False) as buffer:
                path = Path(buffer.name)
                fcntl.flock(buffer, fcntl.LOCK_EX)
                for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("File size limit exceeded")
                    digest.update(chunk)
                    buffer.write(chunk)
                buffer.flush()
                os.fsync(buffer.fileno())
                yield StagedObject(path, digest.hexdigest(), size)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def publish(self, staged):
        """Place durable bytes while the caller's transaction protects their reference."""
        key = staged.digest
        target = self._path(key)
        lock_digest(key)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check(staged.path, staged)
        if target.exists():
            self._check(target, staged)
        else:
            os.replace(staged.path, target)
            _sync_dir(staged.path.parent)
        for folder in target.parents:
            _sync_dir(folder)
            if folder == self.root:
                break
        return key

    @staticmethod
    def _check(path, staged):
        with path.open("rb") as file:
            if os.fstat(file.fileno()).st_size != staged.size or hashlib.file_digest(file, "sha256").hexdigest() != staged.digest:
                raise CorruptObject(staged.digest)
            os.fsync(file.fileno())

    def put(self, chunks, max_bytes):
        with self.stage(chunks, max_bytes) as staged, transaction.atomic():
            return self.publish(staged)

    def open(self, digest):
        return self._path(digest).open("rb")

    def sweep(self):
        from .models import FileObject

        cutoff = time.time() - 86400
        with transaction.atomic(durable=True), connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            marked = set(FileObject.objects.values_list("pk", flat=True))
        for path in (self.root / "files").glob("*/*/*"):
            key = path.name
            if not re.fullmatch(r"[0-9a-f]{64}", key) or path != self._path(key) or key in marked:
                continue
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock_shared(420971, 1)")
                lock_digest(key)
                if not FileObject.objects.filter(pk=key).exists():
                    self._unlink_old(path, cutoff)
        for path in (self.root / "tmp").glob("tmp*"):
            try:
                with path.open("rb") as file:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._unlink_old(path, cutoff)
            except (FileNotFoundError, BlockingIOError):
                continue

    @staticmethod
    def _unlink_old(path, cutoff):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                _sync_dir(path.parent)
        except FileNotFoundError:
            pass
