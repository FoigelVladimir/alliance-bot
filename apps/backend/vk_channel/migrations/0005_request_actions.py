import django.db.models.deletion
from django.db import migrations, models


def guard_reverse(apps, schema_editor):
    alias = schema_editor.connection.alias
    if (apps.get_model("vk_channel", "StudentAction").objects.using(alias).filter(request__isnull=False).exists()
            or apps.get_model("support", "AIRequest").objects.using(alias).filter(resolved_at__isnull=False).exists()
            or apps.get_model("support", "AIPartDelivery").objects.using(alias).filter(vk_keyboard__isnull=False).exists()):
        raise RuntimeError("Request actions cannot be downgraded without losing their history; restore a verified backup in quarantine.")


class Migration(migrations.Migration):
    dependencies = [("vk_channel", "0004_history_budget"), ("support", "0019_request_resolution")]

    operations = [
        migrations.AlterField("studentaction", "ticket", models.ForeignKey(
            null=True, on_delete=django.db.models.deletion.PROTECT, to="support.ticket",
        )),
        migrations.AddField("studentaction", "request", models.ForeignKey(
            null=True, on_delete=django.db.models.deletion.PROTECT, to="support.airequest",
        )),
        migrations.AddField("studentaction", "request_version", models.PositiveIntegerField(null=True)),
        migrations.AddConstraint("studentaction", models.CheckConstraint(
            condition=models.Q(ticket__isnull=False, request__isnull=True, request_version__isnull=True,
                               action__in=("resolve", "call_manager", "continue"))
            | models.Q(ticket__isnull=True, request__isnull=False, request_version__isnull=False, request_version__gte=1,
                       action__in=("resolve", "call_manager")),
            name="vk_student_action_target",
        )),
        migrations.RunPython(migrations.RunPython.noop, guard_reverse),
    ]
