import base64
import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.utils.crypto import constant_time_compare

BASE_URL = "https://id.vk.com"


class VKLoginError(Exception):
    pass


@dataclass(frozen=True)
class VKIdentity:
    external_id: str
    display_name: str
    photo_url: str


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def authorize_url(state, verifier):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return f"{BASE_URL}/authorize?" + urlencode({
        "client_id": settings.VK_ID_CLIENT_ID,
        "redirect_uri": settings.VK_ID_REDIRECT_URI,
        "response_type": "code",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def post(endpoint, params, body):
    request = Request(f"{BASE_URL}/oauth2/{endpoint}?{urlencode(params)}", data=urlencode(body).encode())
    try:
        with build_opener(NoRedirect()).open(request, timeout=5) as response:
            data = json.loads(response.read(65536))
    except (OSError, ValueError):
        raise VKLoginError from None
    if not isinstance(data, dict) or "error" in data:
        raise VKLoginError
    return data


def exchange(code, device_id, state, verifier):
    token_data = post("auth", {
        "grant_type": "authorization_code",
        "client_id": settings.VK_ID_CLIENT_ID,
        "redirect_uri": settings.VK_ID_REDIRECT_URI,
        "code_verifier": verifier,
        "state": state,
        "device_id": device_id,
    }, {"code": code})
    token = token_data.get("access_token")
    returned_state = token_data.get("state")
    if not isinstance(token, str) or not token or not isinstance(returned_state, str):
        raise VKLoginError
    if not constant_time_compare(returned_state, state):
        raise VKLoginError
    user = post("user_info", {"client_id": settings.VK_ID_CLIENT_ID}, {"access_token": token}).get("user")
    if not isinstance(user, dict):
        raise VKLoginError
    external_id = str(user.get("user_id"))
    if not re.fullmatch(r"[1-9][0-9]{0,31}", external_id):
        raise VKLoginError
    names = (user.get("first_name"), user.get("last_name"))
    name = " ".join(value.strip() for value in names if isinstance(value, str))
    photo = user.get("avatar")
    try:
        photo = photo if isinstance(photo, str) and len(photo) <= 2048 and urlsplit(photo).scheme == "https" else ""
    except ValueError:
        photo = ""
    return VKIdentity(external_id, name[:255], photo)


def is_configured():
    try:
        uri = urlsplit(settings.VK_ID_REDIRECT_URI)
        return (
            settings.VK_ID_CLIENT_ID.isascii() and settings.VK_ID_CLIENT_ID.isdigit()
            and uri.scheme == "https" and bool(uri.hostname)
            and not (uri.username or uri.password or uri.query or uri.fragment)
            and uri.path == "/auth/vk/callback/"
        )
    except ValueError:
        return False
