from dataclasses import dataclass, field
import json
from http.client import HTTPException
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from accounts.vk_id_client import NoRedirect


class DeliveryUnknown(Exception):
    pass


class DeliveryRejected(Exception):
    def __init__(self, code):
        self.code = code


def call_vk_method(method, token, *, timeout=10, **params):
    request = Request(f"https://api.vk.com/method/{method}", data=urlencode({
        "access_token": token, "v": "5.199", **params,
    }).encode())
    return read_json(request, timeout=timeout)


def read_json(request, *, timeout):
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            payload = json.loads(response.read(512 * 1024))
    except (OSError, ValueError, HTTPException):
        raise DeliveryUnknown from None
    if not isinstance(payload, dict):
        raise DeliveryUnknown
    return payload


def send_message(token, peer_id, random_id, text, keyboard=None):
    params = {"peer_id": peer_id, "random_id": random_id, "message": text}
    if keyboard:
        params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
    payload = call_vk_method("messages.send", token, **params)
    error = payload.get("error")
    if isinstance(error, dict) and "response" not in payload:
        code = error.get("error_code")
        if type(code) is int and code in (901, 902):
            raise DeliveryRejected(code)
    message_id = payload.get("response")
    if "error" not in payload and type(message_id) is int and 0 < message_id < 2**63:
        return message_id
    raise DeliveryUnknown



def read_message_history(token, peer_id, *, timeout=10):
    payload = call_vk_method("messages.getHistory", token, peer_id=peer_id, count=50, timeout=timeout)
    result = payload.get("response")
    if "error" in payload or not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise DeliveryUnknown
    if len(result["items"]) > 50:
        raise DeliveryUnknown
    return result["items"]



@dataclass(frozen=True)
class LongPollServer:
    url: str
    position: str | int
    key: str = field(repr=False)


def long_poll_server(token, community_id):
    payload = call_vk_method("groups.getLongPollServer", token, group_id=community_id)
    server = payload.get("response")
    if "error" in payload or not isinstance(server, dict):
        raise DeliveryUnknown
    url, key = server.get("server"), server.get("key")
    if not isinstance(url, str) or not isinstance(key, str) or not 0 < len(key) <= 512:
        raise DeliveryUnknown
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        valid = (
            parsed.scheme == "https" and host.endswith((".vk.com", ".vk.ru"))
            and parsed.port in (None, 443) and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        )
    except ValueError:
        valid = False
    if not valid:
        raise DeliveryUnknown
    return LongPollServer(url, server.get("ts"), key)


def read_long_poll(server, position):
    query = urlencode({"act": "a_check", "key": server.key, "ts": position, "wait": 25})
    return read_json(Request(f"{server.url}?{query}"), timeout=35)


def read_conversations(token, *, timeout=10):
    payload = call_vk_method("messages.getConversations", token, count=50, timeout=timeout)
    result = payload.get("response")
    if "error" in payload or not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise DeliveryUnknown
    return result


def send_click_answer(token, event_id, user_id, peer_id, text):
    payload = call_vk_method(
        "messages.sendMessageEventAnswer", token, event_id=event_id, user_id=user_id, peer_id=peer_id,
        event_data=json.dumps({"type": "show_snackbar", "text": text}, ensure_ascii=False),
    )
    if "error" in payload or type(payload.get("response")) is not int or payload["response"] != 1:
        raise DeliveryUnknown
