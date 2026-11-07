from django.db.models import Prefetch

from accounts.services import AccessError

from .models import Material, MaterialVersion


def materials():
    return Material.objects.order_by("-created_at", "-pk").prefetch_related(Prefetch(
        "versions", queryset=MaterialVersion.objects.select_related("created_by", "updated_by").defer("source_text").order_by("-updated_at", "-pk"),
    ))


def read_draft(material_id, version_id):
    version = MaterialVersion.objects.filter(material_id=material_id, pk=version_id).select_related(
        "material", "created_by", "updated_by",
    ).first()
    if version is None:
        raise AccessError("draft_not_found", 404)
    return version


def material_data(material):
    return {"id": str(material.pk), "publication_revision": material.publication_revision,
            "created_at": material.created_at}


def draft_data(version):
    return {
        "id": str(version.pk), "material_id": str(version.material_id),
        "title": version.title, "description": version.description, "tags": version.tags,
        "source_type": version.source_type, "source_text": version.source_text,
        "draft_revision": version.draft_revision, "base_publication_revision": version.base_publication_revision,
        "needs_rebase": version.base_publication_revision != version.material.publication_revision,
        "created_by_id": str(version.created_by_id), "updated_by_id": str(version.updated_by_id),
        "created_at": version.created_at, "updated_at": version.updated_at, "editable": version.sealed_at is None,
        "preparation_status": "not_prepared",
    }
