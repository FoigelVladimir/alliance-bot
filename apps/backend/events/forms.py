"""Two bounded registration templates, with immutable published rules."""

import hashlib
import json
from urllib.parse import urlsplit

from accounts.services import AccessError

OPTIONAL_FIELDS = {"full_name", "study_group", "comment"}
PLAYER_FIELDS = {"full_name", "student_number", "vk_url", "study_group"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def text(value, maximum, *, required=False):
    if not isinstance(value, str) or "\0" in value or len(value) > maximum:
        raise AccessError("invalid_content", 422)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise AccessError("invalid_content", 422) from None
    if required and not value.strip():
        raise AccessError("invalid_content", 422)
    return value


def schema_for(participation_format, *, categories=None, min_players=None, max_players=None, optional_fields=None, hints=None):
    if participation_format not in {"team", "individual"}:
        raise AccessError("invalid_participation_format", 422)
    categories = [] if categories is None else categories
    optional_fields = ["full_name", "study_group"] if optional_fields is None else optional_fields
    hints = {} if hints is None else hints
    if (not isinstance(categories, list) or len(categories) > 50
            or any(not isinstance(category, str) for category in categories)
            or len(set(categories)) != len(categories)):
        raise AccessError("invalid_categories", 422)
    for category in categories:
        text(category, 100, required=True)
    if (not isinstance(optional_fields, list) or any(not isinstance(key, str) for key in optional_fields)
            or len(set(optional_fields)) != len(optional_fields) or set(optional_fields) - OPTIONAL_FIELDS):
        raise AccessError("invalid_form_fields", 422)
    if not isinstance(hints, dict) or set(hints) - (PLAYER_FIELDS | {"contact", "team_name", "comment", "category"}):
        raise AccessError("invalid_form_fields", 422)
    for hint in hints.values():
        text(hint, 500)
    if any(value is not None and (type(value) is not int or not 1 <= value <= 100) for value in (min_players, max_players)):
        raise AccessError("invalid_roster_limits", 422)
    if (min_players is not None and max_players is not None and min_players > max_players
            or participation_format == "individual" and (min_players is not None or max_players is not None)):
        raise AccessError("invalid_roster_limits", 422)
    return {"template_version": 1, "participation_format": participation_format, "categories": categories,
            "min_players": min_players, "max_players": max_players, "optional_fields": optional_fields, "hints": hints}


def validate_schema(value):
    expected = {"template_version", "participation_format", "categories", "min_players", "max_players", "optional_fields", "hints"}
    if not isinstance(value, dict) or set(value) != expected or type(value["template_version"]) is not int or value["template_version"] != 1:
        raise AccessError("invalid_form_schema", 422)
    return schema_for(**{key: item for key, item in value.items() if key != "template_version"})


def compatible(previous, next_schema):
    fixed = {"template_version", "participation_format", "categories", "min_players", "max_players"}
    return (all(previous[key] == next_schema[key] for key in fixed)
            and set(previous["optional_fields"]) <= set(next_schema["optional_fields"]))


def validate_values(values, schema, *, complete=False):
    if not isinstance(values, dict) or set(values) - {"team_name", "contact", "category", "comment", "players"}:
        raise AccessError("invalid_application_data", 422)
    result = {key: text(values.get(key, ""), limit) for key, limit in
              (("team_name", 200), ("contact", 500), ("category", 100), ("comment", 2000))}
    if result["comment"] and "comment" not in schema["optional_fields"]:
        raise AccessError("invalid_application_data", 422)
    players = values.get("players", [])
    if not isinstance(players, list) or len(players) > 100:
        raise AccessError("invalid_roster_size", 422)
    clean_players, seen_ids, self_count = [], set(), 0
    from uuid import UUID
    for position, player in enumerate(players):
        if not isinstance(player, dict) or set(player) - (PLAYER_FIELDS | {"id", "is_self"}):
            raise AccessError("invalid_application_data", 422)
        clean = {key: text(player.get(key, ""), 255 if key == "full_name" else 64 if key == "student_number"
                           else 100 if key == "study_group" else 500) for key in PLAYER_FIELDS}
        for field in ("full_name", "study_group"):
            if clean[field] and field not in schema["optional_fields"]:
                raise AccessError("invalid_application_data", 422)
        if "id" in player:
            try:
                identifier = str(UUID(player["id"]))
            except (ValueError, TypeError, AttributeError):
                raise AccessError("invalid_application_data", 422) from None
            if identifier in seen_ids:
                raise AccessError("invalid_application_data", 422)
            seen_ids.add(identifier)
            clean["id"] = identifier
        is_self = player.get("is_self", schema["participation_format"] == "individual")
        if type(is_self) is not bool:
            raise AccessError("invalid_application_data", 422)
        clean["is_self"] = is_self
        self_count += int(is_self)
        clean_players.append(clean)
    result["players"] = clean_players
    errors = {}
    if complete:
        if not result["contact"].strip():
            errors["contact"] = "Укажите контакт для связи."
        if schema["participation_format"] == "team" and not result["team_name"].strip():
            errors["team_name"] = "Укажите название команды."
        if schema["participation_format"] == "individual" and (len(players) != 1 or not clean_players[0]["is_self"]):
            errors["players"] = "Индивидуальная заявка содержит одного участника — её владельца."
        minimum = schema["min_players"] or 1
        maximum = schema["max_players"] or 100
        if not minimum <= len(players) <= maximum or self_count > 1:
            errors["players"] = "Проверьте число игроков и запись капитана в составе."
        if schema["categories"] and result["category"] not in schema["categories"]:
            errors["category"] = "Выберите категорию из списка мероприятия."
        if not schema["categories"] and result["category"]:
            errors["category"] = "На мероприятии нет категорий."
        for position, player in enumerate(clean_players):
            if not player["student_number"].strip():
                errors[f"players.{position}.student_number"] = "Укажите номер студенческого как текст."
            try:
                parsed = urlsplit(player["vk_url"])
                valid_url = (parsed.scheme == "https" and parsed.hostname in {"vk.com", "vk.ru", "www.vk.com", "www.vk.ru"}
                             and parsed.port in (None, 443) and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
                             and bool(parsed.path.strip("/")) and "/" not in parsed.path.strip("/"))
            except ValueError:
                valid_url = False
            if not valid_url:
                errors[f"players.{position}.vk_url"] = "Укажите ссылку на профиль ВК."
        if errors:
            raise AccessError("application_validation_failed", 422, field_errors=errors)
    return result
