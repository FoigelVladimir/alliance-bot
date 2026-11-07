from django.db import models
from django.db.models import Q
from django.utils import timezone


class Material(models.Model):
    created_by = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    publication_revision = models.PositiveBigIntegerField(default=0)

    class Meta:
        db_table = "kb_document"


class MaterialVersion(models.Model):
    material = models.ForeignKey(Material, related_name="versions", on_delete=models.PROTECT)
    created_by = models.ForeignKey("accounts.StaffProfile", related_name="+", on_delete=models.PROTECT)
    updated_by = models.ForeignKey("accounts.StaffProfile", related_name="+", on_delete=models.PROTECT)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    tags = models.JSONField(default=list)
    source_type = models.CharField(max_length=16, default="text")
    source_text = models.TextField(blank=True)
    draft_revision = models.PositiveBigIntegerField(default=1)
    base_publication_revision = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    sealed_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "kb_document_version"
        constraints = [
            models.CheckConstraint(condition=Q(draft_revision__gt=0), name="kb_draft_positive_revision"),
            models.CheckConstraint(condition=Q(source_type="text"), name="kb_version_supported_source"),
        ]
        indexes = [models.Index(fields=["material", "-updated_at", "-id"], name="kb_material_versions")]


class MaterialCommand(models.Model):
    actor = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT)
    client_operation_id = models.UUIDField()
    action = models.CharField(max_length=24)
    request_fingerprint = models.CharField(max_length=64)
    material = models.ForeignKey(Material, null=True, on_delete=models.PROTECT)
    version = models.ForeignKey(MaterialVersion, null=True, on_delete=models.PROTECT)
    saved_draft_revision = models.PositiveBigIntegerField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "kb_command"
        constraints = [
            models.UniqueConstraint(fields=["actor", "client_operation_id"], name="kb_actor_operation_unique"),
            models.CheckConstraint(condition=Q(action__in=["create_material", "create_draft", "save_draft"]),
                                   name="kb_command_known_action"),
        ]
