from django.db import migrations, models


def populate_display_order(apps, schema_editor):
    RecurringTaskTemplate = apps.get_model("workboard", "RecurringTaskTemplate")
    templates = list(RecurringTaskTemplate.objects.order_by("next_run_date", "title", "pk"))
    for index, template in enumerate(templates, start=1):
        template.display_order = index
        template.save(update_fields=["display_order"])


class Migration(migrations.Migration):

    dependencies = [
        ("workboard", "0010_merge_assigned_into_new"),
    ]

    operations = [
        migrations.AddField(
            model_name="recurringtasktemplate",
            name="display_order",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(populate_display_order, migrations.RunPython.noop),
    ]
