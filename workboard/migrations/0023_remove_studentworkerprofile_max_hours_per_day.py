from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("workboard", "0022_team_recurringtasktemplate_team_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="studentworkerprofile",
            name="max_hours_per_day",
        ),
    ]