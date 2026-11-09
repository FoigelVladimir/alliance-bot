import django.db.models.deletion
from django.db import migrations, models


def protect_review_history(apps, schema_editor):
    alias = schema_editor.connection.alias
    if (apps.get_model("support", "AIAnswerReview").objects.using(alias).exists()
            or apps.get_model("support", "AIAnswerReviewEvent").objects.using(alias).exists()):
        raise RuntimeError("Cannot remove saved answer reviews or their history; archive and migrate them first.")


class Migration(migrations.Migration):
    dependencies = [("support", "0019_request_resolution"),
                    ("accounts", "0002_accesscontrol_quarantine_incident_and_more")]

    operations = [
        migrations.CreateModel(
            name="AIAnswerReview",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("problem_flag", models.BooleanField(default=False)),
                ("comment", models.CharField(blank=True, default="", max_length=2000)),
                ("revision", models.PositiveBigIntegerField(default=1)),
                ("flagged_at", models.DateTimeField(null=True)),
                ("updated_at", models.DateTimeField()),
                ("answer", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
                                               related_name="quality_review", to="support.aianswer")),
                ("flagged_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT,
                                                related_name="+", to="accounts.staffprofile")),
                ("updated_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                                related_name="+", to="accounts.staffprofile")),
            ],
            options={"db_table": "support_ai_answer_review", "constraints": [
                models.CheckConstraint(condition=models.Q(revision__gte=1), name="support_ai_review_revision"),
                models.CheckConstraint(
                    condition=models.Q(problem_flag=True, flagged_by__isnull=False, flagged_at__isnull=False)
                    | models.Q(problem_flag=False, flagged_by__isnull=True, flagged_at__isnull=True),
                    name="support_ai_review_flag_author"),
            ]},
        ),
        migrations.CreateModel(
            name="AIAnswerReviewEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("client_operation_id", models.UUIDField(unique=True)),
                ("expected_revision", models.PositiveBigIntegerField()),
                ("resulting_revision", models.PositiveBigIntegerField()),
                ("request_payload", models.JSONField()),
                ("before_state", models.JSONField()),
                ("after_state", models.JSONField()),
                ("created_at", models.DateTimeField()),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                           related_name="+", to="accounts.staffprofile")),
                ("review", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                            related_name="events", to="support.aianswerreview")),
            ],
            options={"db_table": "support_ai_answer_review_event", "constraints": [
                models.UniqueConstraint(fields=["review", "resulting_revision"], name="support_ai_review_event_unique"),
                models.CheckConstraint(condition=models.Q(resulting_revision=models.F("expected_revision") + 1),
                                       name="support_ai_review_event_revision"),
            ], "indexes": [models.Index(fields=["review", "resulting_revision"], name="support_ai_review_history")]},
        ),
        # Reversal runs this guard before either table can be dropped.
        migrations.RunPython(migrations.RunPython.noop, protect_review_history),
    ]
