from django.core.management.base import BaseCommand
from django.utils import timezone

from workboard.recurring_service import RecurringTaskService
from workboard.task_views import _backfill_orphan_recurring_tasks


class Command(BaseCommand):
    help = "Generate task instances from recurring templates that are due."

    def handle(self, *args, **options):
        now = timezone.now()
        _backfill_orphan_recurring_tasks()
        created_count, reopened_count = RecurringTaskService.run_templates_ready_today(now=now)
        self.stdout.write(self.style.SUCCESS(f"Generated {created_count} recurring task(s) and reopened {reopened_count} recurring task(s)."))
