from functools import partial
from time import sleep

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.policies import quarantine_incident
from vk_channel.backfill import run_gap_once
from vk_channel.client import read_conversations, read_message_history
from vk_channel.services import valid_id


class Command(BaseCommand):
    help = "Восполнить доступные сообщения после разрыва Long Poll."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        if settings.VK_LONG_POLL_PILOT_PEER_ID:
            raise CommandError("В пилотном режиме восполнение истории сообщества выключено.")
        value = settings.VK_GROUP_ID
        if not settings.VK_GAP_HISTORY_VERIFIED or not settings.VK_ACCESS_TOKEN:
            raise CommandError("Чтение истории разрыва выключено.")
        if not value.isascii() or not value.isdecimal() or len(value) > 19 or not valid_id(int(value)):
            raise CommandError("Укажите VK_GROUP_ID сообщества.")
        conversations = partial(read_conversations, settings.VK_ACCESS_TOKEN)
        history = partial(read_message_history, settings.VK_ACCESS_TOKEN)
        while True:
            if quarantine_incident():
                raise CommandError("Чтение остановлено на время сверки после восстановления.")
            worked = run_gap_once(int(value), conversations, history)
            if options["once"]:
                return
            if not worked:
                sleep(2)
