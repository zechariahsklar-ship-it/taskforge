from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("workboard", "0011_recurringtasktemplate_display_order"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="assignable_to_tasks",
            field=models.BooleanField(default=True),
        ),
    ]
