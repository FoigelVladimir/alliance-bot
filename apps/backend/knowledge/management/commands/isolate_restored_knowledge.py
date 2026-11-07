from django.core.management.base import BaseCommand, CommandError

from knowledge.publication import isolate_restored_publications


class Command(BaseCommand):
    help = "Exclude restored knowledge publications from search until republished."

    def handle(self, *args, **options):
        try:
            count = isolate_restored_publications()
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Publications awaiting verification: {count}")
