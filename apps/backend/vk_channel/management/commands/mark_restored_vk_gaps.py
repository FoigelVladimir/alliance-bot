from django.core.management.base import BaseCommand, CommandError

from vk_channel.backfill import mark_restored_gaps


class Command(BaseCommand):
    help = "Приостановить автоматизацию для восстановленных курсоров Long Poll."

    def handle(self, *args, **options):
        try:
            mark_restored_gaps()
        except ValueError as exc:
            raise CommandError(str(exc)) from None
