from time import monotonic, sleep

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from jobs.worker import run_one
from support.attachment_source import download_source
from support.attachments import COLLECT, collect_task, expire_copies, recover_preparations, run_fetch_once, sweep_orphans


class Command(BaseCommand):
    help = "Prepare support files and collect expired copies."

    def add_arguments(self, parser):
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument("--cleanup-only", action="store_true")
        modes.add_argument("--recover-restored", action="store_true")
        parser.add_argument("--once", action="store_true")

    def handle(self, **options):
        if options["recover_restored"]:
            recover_preparations()
            return
        fetch = not options["cleanup_only"]
        if fetch and not (settings.VK_ATTACHMENT_FETCH_ENABLED and settings.VK_ATTACHMENT_HOSTS):
            raise CommandError("Enable attachment fetching and configure exact CDN hosts on the test stand first.")
        next_sweep = 0
        while True:
            worked = expire_copies()
            if fetch:
                worked = run_fetch_once(download_source) or worked
            worked = run_one({COLLECT: collect_task}) or worked
            if monotonic() >= next_sweep:
                sweep_orphans()
                next_sweep = monotonic() + 3600
            if options["once"]:
                return
            if not worked:
                sleep(2)
