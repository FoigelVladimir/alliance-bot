from functools import partial
from time import sleep

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.policies import quarantine_incident
from vk_channel.client import DeliveryUnknown, long_poll_server, read_long_poll
from vk_channel.services import InvalidBatch, ReaderLeaseLost, claim_reader, poll_once, release_reader, valid_id


class Command(BaseCommand):
    help = "Принимать события одного пилотного диалога через VK Bots Long Poll."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        if not settings.VK_LONG_POLL_ENABLED or not settings.VK_ACCESS_TOKEN:
            raise CommandError("Чтение VK Long Poll выключено.")
        value = settings.VK_GROUP_ID
        if not value.isascii() or not value.isdecimal() or len(value) > 19 or not valid_id(int(value)):
            raise CommandError("Укажите VK_GROUP_ID сообщества.")

        peer = settings.VK_LONG_POLL_PILOT_PEER_ID
        if not peer.isascii() or not peer.isdecimal() or len(peer) > 19 or not valid_id(int(peer)):
            raise CommandError("Укажите числовой VK_LONG_POLL_PILOT_PEER_ID тестового диалога.")
        community_id = int(value)
        pilot_peer_id = int(peer)
        discover = partial(long_poll_server, settings.VK_ACCESS_TOKEN, community_id)
        reader = server = None
        delay = 1
        try:
            while True:
                if quarantine_incident():
                    raise CommandError("Приём остановлен на время сверки после восстановления.")
                try:
                    if reader is None:
                        server = discover()
                        reader = claim_reader(community_id, server.position)
                    if reader is not None:
                        reader, server = poll_once(reader, server, read_long_poll, discover, pilot_peer_id=pilot_peer_id)
                    if options["once"]:
                        return
                    delay = 1
                    if reader is None:
                        sleep(delay)
                except ReaderLeaseLost:
                    reader = None
                    if options["once"]:
                        raise CommandError("Читатель потерял аренду.") from None
                    sleep(delay)
                except (DeliveryUnknown, InvalidBatch):
                    if options["once"]:
                        raise CommandError("Не удалось подтвердить порцию VK Long Poll.") from None
                    self.stderr.write("Порция VK Long Poll не подтверждена. Повтор с сохранённой позиции.")
                    sleep(delay)
                    delay = min(15, delay * 2)
        finally:
            if reader is not None:
                release_reader(reader)
