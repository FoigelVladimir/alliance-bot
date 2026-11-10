from uuid import UUID, uuid4

from django.http import HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from accounts.services import AccessError
from accounts.views import json_object, private
from support.services import support_actor

from . import queries, sources, views
from .table_sources import selection_catalog


def number(value, *, minimum=1, form=False):
    if form and isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not minimum <= value < 2**63:
        raise AccessError("invalid_request", 400)
    return value


@private
@require_http_methods(["POST"])
def attach(request, material_id, version_id):
    support_actor(request.session)
    views.check_ids(material_id, version_id)
    try:
        if request.content_type == "multipart/form-data":
            data = request.POST.dict()
            data.pop("csrfmiddlewaretoken", None)
            if (set(data) != {"client_operation_id", "expected_draft_revision"} or set(request.FILES) != {"source_file"}
                    or any(len(values) != 1 for _, values in request.POST.lists())
                    or len(request.FILES.getlist("source_file")) != 1):
                raise AccessError("invalid_request", 400)
            try:
                operation_id = UUID(data["client_operation_id"])
            except (ValueError, TypeError, AttributeError):
                raise AccessError("invalid_request", 400) from None
            revision = number(data["expected_draft_revision"], form=True)
            command, replayed = sources.upload_source(request.session, material_id, version_id, operation_id,
                                                      revision, request.FILES["source_file"])
        else:
            data = json_object(request, ["client_operation_id", "expected_draft_revision", "google_url"])
            revision = number(data["expected_draft_revision"], form=request.content_type != "application/json")
            command, replayed = sources.connect_google(request.session, material_id, version_id,
                                                      data["client_operation_id"], revision, data["google_url"])
    except AccessError as error:
        if views.as_json(request) or error.status not in (400, 409, 413, 422):
            raise
        return views.draft_page(request, queries.read_draft(support_actor(request.session), material_id, version_id), error=error)
    return views.command_response(request, command, replayed)


@private
@require_http_methods(["GET", "POST"])
def table_selection(request, material_id, version_id):
    actor = support_actor(request.session)
    views.check_ids(material_id, version_id)
    version = queries.read_draft(actor, material_id, version_id)
    sources.raw_access(actor, version)
    error = None
    if request.method == "POST":
        try:
            if request.content_type == "application/x-www-form-urlencoded":
                data = request.POST.dict()
                data.pop("csrfmiddlewaretoken", None)
                required = {"client_operation_id", "expected_draft_revision", "source_snapshot_id"}
                if (not required <= set(data) or any(len(values) != 1 for _, values in request.POST.lists())):
                    raise AccessError("invalid_request", 400)
                selection = []
                choices = {}
                for key in data.keys() - required:
                    parts = key.split(".")
                    if len(parts) != 3 or parts[0] != "column" or data[key] != "on":
                        raise AccessError("invalid_request", 400)
                # QueryDict preserves the form's visible sheet/column order.
                for key in request.POST:
                    if key.startswith("column."):
                        _, sheet, column = key.split(".")
                        if sheet not in choices:
                            choices[sheet] = []
                            selection.append({"sheet": sheet, "columns": choices[sheet]})
                        choices[sheet].append(column)
                try:
                    data["client_operation_id"] = UUID(data["client_operation_id"])
                except (ValueError, AttributeError):
                    raise AccessError("invalid_request", 400) from None
                data["selection"] = selection
            else:
                data = json_object(request, ["client_operation_id", "expected_draft_revision", "source_snapshot_id", "selection"])
            revision = number(data["expected_draft_revision"], form=request.content_type != "application/json")
            snapshot_id = number(data["source_snapshot_id"], form=True)
            command, replayed = sources.select_table(request.session, material_id, version_id, data["client_operation_id"],
                                                     revision, snapshot_id, data["selection"])
        except AccessError as caught:
            if views.as_json(request) or caught.status not in (400, 409, 422):
                raise
            error = caught
            version = queries.read_draft(actor, material_id, version_id)
        else:
            return views.command_response(request, command, replayed)
    if not version.current_table_snapshot_id:
        if error:
            return views.draft_page(request, version, error=error)
        raise AccessError("source_snapshot_not_found", 404)
    catalog = selection_catalog(version.current_table_snapshot, version.table_selection)
    if views.as_json(request):
        return JsonResponse({"draft_revision": version.draft_revision, **catalog})
    selected = {key for choice in catalog["selection"] for key in choice["columns"]}
    for sheet in catalog["sheets"]:
        for column in sheet["columns"]:
            column["selected"] = column["key"] in selected
    return views.render_page(request, "table_selection", {
        "version": version, "catalog": catalog, "operation_id": uuid4(),
        "error_message": views.ERROR_MESSAGES.get(error.code, "Выбор недоступен. Проверьте текущий снимок.") if error else None,
    }, status=error.status if error else 200)


@private
@require_http_methods(["POST"])
def refresh_google(request, material_id, version_id):
    support_actor(request.session)
    views.check_ids(material_id, version_id)
    data = json_object(request, ["client_operation_id", "expected_publication_revision"])
    revision = number(data["expected_publication_revision"], minimum=0, form=request.content_type != "application/json")
    try:
        command, replayed = sources.refresh_google(request.session, material_id, version_id, data["client_operation_id"], revision)
    except AccessError as error:
        if views.as_json(request) or error.status != 409:
            raise
        return views.draft_page(request, queries.read_draft(support_actor(request.session), material_id, version_id), error=error)
    return views.command_response(request, command, replayed, created=True)
