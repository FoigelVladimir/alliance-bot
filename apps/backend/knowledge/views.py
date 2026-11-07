from uuid import uuid4

from django.core.exceptions import RequestDataTooBig
from django.core.paginator import Paginator
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from accounts.services import AccessError
from accounts.views import json_object, private
from support.queries import allowed_communities
from support.services import support_actor

from . import preparation, publication, queries, services
from .models import Material, MaterialVersion

ERROR_MESSAGES = {
    "draft_revision_conflict": "Черновик изменил другой сотрудник. Сравните сохранённый текст с вашим вводом перед повторным сохранением.",
    "material_not_ready": "Дождитесь готовности индекса и снова откройте снимок.",
    "draft_base_changed": "Публикация изменилась. Сверьте черновик с текущей версией.",
    "publication_revision_conflict": "Публикация изменилась. Проверьте текущую версию.",
    "prepared_snapshot_changed": "Подготовленный снимок изменился. Проверьте новый текст.",
    "published_version_changed": "Действующая версия изменилась. Проверьте текущую публикацию.",
    "support_message_not_found": "Выберите вопрос и подтверждённый ответ из этого обращения.",
    "preparation_in_progress": "Эта правка уже готовится. Обновите страницу, чтобы проверить результат.",
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
            data["expected_draft_revision"] = expected_revision(data)
    return data, {
        "title": data["title"], "description": data.get("description", ""), "tags": tags,
        "source_type": data.get("source_type", "text"), "source_text": data.get("source_text", ""),
    }


def expected_revision(data):
    value = data["expected_draft_revision"]
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        return int(value)
    raise AccessError("invalid_request", 400)


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
    page = Paginator(queries.materials(support_actor(request.session)), 20).get_page(request.GET.get("page", "1"))
    for material in page:
        for version in material.versions.all():
            version.preparation_state = preparation.preparation_data(version.current_preparation)
    if as_json(request) and error is None:
        return JsonResponse({"materials": [
            {**queries.material_data(material), "drafts": [
                {"id": str(version.pk), "title": version.title, "draft_revision": version.draft_revision,
                 "updated_at": version.updated_at, "editable": version.sealed_at is None,
                 "preparation_status": version.preparation_state["status"],
                 "index_status": version.preparation_state.get("index_status", "not_indexed")}
                for version in material.versions.all()
            ]} for material in page
        ], "page": page.number, "total_count": page.paginator.count})
    return render_page(request, "materials", {
        "materials": page, "operation_id": uuid4(), "form_values": form_values(request=request) if error else form_values(),
        "error_message": ERROR_MESSAGES.get(error.code, "Действие недоступно.") if error else None,
    }, status=error.status if error else 200)


def command_response(request, command, replayed, *, created=False):
    version = queries.read_draft(support_actor(request.session), command.material_id, command.version_id)
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
    material = services.visible_materials(support_actor(request.session)).filter(pk=material_id).first()
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
    version = queries.read_draft(support_actor(request.session), material_id, version_id)
    if as_json(request):
        return JsonResponse({"material": queries.material_data(version.material), "draft": queries.draft_data(version)})
    return draft_page(request, version, error=error, save_conflict=bool(error))


def draft_page(request, version, *, error=None, save_conflict=False):
    state = preparation.preparation_data(version.current_preparation)
    snapshot = version.current_preparation.snapshot if state["status"] == "source_prepared" else None
    origin = getattr(version, "support_origin", None)
    origin_ticket_id = None
    if origin and allowed_communities(support_actor(request.session)).filter(cursor_id=origin.ticket.dialog.cursor_id).exists():
        origin_ticket_id = origin.ticket_id
    current = None
    if version.base_publication_revision != version.material.publication_revision and version.sealed_at is None:
        current = MaterialVersion.objects.filter(material=version.material, is_published=True).select_related(
            "published_prepared_snapshot",
        ).first()
    return render_page(request, "draft", {
        "material": version.material, "version": version, "operation_id": uuid4(), "prepare_operation_id": uuid4(),
        "publication_operation_id": uuid4(), "rebase_operation_id": uuid4(), "current_publication": current, "origin_ticket_id": origin_ticket_id,
        "preparation": state, "snapshot": snapshot,
        "form_values": form_values(request=request) if save_conflict else form_values(version),
        "error_message": ERROR_MESSAGES.get(error.code, "Действие недоступно.") if error else None,
        "show_comparison": bool(save_conflict and error.code == "draft_revision_conflict"),
    }, status=error.status if error else 200)


@private
@require_http_methods(["POST"])
def prepare(request, material_id, version_id):
    support_actor(request.session)
    check_ids(material_id, version_id)
    try:
        data = json_object(request, ["client_operation_id", "expected_draft_revision"])
        revision = expected_revision(data) if request.content_type == "application/x-www-form-urlencoded" else data["expected_draft_revision"]
        record, replayed = preparation.request_preparation(
            request.session, material_id, version_id, data["client_operation_id"], revision,
        )
    except AccessError as error:
        if as_json(request) or error.status not in (400, 409, 422):
            raise
        return draft_page(request, queries.read_draft(support_actor(request.session), material_id, version_id), error=error)
    version = queries.read_draft(support_actor(request.session), material_id, version_id)
    if as_json(request):
        state = preparation.preparation_data(record)
        return JsonResponse({
            "operation": {"client_operation_id": str(data["client_operation_id"]), "action": "prepare", "replayed": replayed},
            "preparation": state, "draft": queries.draft_data(version),
        }, status=202 if state["status"] == "preparing" else 200)
    return HttpResponseRedirect(reverse("knowledge-draft", args=[material_id, version_id]), status=303)


def command_number(request, data, key, *, minimum=0):
    value = data[key]
    if isinstance(value, str) and (request.content_type == "application/x-www-form-urlencoded" or key.endswith("_id")):
        if not (value.isascii() and value.isdecimal() and len(value) <= 19):
            raise AccessError("invalid_request", 400)
        value = int(value)
    publication.validate_revision(value, minimum=minimum)
    return value


@private
@require_http_methods(["POST"])
def publication_command(request, material_id, action):
    support_actor(request.session)
    check_ids(material_id)
    required = ["client_operation_id", "version_id", "expected_publication_revision"]
    if action == "publish":
        required.append("prepared_snapshot_id")
    data = json_object(request, required)
    version_id = command_number(request, data, "version_id", minimum=1)
    revision = command_number(request, data, "expected_publication_revision")
    snapshot_id = command_number(request, data, "prepared_snapshot_id", minimum=1) if action == "publish" else None
    try:
        event, replayed = publication.change_publication(
            request.session, material_id, version_id, data["client_operation_id"], revision, snapshot_id=snapshot_id,
        )
    except AccessError as error:
        if as_json(request) or error.status != 409:
            raise
        return draft_page(request, queries.read_draft(support_actor(request.session), material_id, version_id), error=error)
    if as_json(request):
        return JsonResponse({
            "operation": {"client_operation_id": str(data["client_operation_id"]), "action": action, "replayed": replayed},
            "event": {"id": str(event.pk), "publication_revision": event.publication_revision,
                      "previous_version_id": str(event.previous_version_id) if event.previous_version_id else None,
                      "next_version_id": str(event.next_version_id) if event.next_version_id else None,
                      "prepared_snapshot_id": str(event.snapshot_id), "created_at": event.created_at},
            "material": queries.material_data(Material.objects.get(pk=material_id)),
        })
    return HttpResponseRedirect(reverse("knowledge-draft", args=[material_id, version_id]), status=303)


@private
@require_http_methods(["POST"])
def rebase(request, material_id, version_id):
    support_actor(request.session)
    check_ids(material_id, version_id)
    data = json_object(request, ["client_operation_id", "expected_draft_revision", "expected_publication_revision"])
    draft_revision = command_number(request, data, "expected_draft_revision", minimum=1)
    publication_revision = command_number(request, data, "expected_publication_revision")
    try:
        command, replayed = publication.rebase_draft(
            request.session, material_id, version_id, data["client_operation_id"], draft_revision, publication_revision,
        )
    except AccessError as error:
        if as_json(request) or error.status != 409:
            raise
        return draft_page(request, queries.read_draft(support_actor(request.session), material_id, version_id), error=error)
    return command_response(request, command, replayed)


@private
@require_http_methods(["GET", "POST"])
def from_support(request):
    actor = support_actor(request.session)
    if request.method == "GET":
        value = request.GET.get("ticket_id", "")
        if not (value.isascii() and value.isdecimal() and len(value) <= 19):
            raise AccessError("invalid_request", 400)
        ticket_id = int(value)
        check_ids(ticket_id)
        ticket = services.support_ticket(actor, ticket_id)
        questions, answers = services.eligible_messages(ticket)
        return render_page(request, "from_support", {
            "ticket": ticket, "questions": questions.order_by("-pk")[:100], "answers": answers.order_by("-pk")[:100],
            "materials": services.visible_materials(actor).order_by("-pk")[:100], "operation_id": uuid4(),
        })
    data = json_object(request, ["client_operation_id", "ticket_id", "question_id", "answer_id"], ["material_id"])
    ticket_id = command_number(request, data, "ticket_id", minimum=1)
    question_id = command_number(request, data, "question_id", minimum=1)
    answer_id = command_number(request, data, "answer_id", minimum=1)
    material_id = command_number(request, data, "material_id", minimum=1) if data.get("material_id") not in ("", None) else None
    command, replayed = services.create_from_support(
        request.session, data["client_operation_id"], ticket_id, question_id, answer_id, material_id,
    )
    return command_response(request, command, replayed, created=True)
