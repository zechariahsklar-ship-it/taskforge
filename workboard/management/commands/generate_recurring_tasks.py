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
        reopened_count = 0
        templates = RecurringTaskTemplate.objects.filter(active=True, next_run_date__lte=today).prefetch_related("additional_assignees", "generated_tasks")
        for template in templates:
            last_generated_task = template.generated_tasks.order_by("-created_at", "-pk").first()
            if last_generated_task and last_generated_task.status != TaskStatus.DONE:
                continue

            next_order = (Task.objects.filter(status=TaskStatus.NEW).aggregate(max_order=Max("board_order")).get("max_order") or 0) + 1
            exclude_user_ids = []
            if last_generated_task and last_generated_task.assigned_to_id and User.objects.filter(role__in=UserRole.worker_roles(), worker_profile__active_status=True).exclude(pk=last_generated_task.assigned_to_id).exists():
                exclude_user_ids = [last_generated_task.assigned_to_id]

            default_assignee = template.assign_to if last_generated_task is None else None
            assigned_to = default_assignee
            if assigned_to and template.scheduled_start_time and template.scheduled_end_time and not TaskAssignmentService.user_is_available_for_window(
                assigned_to,
                scheduled_date=template.next_run_date,
                scheduled_start_time=template.scheduled_start_time,
                scheduled_end_time=template.scheduled_end_time,
                exclude_task_id=last_generated_task.pk if last_generated_task else None,
            ):
                assigned_to = None
                default_assignee = None
            if assigned_to is None:
                assigned_to, _, _ = TaskAssignmentService.suggest_assignee(
                    due_date=template.next_run_date,
                    estimated_minutes=template.estimated_minutes,
                    fallback_supervisor=template.requested_by,
                    exclude_user_ids=exclude_user_ids,
                    scheduled_date=template.next_run_date if template.scheduled_start_time and template.scheduled_end_time else None,
                    scheduled_start_time=template.scheduled_start_time,
                    scheduled_end_time=template.scheduled_end_time,
                    exclude_task_id=last_generated_task.pk if last_generated_task else None,
                )
            assigned_to = assigned_to or default_assignee or template.requested_by

            fixed_additional_ids = list(template.additional_assignees.exclude(pk=getattr(assigned_to, "pk", None)).values_list("pk", flat=True))
            previous_rotating_user_id = last_generated_task.rotating_additional_assignee_id if last_generated_task else None
            rotating_assignee = None
            if template.rotate_additional_assignee:
                rotating_exclude_ids = set(fixed_additional_ids)
                if assigned_to:
                    rotating_exclude_ids.add(assigned_to.pk)
                if previous_rotating_user_id and User.objects.filter(role__in=UserRole.worker_roles(), worker_profile__active_status=True).exclude(pk__in=list(rotating_exclude_ids) + [previous_rotating_user_id]).exists():
                    rotating_exclude_ids.add(previous_rotating_user_id)
                rotating_assignee = TaskAssignmentService.suggest_worker_assignee(
                    due_date=template.next_run_date,
                    estimated_minutes=template.estimated_minutes,
                    exclude_user_ids=list(rotating_exclude_ids),
                    scheduled_date=template.next_run_date if template.scheduled_start_time and template.scheduled_end_time else None,
                    scheduled_start_time=template.scheduled_start_time,
                    scheduled_end_time=template.scheduled_end_time,
                    exclude_task_id=last_generated_task.pk if last_generated_task else None,
                )

            if last_generated_task:
                task = last_generated_task
                task.title = template.title
                task.description = template.description
                task.priority = template.priority
                task.status = TaskStatus.NEW
                task.due_date = template.next_run_date
                task.scheduled_date = template.next_run_date if template.scheduled_start_time and template.scheduled_end_time else None
                task.scheduled_start_time = template.scheduled_start_time
                task.scheduled_end_time = template.scheduled_end_time
                task.estimated_minutes = template.estimated_minutes
                task.assigned_to = assigned_to
                task.requested_by = template.requested_by
                task.recurring_task = True
                task.recurring_template = template
                task.recurrence_pattern = template.recurrence_pattern
                task.recurrence_interval = template.recurrence_interval
                task.recurrence_day_of_week = template.day_of_week
                task.recurrence_day_of_month = template.day_of_month
                task.rotate_additional_assignee = template.rotate_additional_assignee
                task.rotating_additional_assignee = rotating_assignee
                task.board_order = next_order
                task.completed_at = None
                task.save()
                task.additional_assignees.set(fixed_additional_ids)
                task.checklist_items.update(is_completed=False)
                reopened_count += 1
            else:
                task = Task.objects.create(
                    title=template.title,
                    description=template.description,
                    priority=template.priority,
                    status=TaskStatus.NEW,
                    due_date=template.next_run_date,
                    scheduled_date=template.next_run_date if template.scheduled_start_time and template.scheduled_end_time else None,
                    scheduled_start_time=template.scheduled_start_time,
                    scheduled_end_time=template.scheduled_end_time,
                    estimated_minutes=template.estimated_minutes,
                    assigned_to=assigned_to,
                    rotating_additional_assignee=rotating_assignee,
                    requested_by=template.requested_by,
                    recurring_task=True,
                    recurring_template=template,
                    recurrence_pattern=template.recurrence_pattern,
                    recurrence_interval=template.recurrence_interval,
                    recurrence_day_of_week=template.day_of_week,
                    recurrence_day_of_month=template.day_of_month,
                    rotate_additional_assignee=template.rotate_additional_assignee,
                    board_order=next_order,
                )
                if fixed_additional_ids:
                    task.additional_assignees.set(fixed_additional_ids)
                created_count += 1

            template.advance_next_run_date()
            template.save(update_fields=["next_run_date", "updated_at"])

        self.stdout.write(self.style.SUCCESS(f"Generated {created_count} recurring task(s) and reopened {reopened_count} recurring task(s)."))
