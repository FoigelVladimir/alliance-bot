from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import BrowserLogin, LeaderRecovery


class Command(BaseCommand):
    help = "Удалить истёкшие попытки VK ID и очистить временные данные восстановления."

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()
        attempts, _ = BrowserLogin.objects.filter(expires_at__lte=now).delete()
        recoveries = LeaderRecovery.objects.filter(expires_at__lte=now).exclude(
            status=LeaderRecovery.Status.COMPLETED,
        ).exclude(code_hash__isnull=True, external_id="").update(code_hash=None, external_id="")
        self.stdout.write(f"Удалено попыток входа: {attempts}. Очищено операций восстановления: {recoveries}.")
