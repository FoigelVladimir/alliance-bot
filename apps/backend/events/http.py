from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from accounts.services import AccessError
from accounts.views import json_object

from .services import number


def as_json(request):
    return request.content_type == "application/json" or "application/json" in request.headers.get("Accept", "")


def command(request, required=(), optional=()):
    return json_object(request, ["client_operation_id", *required], optional)


def integer(request, data, key, *, minimum=1):
    value = data[key]
    if isinstance(value, str) and (request.content_type == "application/x-www-form-urlencoded" or key.endswith("_id")):
        if not value.isascii() or not value.isdecimal() or len(value) > 19:
            raise AccessError("invalid_request", 400)
        value = int(value)
    return number(value, minimum=minimum)


def identifier(value):
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise AccessError("invalid_request", 400) from None


def instant(value, *, timezone_name=None):
    if not isinstance(value, str) or len(value) > 64:
        raise AccessError("invalid_event_datetime", 422)
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            if not timezone_name:
                raise ValueError
            zone = ZoneInfo(timezone_name)
            first, second = result.replace(tzinfo=zone, fold=0), result.replace(tzinfo=zone, fold=1)
            if first.utcoffset() != second.utcoffset():
                raise ValueError
            if first.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None) != result:
                raise ValueError
            result = first
        return result
    except (ValueError, ZoneInfoNotFoundError):
        raise AccessError("invalid_event_datetime", 422) from None


def boolean(request, data, key, default=False):
    value = data.get(key, default)
    if request.content_type == "application/x-www-form-urlencoded" and value in ("on", "true", "false"):
        value = value in ("on", "true")
    if type(value) is not bool:
        raise AccessError("invalid_request", 400)
    return value


def editor_command(request, *, form_required=False, submit=False):
    required = ["expected_application_revision"] + (["form_revision_id"] if form_required else [])
    if submit:
        required.extend(["submit_operation_id", "registration_window_id"])
    if request.content_type == "application/json":
        return command(request, [*required, "values"])
    if request.content_type != "application/x-www-form-urlencoded":
        raise AccessError("invalid_request", 400)
    data = request.POST.dict()
    data.pop("csrfmiddlewaretoken", None)
    basic = {"client_operation_id", *required}
    extra = {"submit_operation_id", "registration_window_id"} if form_required else set()
    allowed = basic | extra | {"team_name", "contact", "category", "comment", "editor_action"}
    if not basic <= set(data) or any(len(items) != 1 for _, items in request.POST.lists()):
        raise AccessError("invalid_request", 400)
    players = {}
    for key, value in data.items():
        if key in allowed:
            continue
        parts = key.split(".")
        if (len(parts) != 3 or parts[0] != "player" or not parts[1].isascii() or not parts[1].isdecimal()
                or len(parts[1]) > 3 or int(parts[1]) >= 100
                or parts[2] not in {"id", "full_name", "student_number", "vk_url", "study_group", "is_self"}):
            raise AccessError("invalid_request", 400)
        players.setdefault(int(parts[1]), {})[parts[2]] = value
    for player in players.values():
        player["is_self"] = player.get("is_self") == "on"
        if not player.get("id"):
            player.pop("id", None)
    values = {key: data.get(key, "") for key in ("team_name", "contact", "category", "comment")}
    values["players"] = [players[key] for key in sorted(players)]
    return {**{key: data[key] for key in basic | extra if key in data}, "client_operation_id": identifier(data["client_operation_id"]),
            "values": values, "editor_action": data.get("editor_action", "")}
