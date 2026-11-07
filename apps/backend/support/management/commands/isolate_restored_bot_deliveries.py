from django.core.management.base import BaseCommand, CommandError

from support.ai_delivery import isolate_restored_bot_deliveries


class Command(BaseCommand):
    help = "Block uncertain bot deliveries after restoring a database."

    def handle(self, *args, **options):
        try:
            isolate_restored_bot_deliveries()
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
