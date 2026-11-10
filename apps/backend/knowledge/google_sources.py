"""Explicit read-only Sheets snapshots; never called from student retrieval."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from django.conf import settings

from .extractors import MAX_CELLS, MAX_COLUMNS, MAX_ROWS, MAX_SHEETS, TABLE_TEXT_LIMIT, XML_LIMIT, cell_text
from .table_sources import SourceError

SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


def spreadsheet_id(value):
    if not isinstance(value, str):
        raise SourceError("invalid_google_url")
    match = re.fullmatch(r"https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{10,160})(?:/[^?#]*)?(?:[?#].*)?", value.strip())
    if not match:
        raise SourceError("invalid_google_url")
    return match[1]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, *, method="GET", body=None, headers=None):
    try:
        with build_opener(NoRedirect).open(Request(url, data=body, headers=headers or {}, method=method), timeout=10) as response:
            raw = response.read(XML_LIMIT + 1)
            if len(raw) > XML_LIMIT:
                raise SourceError("google_source_too_large")
            return response.status, raw, dict(response.headers)
    except HTTPError as error:
        if error.code in (401, 403, 404):
            raise SourceError("google_access_denied") from None
        raise SourceError("google_unavailable") from None
    except (URLError, TimeoutError, OSError):
        raise SourceError("google_unavailable") from None


def read_google(identifier):
    if not settings.KNOWLEDGE_GOOGLE_ENABLED or not settings.GOOGLE_SERVICE_ACCOUNT_FILE:
        raise SourceError("google_not_configured")
    from google.oauth2.service_account import Credentials
    from google.auth.transport import Response

    class TokenResponse(Response):
        def __init__(self, status, raw, headers):
            self._status, self._data, self._headers = status, raw, headers

        status = property(lambda self: self._status)
        data = property(lambda self: self._data)
        headers = property(lambda self: self._headers)

    def token_request(url, method="GET", body=None, headers=None, **kwargs):
        if url != "https://oauth2.googleapis.com/token":
            raise SourceError("invalid_google_credentials")
        return TokenResponse(*fetch(url, method=method, body=body, headers=headers))

    try:
        credentials = Credentials.from_service_account_file(settings.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=[SCOPE])
        credentials.refresh(token_request)
    except SourceError:
        raise
    except Exception:
        raise SourceError("google_credentials_unavailable") from None
    # Formatting and effective values are intentionally absent: formulas never become ordinary data.
    fields = "spreadsheetId,sheets(properties,merges,basicFilter,filterViews,data(startRow,startColumn,rowData(values(userEnteredValue)),rowMetadata(hiddenByUser,hiddenByFilter),columnMetadata(hiddenByUser)))"
    from urllib.parse import urlencode
    _, raw, _ = fetch("https://sheets.googleapis.com/v4/spreadsheets/" + identifier + "?" + urlencode({"includeGridData": "true", "fields": fields}),
                      headers={"Authorization": "Bearer " + credentials.token})
    try:
        return normalize(json.loads(raw), identifier)
    except (ValueError, TypeError, KeyError, IndexError):
        raise SourceError("invalid_google_response") from None


def normalize(document, identifier):
    if document.get("spreadsheetId") != identifier or not isinstance(document.get("sheets"), list):
        raise SourceError("invalid_google_response")
    if len(document["sheets"]) > MAX_SHEETS:
        raise SourceError("table_limits_exceeded")
    result, total, text_bytes = [], 0, 0
    seen_ids = set()
    for sheet in document["sheets"]:
        props = sheet["properties"]
        if props.get("sheetType", "GRID") != "GRID":
            raise SourceError("unsupported_table_structure")
        identity = props["sheetId"]
        if type(identity) is not int or identity in seen_ids:
            raise SourceError("invalid_google_response")
        seen_ids.add(identity)
        grids = sheet.get("data", [])
        if len(grids) > 1:
            raise SourceError("unsupported_table_structure")
        grid = grids[0] if grids else {}
        if grid.get("startRow", 0) or grid.get("startColumn", 0):
            raise SourceError("unsupported_table_structure")
        rows = grid.get("rowData", [])
        width = max((len(row.get("values", [])) for row in rows), default=0)
        total += len(rows) * width
        if len(rows) > MAX_ROWS or width > MAX_COLUMNS or total > MAX_CELLS:
            raise SourceError("table_limits_exceeded")
        parsed = []
        for row in rows:
            cells = []
            for value in row.get("values", []):
                entered = value.get("userEnteredValue", {})
                formula = "formulaValue" in entered
                plain = next((entered[key] for key in ("stringValue", "numberValue", "boolValue") if key in entered), "")
                text = "" if formula else cell_text(plain)
                text_bytes += len(text.encode("utf-8"))
                if text_bytes > TABLE_TEXT_LIMIT:
                    raise SourceError("table_limits_exceeded")
                cells.append({"text": text, "formula": formula})
            cells.extend({"text": "", "formula": False} for _ in range(width - len(cells)))
            parsed.append(cells)
        column_meta = grid.get("columnMetadata", [])
        columns = [{"key": uuid4().hex, "index": index,
                    "header": parsed[0][index]["text"] if parsed else "",
                    "hidden": bool(column_meta[index].get("hiddenByUser")) if index < len(column_meta) else False}
                   for index in range(width)]
        blocked = "merged_cells" if sheet.get("merges") else ""
        if any(item.get("hiddenByUser") or item.get("hiddenByFilter") for item in grid.get("rowMetadata", [])):
            blocked = "hidden_rows"
        result.append({"key": uuid4().hex, "identity": identity, "title": cell_text(props["title"]),
                       "hidden": bool(props.get("hidden")), "columns": columns,
                       "rows": parsed[1:], "blocked_reason": blocked})
    return {"schema": 1, "source_type": "google", "source_id": identifier, "sheets": result}
