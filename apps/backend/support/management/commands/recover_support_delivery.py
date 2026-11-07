from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from support.recovery import RecoveryConflict, recover_support_delivery, register_recovery


class Command(BaseCommand):
    help = "Санитарная обработка отправок поддержки в изолированной восстановленной БД."

    def add_arguments(self, parser):
        parser.add_argument("--recovery-run-id", required=True, type=UUID)

    def handle(self, *args, **options):
        try:
            run = register_recovery(options["recovery_run_id"])
            changed = recover_support_delivery(run.pk)
        except RecoveryConflict as error:
            raise CommandError(str(error)) from error
        self.stdout.write("Recovery completed." if changed else "Recovery already completed.")
