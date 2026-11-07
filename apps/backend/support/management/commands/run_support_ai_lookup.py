from time import sleep

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from knowledge.embeddings import get_embedder
from support.ai_generation import ModelUnavailable, local_endpoint
from support.ai_processing import run_lookup_once


class Command(BaseCommand):
    help = "Run diagnostic knowledge lookup and save the result of each processing run."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        if not settings.SUPPORT_AI_LOOKUP_ENABLED or not settings.KNOWLEDGE_E5_ROOT:
            raise CommandError("Configure local E5 and enable SUPPORT_AI_LOOKUP_ENABLED.")
        try:
            get_embedder()
            if settings.SUPPORT_AI_GENERATION_ENABLED:
                local_endpoint()
        except (ImportError, ModelUnavailable, OSError, ValueError) as exc:
            raise CommandError(f"Cannot initialize local E5: {exc}") from exc
        while True:
            if not run_lookup_once():
                if options["once"]:
                    return
                sleep(0.5)
