from django.core.management.base import BaseCommand
from django.utils import timezone

from workboard.recurring_service import RecurringTaskService


class Command(BaseCommand):
    help = "Generate task instances from recurring templates that are due."

    def handle(self, *args, **options):
        today = timezone.localdate()
        created_count, reopened_count = RecurringTaskService.run_due_templates(run_date=today)
        self.stdout.write(self.style.SUCCESS(f"Generated {created_count} recurring task(s) and reopened {reopened_count} recurring task(s)."))
