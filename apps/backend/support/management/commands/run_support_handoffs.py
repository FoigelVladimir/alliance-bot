from time import sleep

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from support.handoff_plans import run_handoffs_once


class Command(BaseCommand):
    help = "Check saved handoff plans and their common preparation/delivery deadlines."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        if not settings.SUPPORT_AI_HANDOFF_ENABLED:
            raise CommandError("Enable SUPPORT_AI_HANDOFF_ENABLED to run the handoff worker.")
        while True:
            if not run_handoffs_once():
                if options["once"]:
                    return
                sleep(0.5)
