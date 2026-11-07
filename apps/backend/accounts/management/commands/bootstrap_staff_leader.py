from django.core.management.base import BaseCommand, CommandError

from accounts.services import AccessError, bootstrap_leader


class Command(BaseCommand):
    help = "Назначить первого руководителя после проверки личности вне кабинета."

    def add_arguments(self, parser):
        parser.add_argument("--profile-id", required=True, type=int)
        parser.add_argument("--expected-identity-version", required=True, type=int)

    def handle(self, *args, **options):
        try:
            event = bootstrap_leader(options["profile_id"], options["expected_identity_version"])
        except AccessError as error:
            raise CommandError(error.code) from None
        self.stdout.write(f"Профиль {event.subject_id}, событие {event.pk}")
