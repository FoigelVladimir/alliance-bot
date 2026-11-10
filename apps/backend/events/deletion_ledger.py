"""Minimal append-only journal on a physically independent PostgreSQL cluster."""

from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from django.conf import settings
from django.db import connection


class LedgerUnavailable(Exception):
    pass


class LedgerMismatch(Exception):
    pass


def primary_cluster_id():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT system_identifier::text FROM pg_catalog.pg_control_system()")
            return cursor.fetchone()[0]
    except Exception:
        raise LedgerUnavailable("primary_cluster_identity_unavailable") from None


@contextmanager
def journal_connection():
    if not settings.EVENT_DELETION_LEDGER_DSN or not settings.APP_ENVIRONMENT_ID:
        raise LedgerUnavailable("ledger_not_configured")
    primary = primary_cluster_id()
    try:
        with psycopg.connect(settings.EVENT_DELETION_LEDGER_DSN, row_factory=dict_row, connect_timeout=5,
                             options="-c statement_timeout=5000 -c lock_timeout=5000", tcp_user_timeout=10000) as journal:
            cluster = journal.execute("SELECT system_identifier::text FROM pg_catalog.pg_control_system()").fetchone()["system_identifier"]
            if primary == cluster:
                raise LedgerMismatch("ledger_requires_separate_cluster")
            if journal.execute("SHOW fsync").fetchone()["fsync"] != "on":
                raise LedgerMismatch("ledger_requires_fsync")
            journal.execute("SET LOCAL synchronous_commit = on")
            yield journal
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise LedgerUnavailable("ledger_unavailable") from None
    except psycopg.errors.InsufficientPrivilege:
        raise LedgerUnavailable("ledger_permissions_unavailable") from None
    except psycopg.errors.UndefinedTable:
        raise LedgerUnavailable("ledger_schema_unavailable") from None


def mark_values(intent):
    return {"environment": intent.environment, "deletion_id": intent.pk, "event_uuid": intent.event_uuid, "actor_uuid": intent.actor_uuid,
            "operation_id": intent.client_operation_id, "request_fingerprint": intent.request_fingerprint,
            "notice_mode": intent.notice_mode}


def same_mark(mark, expected):
    return all(str(mark[key]) == str(value) for key, value in expected.items())


def append_mark(intent):
    if intent.environment != settings.APP_ENVIRONMENT_ID:
        raise LedgerMismatch("ledger_environment_mismatch")
    expected = mark_values(intent)
    try:
        with journal_connection() as journal:
            journal.execute("""
                INSERT INTO deletion_ledger.marks
                    (environment, deletion_id, event_uuid, actor_uuid, operation_id, request_fingerprint, notice_mode)
                VALUES (%(environment)s, %(deletion_id)s, %(event_uuid)s, %(actor_uuid)s, %(operation_id)s, %(request_fingerprint)s, %(notice_mode)s)
                ON CONFLICT (environment, event_uuid) DO NOTHING
            """, expected)
            mark = journal.execute("SELECT * FROM deletion_ledger.marks WHERE environment=%s AND event_uuid=%s",
                                   [intent.environment, intent.event_uuid]).fetchone()
            if mark is None or not same_mark(mark, expected):
                raise LedgerMismatch("ledger_command_mismatch")
        # Exiting the context has committed with synchronous_commit=on before returning success.
        return mark
    except psycopg.errors.UniqueViolation:
        raise LedgerMismatch("ledger_operation_mismatch") from None


def read_mark(environment, event_uuid):
    with journal_connection() as journal:
        return journal.execute("SELECT * FROM deletion_ledger.marks WHERE environment=%s AND event_uuid=%s",
                               [environment, event_uuid]).fetchone()


def read_deletion(environment, deletion_id):
    with journal_connection() as journal:
        return journal.execute("SELECT * FROM deletion_ledger.marks WHERE environment=%s AND deletion_id=%s",
                               [environment, deletion_id]).fetchone()


def read_environment(environment):
    with journal_connection() as journal:
        return journal.execute("SELECT * FROM deletion_ledger.marks WHERE environment=%s ORDER BY event_uuid", [environment]).fetchall()
