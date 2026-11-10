from uuid import uuid4

from django.db import migrations, models


def populate_ids(apps, schema_editor):
    profiles = apps.get_model("accounts", "StaffProfile").objects.using(schema_editor.connection.alias)
    for pk in profiles.filter(stable_id__isnull=True).values_list("pk", flat=True).iterator():
        profiles.filter(pk=pk, stable_id__isnull=True).update(stable_id=uuid4())


def refuse_identity_loss(apps, schema_editor):
    if apps.get_model("accounts", "StaffProfile").objects.using(schema_editor.connection.alias).exists():
        raise RuntimeError("Cannot discard persistent staff UUIDs; restore a verified backup and reconcile external records.")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_accesscontrol_quarantine_incident_and_more")]
    operations = [
        migrations.AddField(model_name="staffprofile", name="stable_id", field=models.UUIDField(null=True, unique=True, editable=False)),
        migrations.RunPython(populate_ids, migrations.RunPython.noop),
        migrations.AlterField(model_name="staffprofile", name="stable_id", field=models.UUIDField(default=uuid4, unique=True, editable=False)),
        migrations.RunPython(migrations.RunPython.noop, refuse_identity_loss),
    ]
