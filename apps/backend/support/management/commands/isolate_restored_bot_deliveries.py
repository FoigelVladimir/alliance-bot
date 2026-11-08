from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from support.ai_delivery import isolate_restored_bot_deliveries


class Command(BaseCommand):
    help = "Block uncertain bot deliveries after restoring a database."

    def add_arguments(self, parser):
        parser.add_argument("--recovery-run-id", type=UUID)

    def handle(self, *args, **options):
        try:
            isolate_restored_bot_deliveries(options["recovery_run_id"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
