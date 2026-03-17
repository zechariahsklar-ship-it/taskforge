from django.core.management.base import BaseCommand
from django.utils import timezone

from django.db.models import Max

from workboard.models import RecurringTaskTemplate, Task, TaskStatus, User, UserRole
from workboard.services import TaskAssignmentService


class Command(BaseCommand):
    help = "Generate task instances from recurring templates that are due."

    def handle(self, *args, **options):
        today = timezone.localdate()
        created_count = 0
        templates = RecurringTaskTemplate.objects.filter(active=True, next_run_date__lte=today)
        for template in templates:
            next_order = (Task.objects.filter(status=TaskStatus.NEW).aggregate(max_order=Max("board_order")).get("max_order") or 0) + 1
            last_generated_task = template.generated_tasks.exclude(assigned_to__isnull=True).order_by("-created_at", "-pk").first()
            exclude_user_ids = []
            if last_generated_task and User.objects.filter(role__in=UserRole.worker_roles(), worker_profile__active_status=True).exclude(pk=last_generated_task.assigned_to_id).exists():
                exclude_user_ids = [last_generated_task.assigned_to_id]

            assigned_to = template.assign_to if last_generated_task is None else None
            if assigned_to is None:
                assigned_to, _, _ = TaskAssignmentService.suggest_assignee(
                    due_date=template.next_run_date,
                    estimated_minutes=template.estimated_minutes,
                    fallback_supervisor=template.requested_by,
                    exclude_user_ids=exclude_user_ids,
                )
            assigned_to = assigned_to or template.assign_to or template.requested_by

            Task.objects.create(
                title=template.title,
                description=template.description,
                priority=template.priority,
                status=TaskStatus.NEW,
                estimated_minutes=template.estimated_minutes,
                assigned_to=assigned_to,
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
