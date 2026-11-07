import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts.services import AccessError, recover_leader


@dataclass(frozen=True)
class EmergencyAuthority:
    incident: str
    profile_id: int
    reference: str
    expires_at: datetime


def read_authority(path):
    try:
        with Path(path).open(encoding="utf-8") as source:
            data = json.loads(source.read(8192))
        if not isinstance(data, dict) or set(data) != {"incident", "profile_id", "reference", "expires_at"}:
            raise ValueError
        incident, profile_id, reference = data["incident"], data["profile_id"], data["reference"]
        expires_at = datetime.fromisoformat(data["expires_at"])
        if (
            not isinstance(incident, str) or not 0 < len(incident.strip()) <= 128
            or type(profile_id) is not int or profile_id <= 0
            or not isinstance(reference, str) or not 0 < len(reference.strip()) <= 255
            or timezone.is_naive(expires_at)
        ):
            raise ValueError
    except (OSError, TypeError, ValueError):
        raise CommandError("Требуется действительное внешнее подтверждение полномочий.") from None
    return EmergencyAuthority(incident, profile_id, reference, expires_at)


class Command(BaseCommand):
    help = "Назначить проверенного руководителя в аварийном карантине."

    def add_arguments(self, parser):
        parser.add_argument("--recovery-id", required=True, type=int)
        parser.add_argument("--authority-file", required=True)
        parser.add_argument("--responsible", required=True)
        parser.add_argument("--transfer-empty-profile-id", type=int)

    def handle(self, *args, **options):
        responsible = options["responsible"].strip()
        if not responsible or len(responsible) > 255:
            raise CommandError("Укажите ответственное лицо.")
        authority = read_authority(options["authority_file"])
        try:
            event = recover_leader(
                options["recovery_id"], responsible, options["transfer_empty_profile_id"], authority=authority,
            )
        except AccessError as error:
            profile = error.details.get("profile_id")
            raise CommandError(f"{error.code}, профиль {profile}" if profile else error.code) from None
        self.stdout.write(f"Профиль {event.subject_id}, событие {event.pk}")
