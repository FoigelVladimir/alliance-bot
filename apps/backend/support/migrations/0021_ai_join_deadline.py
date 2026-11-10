from django.db import migrations, models


def protect_join_state(apps, schema_editor):
    alias = schema_editor.connection.alias
    if (apps.get_model("support", "AIProcessingRun").objects.using(alias).filter(join_until__isnull=False).exists()
            or apps.get_model("support", "TicketEvent").objects.using(alias).filter(
                command__action="join_ai_dialog").exists()):
        raise RuntimeError("Cannot remove saved manager join windows or results; migrate them first.")


class Migration(migrations.Migration):
    dependencies = [("support", "0020_ai_answer_review")]

    operations = [
        # Historical results stay inactive; migration time must not open a fresh window.
        migrations.AddField("aiprocessingrun", "join_until", models.DateTimeField(null=True)),
        migrations.AddConstraint("aiprocessingrun", models.CheckConstraint(
            condition=models.Q(join_until__isnull=True)
            | (models.Q(status__in=["answer_saved", "no_answer"], finished_at__isnull=False,
                        join_until__gte=models.F("finished_at"))
               & ~models.Q(reason__in=["superseded", "recovered_from_backup"])),
            name="support_ai_join_deadline",
        )),
        migrations.RunPython(migrations.RunPython.noop, protect_join_state),
    ]
