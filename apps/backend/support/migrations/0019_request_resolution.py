from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("support", "0018_handoff_plan")]

    operations = [
        migrations.AddField("airequest", "resolved_at", models.DateTimeField(null=True)),
        migrations.AddField("airequest", "resolved_by_vk_id", models.BigIntegerField(null=True)),
        migrations.AddField("aipartdelivery", "vk_keyboard", models.JSONField(null=True)),
        migrations.AddConstraint("airequest", models.CheckConstraint(
            condition=models.Q(resolved_at__isnull=True, resolved_by_vk_id__isnull=True)
            | models.Q(resolved_at__isnull=False, resolved_by_vk_id__isnull=False, resolved_by_vk_id__gt=0),
            name="support_ai_resolution_pair",
        )),
    ]
