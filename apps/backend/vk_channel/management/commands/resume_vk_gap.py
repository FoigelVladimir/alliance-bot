from django.core.management.base import BaseCommand, CommandError

from vk_channel.backfill import resume_gap
from vk_channel.services import position_value, valid_id


class Command(BaseCommand):
    help = "Зафиксировать подтверждение координатора и снять паузу для новых событий."

    def add_arguments(self, parser):
        parser.add_argument("--gap-id", required=True, type=int)
        parser.add_argument("--profile-id", required=True, type=int)
        parser.add_argument("--period", required=True, type=int)
        parser.add_argument("--position", required=True)
        parser.add_argument("--notes", required=True)

    def handle(self, *args, **options):
        if not all(valid_id(options[key]) for key in ("gap_id", "profile_id", "period")):
            raise CommandError("Некорректный идентификатор или период.")
        try:
            resume_gap(
                options["gap_id"], options["profile_id"], options["period"],
                position_value(options["position"]), options["notes"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from None
