from django.core.management.base import BaseCommand, CommandError

from accounts.services import AccessError
from support.services import grant_support_membership
from vk_channel.models import LongPollCursor


class Command(BaseCommand):
    help = "Выдать проверенному сотруднику доступ к поддержке указанного сообщества."

    def add_arguments(self, parser):
        parser.add_argument("--profile-id", required=True, type=int)
        parser.add_argument("--community-id", required=True, type=int)
        coordination = parser.add_mutually_exclusive_group()
        coordination.add_argument("--coordinator", dest="coordination", action="store_true", default=None)
        coordination.add_argument("--remove-coordination", dest="coordination", action="store_false")

    def handle(self, *args, **options):
        cursor_id = LongPollCursor.objects.filter(
            community_id=options["community_id"],
        ).values_list("pk", flat=True).first()
        if cursor_id is None:
            raise CommandError("Сообщество не настроено.")
        try:
            grant_support_membership(options["profile_id"], cursor_id, coordination=options["coordination"])
        except AccessError as error:
            raise CommandError(error.code) from None
        self.stdout.write("Доступ к поддержке выдан.")
