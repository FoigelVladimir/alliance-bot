import django.db.models.deletion
from django.db import migrations, models


def protect_context(apps, schema_editor):
    if apps.get_model("support", "AIContextResolution").objects.using(schema_editor.connection.alias).exists():
        raise RuntimeError("Cannot remove saved context decisions; migrate their history first.")


class Migration(migrations.Migration):
    dependencies = [("support", "0021_ai_join_deadline")]

    operations = [
        migrations.CreateModel(name="AIContextResolution", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("status", models.CharField(default="pending", max_length=16)),
            ("decision", models.JSONField(default=dict)),
            ("model_snapshot", models.JSONField(default=dict)),
            ("created_at", models.DateTimeField()),
            ("processing_deadline_at", models.DateTimeField()),
            ("finished_at", models.DateTimeField(null=True)),
            ("route", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
                                          related_name="context_resolution", to="support.inboundroute")),
            ("task", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to="jobs.backgroundtask")),
            ("run", models.OneToOneField(null=True, on_delete=django.db.models.deletion.PROTECT,
                                        related_name="context_resolution", to="support.aiprocessingrun")),
            ("request", models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT,
                                         related_name="context_resolutions", to="support.airequest")),
            ("resolved_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT,
                                             related_name="clarified", to="support.aicontextresolution")),
        ], options={"db_table": "support_ai_context_resolution",
            "constraints": [models.CheckConstraint(
                condition=models.Q(status="pending", finished_at__isnull=True, run__isnull=True, request__isnull=True)
                | models.Q(status__in=["applied", "clarification", "failed", "superseded"], finished_at__isnull=False),
                name="support_ai_context_outcome")],
            "indexes": [models.Index(fields=["processing_deadline_at", "id"],
                condition=models.Q(status="pending"), name="support_ai_context_deadline")] }),
        migrations.RunPython(migrations.RunPython.noop, protect_context),
    ]
