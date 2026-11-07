import json

from django.contrib.staticfiles.storage import staticfiles_storage
from django.utils.safestring import mark_safe


def assets(request):
    imports = {
        "alliance/api": staticfiles_storage.url("js/api-client.js"),
        "alliance/polling": staticfiles_storage.url("support/polling.js"),
        "alliance/draft-editor": staticfiles_storage.url("support/draft-editor.js"),
    }
    encoded = json.dumps({"imports": imports}).replace("<", "\\u003c")
    return {"import_map_json": mark_safe(encoded)}
