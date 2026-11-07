from django.core.exceptions import PermissionDenied
from django.http import FileResponse


def download(store, digest, *, allowed, filename):
    if not allowed:
        raise PermissionDenied
    return FileResponse(store.open(digest), as_attachment=True, filename=filename)


def database_time():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        return cursor.fetchone()[0]


def collect_object(digest):
    """Return the next possible collection time if a reader holds the object."""
    from django.db import transaction

    from .cas import lock_digest
    from .models import FileObject, FileReadLease

    with transaction.atomic():
        lock_digest(digest)
        obj = FileObject.objects.filter(pk=digest).first()
        if obj is None:
            return None
        leases = FileReadLease.objects.filter(file_object=obj)
        leases.filter(expires_at__lte=database_time()).delete()
        for field in FileObject._meta.related_objects:
            if field.related_model is FileReadLease:
                continue
            if field.related_model._base_manager.filter(**{field.field.name: obj}).exists():
                return None
        deadline = leases.order_by("-expires_at").values_list("expires_at", flat=True).first()
        if deadline is None:
            obj.delete()
        return deadline
