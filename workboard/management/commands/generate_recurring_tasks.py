from django.core.management.base import BaseCommand
from django.utils import timezone

from django.db.models import Max

from workboard.models import RecurringTaskTemplate, Task, TaskStatus


class Command(BaseCommand):
    help = "Generate task instances from recurring templates that are due."

    def handle(self, *args, **options):
        today = timezone.localdate()
        created_count = 0
        templates = RecurringTaskTemplate.objects.filter(active=True, next_run_date__lte=today)
        for template in templates:
            next_order = (Task.objects.filter(status=TaskStatus.NEW).aggregate(max_order=Max("board_order")).get("max_order") or 0) + 1
            Task.objects.create(
                title=template.title,
                description=template.description,
                priority=template.priority,
                status=TaskStatus.NEW,
                estimated_minutes=template.estimated_minutes,
                assigned_to=template.assign_to,
                requested_by=template.requested_by,
                recurring_task=True,
                recurring_template=template,
                recurrence_pattern=template.recurrence_pattern,
                recurrence_interval=template.recurrence_interval,
                recurrence_day_of_week=template.day_of_week,
                recurrence_day_of_month=template.day_of_month,
                board_order=next_order,
            )
            template.advance_next_run_date()
            template.save(update_fields=["next_run_date", "updated_at"])
            created_count += 1

        self.stdout.write(self.style.SUCCESS(f"Generated {created_count} recurring task(s)."))
