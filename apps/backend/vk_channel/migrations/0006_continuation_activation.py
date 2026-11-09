from django.db import migrations, models


def guard_reverse(apps, schema_editor):
    if apps.get_model("vk_channel", "StudentAction").objects.using(schema_editor.connection.alias).filter(awaiting_close=True).exists():
        raise RuntimeError("Pending continuation actions cannot be downgraded without losing activation state.")


class Migration(migrations.Migration):
    dependencies = [("vk_channel", "0005_request_actions")]

    operations = [
        migrations.AddField("studentaction", "awaiting_close", models.BooleanField(default=False)),
        migrations.AddConstraint("studentaction", models.CheckConstraint(
            condition=models.Q(awaiting_close=False)
            | models.Q(awaiting_close=True, ticket__isnull=False, request__isnull=True,
                       action="continue", expected_closed_at__isnull=True, used_at__isnull=True),
            name="vk_continuation_waits_for_close",
        )),
        migrations.RunPython(migrations.RunPython.noop, guard_reverse),
    ]
