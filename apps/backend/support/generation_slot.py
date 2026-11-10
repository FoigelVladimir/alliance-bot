"""Serialize automatic model work across workers without holding a DB transaction."""

from contextlib import contextmanager
from contextvars import ContextVar

from django.db import connection

_active = ContextVar("support_generation_slot", default=False)
LOCK = (420972, 1)


@contextmanager
def generation_slot():
    if _active.get():
        yield False
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", LOCK)
        acquired = cursor.fetchone()[0]
    token = _active.set(acquired)
    try:
        yield acquired
    finally:
        _active.reset(token)
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s, %s)", LOCK)
