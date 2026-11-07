from functools import partial
from time import sleep

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from support.delivery import run_sender_once
from support.reconciliation import run_reconciliation_once
from vk_channel.client import read_message_history, send_message


class Command(BaseCommand):
    help = "Обработать очередь исходящих ответов поддержки."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--reconcile-only", action="store_true")

    def handle(self, *args, **options):
        reconcile = options["reconcile_only"]
        if not reconcile and (not settings.VK_SEND_ENABLED or not settings.VK_ACCESS_TOKEN):
            raise CommandError("Выбранный режим ВК выключен.")
        value = settings.VK_GROUP_ID
        if not value.isascii() or not value.isdecimal() or len(value) > 19 or not 0 < int(value) < 2**63:
            raise CommandError("Укажите VK_GROUP_ID сообщества.")
        community_id = int(value)
        runner = run_reconciliation_once if reconcile else run_sender_once
        transport = read_message_history if reconcile else send_message
        call = (None if reconcile and not (settings.VK_HISTORY_CORRELATION_VERIFIED and settings.VK_ACCESS_TOKEN)
                else partial(transport, settings.VK_ACCESS_TOKEN))
        while True:
            if not runner(community_id, call):
                if options["once"]:
                    return
                sleep(1)
