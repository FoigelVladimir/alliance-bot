from functools import partial
from time import sleep

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.policies import quarantine_incident
from vk_channel.client import send_click_answer
from vk_channel.services import valid_id
from vk_channel.student_actions import run_click_answer_once


class Command(BaseCommand):
    help = "Отправлять короткие подтверждения нажатий кнопок ВК."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        value = settings.VK_GROUP_ID
        if not settings.VK_CLICK_ANSWERS_VERIFIED or not settings.VK_ACCESS_TOKEN:
            raise CommandError("Подтверждения нажатий ВК выключены.")
        if not value.isascii() or not value.isdecimal() or len(value) > 19 or not valid_id(int(value)):
            raise CommandError("Укажите VK_GROUP_ID сообщества.")
        sender = partial(send_click_answer, settings.VK_ACCESS_TOKEN)
        while True:
            if quarantine_incident():
                raise CommandError("Подтверждения остановлены на время сверки после восстановления.")
            worked = run_click_answer_once(int(value), sender)
            if options["once"]:
                return
            if not worked:
                sleep(0.25)
