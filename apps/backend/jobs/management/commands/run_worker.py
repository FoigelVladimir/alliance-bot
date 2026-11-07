import time

import psycopg
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, connections
from django.utils.module_loading import import_string

from jobs.services import CHANNEL, seconds_until_work
from jobs.worker import run_one


class Command(BaseCommand):
    help = "Run registered internal background tasks."

    def add_arguments(self, parser):
        parser.add_argument("--knowledge-index-only", action="store_true", help="Run the CPU worker for knowledge indexing.")
        parser.add_argument("--once", action="store_true", help="Drain currently available tasks.")

    def handle(self, *args, **options):
        handlers = {name: import_string(path) for name, path in settings.BACKGROUND_TASK_HANDLERS.items()}
        if options["knowledge_index_only"]:
            from knowledge.embeddings import get_embedder
            from knowledge.indexing import TASK_TYPE, index_snapshot_task

            try:
                get_embedder()
            except (ImportError, OSError, ValueError) as exc:
                raise CommandError(f"Cannot initialize the local E5 worker: {exc}") from exc
            handlers = {TASK_TYPE: index_snapshot_task}
        if not handlers:
            self.stdout.write("No background task handlers are registered.")
            return
        if options["once"]:
            while run_one(handlers):
                pass
            return

        while True:
            try:
                with psycopg.connect(settings.DATABASE_URL, autocommit=True) as listener:
                    listener.execute(f"LISTEN {CHANNEL}")
                    while True:
                        if not run_one(handlers):
                            next(listener.notifies(timeout=seconds_until_work(handlers), stop_after=1), None)
            except (psycopg.OperationalError, OperationalError):
                connections.close_all()
                self.stderr.write("Queue connection interrupted. Retrying in one second.")
                time.sleep(1)
