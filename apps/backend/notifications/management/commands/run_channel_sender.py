from functools import partial
from time import sleep

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.policies import quarantine_incident
from notifications.outbox import run_sender_once
from vk_channel.client import send_message
from vk_channel.services import valid_id


class Command(BaseCommand):
    help = "Send application notifications to the verified pilot dialog."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        if not (settings.VK_GENERAL_SEND_ENABLED and settings.VK_SEND_ENABLED and settings.VK_ACCESS_TOKEN):
            raise CommandError("Отправка уведомлений выключена.")
        identifiers = (settings.VK_GROUP_ID, settings.VK_LONG_POLL_PILOT_PEER_ID)
        if not all(value.isascii() and value.isdecimal() and len(value) <= 19 and valid_id(int(value)) for value in identifiers):
            raise CommandError("Укажите VK_GROUP_ID и VK_LONG_POLL_PILOT_PEER_ID.")
        community, peer = map(int, identifiers)
        send = partial(send_message, settings.VK_ACCESS_TOKEN)
        while True:
            if quarantine_incident():
                raise CommandError("Отправка остановлена на время восстановления.")
            worked = run_sender_once(community, peer, send)
            if options["once"]:
                return
            if not worked:
                sleep(0.5)
