from django.db.models import Prefetch

from accounts.services import AccessError

from .models import Material, MaterialVersion
from .preparation import preparation_data
from .services import visible_materials, visible_versions


def materials(actor):
    return visible_materials(actor).order_by("-created_at", "-pk").prefetch_related(Prefetch(
        "versions", queryset=with_preparation(visible_versions(actor).select_related("created_by", "updated_by"))
        .defer("source_text").order_by("-updated_at", "-pk"),
    ))


def with_preparation(versions):
    return versions.select_related(
        "current_preparation__task", "current_preparation__snapshot__generation__task",
    ).defer("current_preparation__snapshot__prepared_text")


def read_draft(actor, material_id, version_id):
    version = with_preparation(visible_versions(actor).filter(material_id=material_id, pk=version_id)
                               .select_related("material", "created_by", "updated_by", "support_origin__ticket__dialog")).first()
    if version is None:
        raise AccessError("draft_not_found", 404)
    return version


def material_data(material):
    if "versions" in getattr(material, "_prefetched_objects_cache", {}):
        current = next((version.pk for version in material.versions.all() if version.is_published), None)
    else:
        current = MaterialVersion.objects.filter(material=material, is_published=True).values_list("pk", flat=True).first()
    return {"id": str(material.pk), "publication_revision": material.publication_revision,
            "created_at": material.created_at, "published_version_id": str(current) if current else None}


def draft_data(version):
    preparation = preparation_data(version.current_preparation)
    return {
        "id": str(version.pk), "material_id": str(version.material_id),
        "title": version.title, "description": version.description, "tags": version.tags,
        "source_type": version.source_type, "source_text": version.source_text,
        "draft_revision": version.draft_revision, "base_publication_revision": version.base_publication_revision,
        "needs_rebase": version.base_publication_revision != version.material.publication_revision,
        "created_by_id": str(version.created_by_id), "updated_by_id": str(version.updated_by_id),
        "created_at": version.created_at, "updated_at": version.updated_at, "editable": version.sealed_at is None,
        "is_published": version.is_published,
        "preparation_status": preparation["status"],
        "preparation": preparation,
    }
