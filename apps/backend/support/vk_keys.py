from secrets import randbelow

from accounts.services import AccessError

from .models import VKRandomReservation


def reserve_random_id():
    for _ in range(8):
        key = randbelow(2**31 - 1) + 1
        _, created = VKRandomReservation.objects.get_or_create(pk=key)
        if created:
            return key
    raise AccessError("send_unavailable", 503)
