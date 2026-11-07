from django.db import models
from django.utils import timezone


class FileObject(models.Model):
    sha256 = models.CharField(max_length=64, primary_key=True)
    size_bytes = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "file_object"


class FileReadLease(models.Model):
    id = models.UUIDField(primary_key=True)
    file_object = models.ForeignKey(FileObject, on_delete=models.PROTECT)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "file_read_lease"
