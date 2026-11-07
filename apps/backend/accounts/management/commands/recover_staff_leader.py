from django.core.management.base import BaseCommand, CommandError

from accounts.services import AccessError, recover_leader


class Command(BaseCommand):
    help = "Применить проверенную замену VK ID к прежнему профилю руководителя."

    def add_arguments(self, parser):
        parser.add_argument("--recovery-id", required=True, type=int)
        parser.add_argument("--responsible", required=True)
        parser.add_argument("--transfer-empty-profile-id", type=int)

    def handle(self, *args, **options):
        responsible = options["responsible"].strip()
        if not responsible or len(responsible) > 255:
            raise CommandError("Укажите ответственное лицо.")
        try:
            event = recover_leader(options["recovery_id"], responsible, options["transfer_empty_profile_id"])
        except AccessError as error:
            profile = error.details.get("profile_id")
            raise CommandError(f"{error.code}, профиль {profile}" if profile else error.code) from None
        self.stdout.write(f"Профиль {event.subject_id}, событие {event.pk}")
