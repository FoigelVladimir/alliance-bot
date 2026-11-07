from uuid import uuid4

from django.core.exceptions import RequestDataTooBig
from django.core.paginator import Paginator
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from accounts.services import AccessError
from accounts.views import json_object, private
from support.services import support_actor

from . import queries, services
from .models import Material

ERROR_MESSAGES = {
    "draft_revision_conflict": "Черновик изменил другой сотрудник. Сравните сохранённый текст с вашим вводом перед повторным сохранением.",
    "draft_sealed": "Этот снимок больше нельзя редактировать. Создайте отдельный черновик.",
    "operation_mismatch": "Этот идентификатор команды уже использован для другого действия. Проверьте данные и отправьте форму заново.",
    "invalid_content": "Укажите название до 200 символов и описание до 2000 символов.",
    "invalid_tags": "Можно указать до 20 меток, каждая длиной до 64 символов.",
    "text_too_large": "Текст превышает предел 64 КиБ UTF-8. Сократите его перед сохранением.",
    "unsupported_source_type": "В этом разделе пока поддерживается только обычный текст.",
    "invalid_request": "Не удалось прочитать форму. Проверьте поля и повторите сохранение.",
}


def as_json(request):
    return request.content_type == "application/json" or "application/json" in request.headers.get("Accept", "")


def check_ids(*values):
    if any(not 0 < value < 2**63 for value in values):
        raise AccessError("not_found", 404)


def content_command(request, *, saving=False):
    if request.content_type not in {"application/json", "application/x-www-form-urlencoded"}:
        raise AccessError("invalid_request", 400)
    try:
        if len(request.body) > 512 * 1024:
            raise AccessError("request_too_large", 413)
    except RequestDataTooBig:
        raise AccessError("request_too_large", 413) from None
    required = ["client_operation_id", "title"]
    if saving:
        required.append("expected_draft_revision")
    data = json_object(request, required, ["description", "tags", "source_type", "source_text"])
    tags = data.get("tags", [])
    if request.content_type == "application/x-www-form-urlencoded":
        tags = [tag.strip() for tag in tags.split(",") if tag.strip()] if isinstance(tags, str) else tags
        if saving:
            value = data["expected_draft_revision"]
            if not value.isascii() or not value.isdecimal() or len(value) > 19:
                raise AccessError("invalid_request", 400)
            data["expected_draft_revision"] = int(value)
    return data, {
        "title": data["title"], "description": data.get("description", ""), "tags": tags,
        "source_type": data.get("source_type", "text"), "source_text": data.get("source_text", ""),
    }


def form_values(version=None, request=None):
    if request is not None:
        return {name: request.POST.get(name, "") for name in ("title", "description", "tags", "source_text")}
    if version is None:
        return {"title": "", "description": "", "tags": "", "source_text": ""}
    return {"title": version.title, "description": version.description,
            "tags": ", ".join(version.tags), "source_text": version.source_text}


def render_page(request, template, context, *, status=200):
    response = render(request, f"knowledge/{template}.html", context, status=status)
    response["Referrer-Policy"] = "strict-origin"
    return response


def catalog_page(request, *, error=None):
    page = Paginator(queries.materials(), 20).get_page(request.GET.get("page", "1"))
    if as_json(request) and error is None:
        return JsonResponse({"materials": [
            {**queries.material_data(material), "drafts": [
                {"id": str(version.pk), "title": version.title, "draft_revision": version.draft_revision,
                 "updated_at": version.updated_at, "editable": version.sealed_at is None}
                for version in material.versions.all()
            ]} for material in page
        ], "page": page.number, "total_count": page.paginator.count})
    return render_page(request, "materials", {
        "materials": page, "operation_id": uuid4(), "form_values": form_values(request=request) if error else form_values(),
        "error_message": ERROR_MESSAGES.get(error.code, "Действие недоступно.") if error else None,
    }, status=error.status if error else 200)


def command_response(request, command, replayed, *, created=False):
    version = queries.read_draft(command.material_id, command.version_id)
    if as_json(request):
        return JsonResponse({
            "operation": {"client_operation_id": str(command.client_operation_id), "action": command.action,
                          "replayed": replayed, "saved_draft_revision": command.saved_draft_revision},
            "material": queries.material_data(version.material), "draft": queries.draft_data(version),
        }, status=200 if replayed or not created else 201)
    response = HttpResponseRedirect(reverse("knowledge-draft", args=[command.material_id, command.version_id]))
    response.status_code = 303
    return response


@private
@require_http_methods(["GET", "POST"])
def materials(request):
    support_actor(request.session)
    if request.method == "GET":
        return catalog_page(request)
    try:
        data, content = content_command(request)
        command, replayed = services.create_material(request.session, data["client_operation_id"], content)
    except AccessError as error:
        if as_json(request) or error.status not in (400, 409, 422):
            raise
        return catalog_page(request, error=error)
    return command_response(request, command, replayed, created=True)


@private
@require_http_methods(["GET", "POST"])
def new_draft(request, material_id):
    support_actor(request.session)
    check_ids(material_id)
    material = Material.objects.filter(pk=material_id).first()
    if material is None:
        raise AccessError("material_not_found", 404)
    error = None
    if request.method == "POST":
        try:
            data, content = content_command(request)
            command, replayed = services.create_draft(request.session, material_id, data["client_operation_id"], content)
        except AccessError as caught:
            if as_json(request) or caught.status not in (400, 409, 422):
                raise
            error = caught
        else:
            return command_response(request, command, replayed, created=True)
    return render_page(request, "draft", {
        "material": material, "new_draft": True, "operation_id": uuid4(),
        "form_values": form_values(request=request) if error else form_values(),
        "error_message": ERROR_MESSAGES.get(error.code, "Действие недоступно.") if error else None,
    }, status=error.status if error else 200)


@private
@require_http_methods(["GET", "POST"])
def draft(request, material_id, version_id):
    support_actor(request.session)
    check_ids(material_id, version_id)
    error = None
    if request.method == "POST":
        try:
            data, content = content_command(request, saving=True)
            command, replayed = services.save_draft(request.session, material_id, version_id,
                data["client_operation_id"], data["expected_draft_revision"], content)
        except AccessError as caught:
            if as_json(request) or caught.status not in (400, 409, 422):
                raise
            error = caught
        else:
            return command_response(request, command, replayed)
    version = queries.read_draft(material_id, version_id)
    if as_json(request):
        return JsonResponse({"material": queries.material_data(version.material), "draft": queries.draft_data(version)})
    return render_page(request, "draft", {
        "material": version.material, "version": version, "operation_id": uuid4(),
        "form_values": form_values(request=request) if error else form_values(version),
        "error_message": ERROR_MESSAGES.get(error.code, "Действие недоступно.") if error else None,
        "show_comparison": bool(error and error.code == "draft_revision_conflict"),
    }, status=error.status if error else 200)
