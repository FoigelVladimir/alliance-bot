from uuid import uuid4

from django import template

from storage.services import database_time
from support.attachments import state_data

register = template.Library()
LABELS = {
    "photo": "Фото", "audio_message": "Голосовое сообщение", "doc": "Документ",
}
STATES = {
    "metadata_only": "Копия ещё не подготовлена",
    "preparing": "Идёт подготовка копии",
    "ready": "Копия готова",
    "unsupported": "Формат файла не поддерживается",
    "unavailable": "Файл недоступен",
}
REASONS = {
    "cache_expired": "Срок хранения копии истёк",
    "source_link_expired": "Срок действия ссылки ВК истёк",
    "source_unavailable": "Оригинал недоступен",
    "file_too_large": "Файл превышает допустимый размер",
    "fetch_budget_exhausted": "Не удалось получить файл. Можно повторить подготовку.",
}


@register.inclusion_tag("support/attachments.html", takes_context=True)
def support_attachments(context, message):
    attachments = list(message.attachments.all())
    entries = []
    if attachments:
        now = database_time()
        for attachment in attachments:
            data = state_data(attachment, now)
            entries.append({
                "data": data, "label": LABELS.get(attachment.attachment_type, "Вложение"),
                "status": REASONS.get(data["reason"], STATES[data["state"]]),
                "operation_id": uuid4(), "is_audio": attachment.verified_content_type == "audio/ogg",
            })
    return {"entries": entries, "csrf_token": context.get("csrf_token")}
