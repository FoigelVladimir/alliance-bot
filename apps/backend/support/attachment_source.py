import http.client
import ipaddress
import re
import socket
import time
from threading import Timer
from urllib.parse import urlsplit

from django.conf import settings

MAX_BYTES = 10 * 1024 * 1024
PREVIEW_TYPES = {"image/png", "image/jpeg", "audio/ogg"}


class SourceError(Exception):
    pass


def source_url(kind, data):
    if kind == "photo":
        sizes = data.get("sizes")
        sizes = sizes if isinstance(sizes, list) else []
        urls = [item["url"] for item in sizes if isinstance(item, dict) and isinstance(item.get("url"), str)]
        value = urls[-1] if urls else None
    else:
        value = data.get({"audio_message": "link_ogg", "doc": "url"}.get(kind, ""))
    return value if isinstance(value, str) and 0 < len(value) <= 4096 else None


def public_endpoint(url):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https" or parts.hostname not in settings.VK_ATTACHMENT_HOSTS
            or parts.port not in (None, 443) or parts.username is not None
            or parts.password is not None or parts.fragment
            or any(ord(char) <= 32 or ord(char) >= 127 or char == chr(92) for char in url)
        ):
            raise SourceError("source_disallowed")
    except ValueError:
        raise SourceError("source_disallowed") from None
    addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or (ip.version == 6 and ip.ipv4_mapped):
            raise SourceError("source_disallowed")
    if not addresses:
        raise OSError("No CDN address")
    return parts, addresses[0][4][0]


def download_source(attachment):
    url = (attachment.source_ref or {}).get("url")
    if not url:
        raise SourceError("source_unavailable")
    deadline = time.monotonic() + 15
    parts, address = public_endpoint(url)
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise TimeoutError
    conn = http.client.HTTPSConnection(parts.hostname, timeout=timeout)
    # Keep the hostname for TLS and HTTP; connect to the already validated address.
    conn._create_connection = lambda _endpoint, *args: socket.create_connection((address, 443), *args)
    timer = None
    try:
        conn.connect()
        transport = conn.sock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError

        def stop_transfer():
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = Timer(remaining, stop_transfer)
        timer.daemon = True
        timer.start()
        conn.request("GET", (parts.path or "/") + ("?" + parts.query if parts.query else ""))
        with conn.getresponse() as response:
            if response.status in (401, 403, 410):
                raise SourceError("source_link_expired")
            if response.status == 404:
                raise SourceError("source_unavailable")
            if response.status == 429 or response.status >= 500:
                raise OSError("Temporary CDN error")
            # Redirect destinations are intentionally never followed.
            if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
                raise SourceError("source_disallowed")
            data = bytearray()
            expected = response.length
            if expected is not None and expected > MAX_BYTES:
                raise SourceError("file_too_large")
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if transport.fileno() != -1:
                    transport.settimeout(remaining)
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise SourceError("file_too_large")
            if time.monotonic() >= deadline:
                raise TimeoutError
            if expected is not None and expected != len(data):
                raise OSError("Incomplete CDN response")
            return bytes(data)
    except http.client.HTTPException:
        raise OSError("Invalid CDN response") from None
    finally:
        if timer is not None:
            timer.cancel()
        conn.close()


def verified_type(kind, data):
    if len(data) > MAX_BYTES:
        raise SourceError("file_too_large")
    if kind == "photo":
        from io import BytesIO
        from PIL import Image

        try:
            with Image.open(BytesIO(data), formats=("PNG", "JPEG")) as image:
                if image.width * image.height > 20_000_000 or getattr(image, "n_frames", 1) != 1:
                    raise SourceError("unsupported_format")
                image.verify()
            with Image.open(BytesIO(data), formats=("PNG", "JPEG")) as image:
                image.load()
                return {"PNG": "image/png", "JPEG": "image/jpeg"}[image.format]
        except (ValueError, SyntaxError, OSError, Image.DecompressionBombError):
            pass
    if kind == "audio_message" and is_ogg_opus(data):
        return "audio/ogg"
    if kind == "doc" and re.match(rb"%PDF-[12]\.\d[\r\n]", data) and data.rstrip().endswith(b"%%EOF"):
        return "application/pdf"
    raise SourceError("unsupported_format")


def is_ogg_opus(data):
    offset, index, serial = 0, 0, None
    has_tags = has_audio = False
    while offset + 27 <= len(data):
        if data[offset:offset + 5] != b"OggS\0":
            return False
        flags = data[offset + 5]
        stream = data[offset + 14:offset + 18]
        sequence = int.from_bytes(data[offset + 18:offset + 22], "little")
        count = data[offset + 26]
        body = offset + 27 + count
        if not count or body > len(data) or sequence != index or flags & ~7:
            return False
        lacing = data[offset + 27:body]
        end = body + sum(lacing)
        if end > len(data):
            return False
        if index == 0:
            serial = stream
            if flags != 2 or count != 1 or lacing[0] < 19 or data[body:body + 8] != b"OpusHead":
                return False
        elif stream != serial or flags & 2:
            return False
        elif index == 1:
            if flags & 1 or lacing[0] < 8 or data[body:body + 8] != b"OpusTags":
                return False
            has_tags = True
            # Audio may share the comments page after the first complete packet.
            has_audio = any(length < 255 for length in lacing[:-1])
        else:
            has_audio = True
        if flags & 4:
            return end == len(data) and has_tags and has_audio
        offset, index = end, index + 1
    return False
