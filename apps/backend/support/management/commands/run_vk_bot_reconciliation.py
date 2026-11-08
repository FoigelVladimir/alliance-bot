from functools import partial
from time import sleep

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.policies import quarantine_incident
from support.ai_reconciliation import run_reconciliation_once
from vk_channel.client import read_message_history
from vk_channel.services import valid_id


class Command(BaseCommand):
    help = "Сверить неизвестные части бота в пилотном диалоге."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        identifiers = (settings.VK_GROUP_ID, settings.VK_LONG_POLL_PILOT_PEER_ID)
        if not all(isinstance(value, str) and value.isascii() and value.isdecimal()
                   and len(value) <= 19 and valid_id(int(value)) for value in identifiers):
            raise CommandError("Укажите VK_GROUP_ID и VK_LONG_POLL_PILOT_PEER_ID.")
        community, peer = map(int, identifiers)
        read = (partial(read_message_history, settings.VK_ACCESS_TOKEN)
                if settings.VK_HISTORY_CORRELATION_VERIFIED and settings.VK_ACCESS_TOKEN else None)
        while True:
            if quarantine_incident():
                raise CommandError("Сверка приостановлена на время восстановления.")
            worked = run_reconciliation_once(community, peer, read)
            if options["once"]:
                return
            if not worked:
                sleep(0.5)
