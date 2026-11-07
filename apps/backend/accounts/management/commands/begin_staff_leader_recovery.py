from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from .assign_emergency_staff_leader import read_authority

from accounts.services import AccessError, begin_leader_recovery
from accounts.vk_id_client import is_configured


class Command(BaseCommand):
    help = "Начать восстановление после проверки личности руководителя вне кабинета."

    def add_arguments(self, parser):
        parser.add_argument("--authority-file", help="Внешнее подтверждение для аварийного режима.")
        parser.add_argument("--profile-id", required=True, type=int)
        parser.add_argument("--expected-identity-version", required=True, type=int)

    def handle(self, *args, **options):
        if not is_configured():
            raise CommandError("Настройте приложение VK ID и HTTPS-адрес возврата.")
        origin = urlsplit(settings.VK_ID_REDIRECT_URI)
        authority = read_authority(options["authority_file"]) if options["authority_file"] else None
        try:
            recovery, code = begin_leader_recovery(
                options["profile_id"], options["expected_identity_version"], authority=authority,
            )
        except AccessError as error:
            raise CommandError(error.code) from None
        url = f"{origin.scheme}://{origin.netloc}{reverse('leader-recovery')}?{urlencode({'code': code})}"
        self.stdout.write(f"Операция {recovery.pk}. Передайте ссылку руководителю по проверенному каналу.")
        self.stdout.write(url)
