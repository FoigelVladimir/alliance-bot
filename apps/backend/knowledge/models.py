from uuid import uuid4

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from django.db.models import Q
from django.utils import timezone
from pgvector.django import VectorField


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
    current_preparation = models.ForeignKey(
        "MaterialPreparation", null=True, on_delete=models.PROTECT, related_name="+",
    )
    sealed_at = models.DateTimeField(null=True)
    is_published = models.BooleanField(default=False)
    published_prepared_snapshot = models.ForeignKey(
        "SourceSnapshot", null=True, on_delete=models.PROTECT, related_name="+",
    )
    publication_verification_required = models.BooleanField(default=False)

    class Meta:
        db_table = "kb_document_version"
        constraints = [
            models.CheckConstraint(condition=Q(draft_revision__gt=0), name="kb_draft_positive_revision"),
            models.CheckConstraint(condition=Q(source_type="text"), name="kb_version_supported_source"),
            models.UniqueConstraint(fields=["material"], condition=Q(is_published=True),
                                    name="kb_one_published_version"),
            models.CheckConstraint(
                condition=Q(is_published=True, published_prepared_snapshot__isnull=False, sealed_at__isnull=False)
                | Q(is_published=False, published_prepared_snapshot__isnull=True),
                name="kb_publication_has_snapshot",
            ),
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
            models.CheckConstraint(condition=Q(action__in=["create_material", "create_draft", "save_draft", "prepare", "publish", "unpublish", "rebase", "from_support"]),
                                   name="kb_command_known_action"),
        ]


class MaterialPreparation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    command = models.OneToOneField(MaterialCommand, on_delete=models.PROTECT)
    version = models.ForeignKey(MaterialVersion, on_delete=models.PROTECT)
    draft_revision = models.PositiveBigIntegerField()
    task = models.OneToOneField("jobs.BackgroundTask", on_delete=models.PROTECT)

    class Meta:
        db_table = "kb_preparation"


class SourceSnapshot(models.Model):
    preparation = models.OneToOneField(MaterialPreparation, on_delete=models.PROTECT, related_name="snapshot")
    prepared_text = models.TextField()
    source_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "kb_source_snapshot"


class FragmentGeneration(models.Model):
    snapshot = models.OneToOneField(SourceSnapshot, on_delete=models.PROTECT, related_name="generation")
    task = models.OneToOneField("jobs.BackgroundTask", on_delete=models.PROTECT)
    model_revision = models.CharField(max_length=40)
    fragment_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "kb_fragment_generation"


class DocumentFragment(models.Model):
    document = models.ForeignKey(Material, on_delete=models.PROTECT)
    version = models.ForeignKey(MaterialVersion, on_delete=models.PROTECT)
    prepared_snapshot = models.ForeignKey(SourceSnapshot, on_delete=models.PROTECT, related_name="fragments")
    position = models.PositiveIntegerField()
    heading = models.CharField(max_length=200)
    content = models.TextField()
    search_vector = models.GeneratedField(
        expression=SearchVector("heading", "content", config="russian"),
        output_field=SearchVectorField(), db_persist=True,
    )
    embedding = VectorField(dimensions=384)
    access_level = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=False)

    class Meta:
        db_table = "kb_document_fragment"
        constraints = [
            models.UniqueConstraint(fields=["prepared_snapshot", "position"], name="kb_fragment_snapshot_position"),
        ]
        indexes = [
            GinIndex(fields=["search_vector"], condition=Q(is_active=True), name="kb_active_fragment_text"),
        ]


class PublicationEvent(models.Model):
    command = models.OneToOneField(MaterialCommand, on_delete=models.PROTECT)
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    previous_version = models.ForeignKey(MaterialVersion, null=True, on_delete=models.PROTECT, related_name="+")
    next_version = models.ForeignKey(MaterialVersion, null=True, on_delete=models.PROTECT, related_name="+")
    snapshot = models.ForeignKey(SourceSnapshot, on_delete=models.PROTECT)
    publication_revision = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "kb_publication_event"
        constraints = [
            models.UniqueConstraint(fields=["material", "publication_revision"], name="kb_publication_event_revision"),
        ]


class SupportOrigin(models.Model):
    version = models.OneToOneField(MaterialVersion, on_delete=models.PROTECT, related_name="support_origin")
    ticket = models.ForeignKey("support.Ticket", on_delete=models.PROTECT)
    question = models.ForeignKey("support.Message", on_delete=models.PROTECT, related_name="+")
    answer = models.ForeignKey("support.Message", on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "kb_support_origin"
