from dataclasses import dataclass
from datetime import date

from django.db.models import Max
from django.utils import timezone

from .audit_service import TaskAuditService
from .models import RecurringTaskTemplate, Task, TaskStatus, User, UserRole
from .services import TaskAssignmentService


@dataclass
class RecurringRunPreview:
    run_date: date
    assignee: User | None
    assignee_summary: str
    fixed_additional_ids: list[int]
    rotating_assignees: list[User]
    current_task_open: bool


class RecurringTaskService:
    @staticmethod
    def upcoming_run_dates(template: RecurringTaskTemplate, *, count: int = 5) -> list[date]:
        if count <= 0:
            return []
        preview_template = RecurringTaskTemplate(
            recurrence_pattern=template.recurrence_pattern,
            recurrence_interval=template.recurrence_interval,
            day_of_week=template.day_of_week,
            day_of_month=template.day_of_month,
            start_date=template.start_date,
            next_run_date=template.next_run_date,
        )
        run_dates = []
        for _ in range(count):
            run_dates.append(preview_template.next_run_date)
            preview_template.advance_next_run_date()
        return run_dates

    @staticmethod
    def _last_generated_task(template: RecurringTaskTemplate):
        return template.generated_tasks.order_by('-created_at', '-pk').first()

    @staticmethod
    def _exclude_recent_primary_assignee(last_generated_task) -> list[int]:
        if not last_generated_task or not last_generated_task.assigned_to_id:
            return []
        if not User.objects.filter(role__in=UserRole.worker_roles(), worker_profile__active_status=True).exclude(pk=last_generated_task.assigned_to_id).exists():
            return []
        return [last_generated_task.assigned_to_id]

    @staticmethod
    def _previous_rotating_user_ids(last_generated_task) -> list[int]:
        if not last_generated_task:
            return []
        rotating_ids = list(last_generated_task.rotating_additional_assignees.values_list('pk', flat=True))
        if not rotating_ids and last_generated_task.rotating_additional_assignee_id:
            rotating_ids = [last_generated_task.rotating_additional_assignee_id]
        return rotating_ids

    @staticmethod
    def preview_next_run(template: RecurringTaskTemplate, *, run_date: date | None = None) -> RecurringRunPreview:
        run_date = run_date or template.next_run_date
        last_generated_task = RecurringTaskService._last_generated_task(template)
        current_task_open = bool(last_generated_task and last_generated_task.status != TaskStatus.DONE)

        assigned_to = template.assign_to
        assignee_summary = ''
        exclude_task_id = last_generated_task.pk if last_generated_task else None
        scheduled_date = run_date if template.scheduled_start_time and template.scheduled_end_time else None

        if assigned_to:
            if scheduled_date and not TaskAssignmentService.user_is_available_for_window(
                assigned_to,
                scheduled_date=scheduled_date,
                scheduled_start_time=template.scheduled_start_time,
                scheduled_end_time=template.scheduled_end_time,
                exclude_task_id=exclude_task_id,
            ):
                assignee_summary = f'{assigned_to.display_label} is the fixed assignee, but that scheduled window is currently unavailable.'
            else:
                assignee_summary = f'{assigned_to.display_label} is the fixed assignee for the next run.'
        else:
            excluded_user_ids = RecurringTaskService._exclude_recent_primary_assignee(last_generated_task)
            suggested_user, summary, _ = TaskAssignmentService.suggest_assignee(
                due_date=run_date,
                estimated_minutes=template.estimated_minutes,
                fallback_supervisor=template.requested_by,
                exclude_user_ids=excluded_user_ids,
                scheduled_date=scheduled_date,
                scheduled_start_time=template.scheduled_start_time,
                scheduled_end_time=template.scheduled_end_time,
                exclude_task_id=exclude_task_id,
            )
            assigned_to = suggested_user
            assignee_summary = summary

        assigned_to = assigned_to or template.requested_by
        fixed_additional_ids = list(template.additional_assignees.exclude(pk=getattr(assigned_to, 'pk', None)).values_list('pk', flat=True))
        rotating_assignees = TaskAssignmentService.suggest_worker_assignees(
            due_date=run_date,
            estimated_minutes=template.estimated_minutes,
            count=template.rotating_additional_assignee_count,
            exclude_user_ids=list(set(fixed_additional_ids + ([assigned_to.pk] if assigned_to else []))),
            avoid_user_ids=RecurringTaskService._previous_rotating_user_ids(last_generated_task),
            scheduled_date=scheduled_date,
            scheduled_start_time=template.scheduled_start_time,
            scheduled_end_time=template.scheduled_end_time,
            exclude_task_id=exclude_task_id,
        )
        return RecurringRunPreview(
            run_date=run_date,
            assignee=assigned_to,
            assignee_summary=assignee_summary,
            fixed_additional_ids=fixed_additional_ids,
            rotating_assignees=rotating_assignees,
            current_task_open=current_task_open,
        )

    @staticmethod
    def run_template(template: RecurringTaskTemplate, *, run_date: date | None = None, force: bool = False):
        run_date = run_date or template.next_run_date
        last_generated_task = RecurringTaskService._last_generated_task(template)
        if last_generated_task and last_generated_task.status != TaskStatus.DONE:
            return None, 'skipped'

        preview = RecurringTaskService.preview_next_run(template, run_date=run_date)
        next_order = (Task.objects.filter(status=TaskStatus.NEW).aggregate(max_order=Max('board_order')).get('max_order') or 0) + 1
        rotating_assignees = preview.rotating_assignees

        if last_generated_task:
            task = last_generated_task
            task.title = template.title
            task.description = template.description
            task.priority = template.priority
            task.status = TaskStatus.NEW
            task.due_date = run_date
            task.scheduled_date = run_date if template.scheduled_start_time and template.scheduled_end_time else None
            task.scheduled_start_time = template.scheduled_start_time
            task.scheduled_end_time = template.scheduled_end_time
            task.estimated_minutes = template.estimated_minutes
            task.assigned_to = preview.assignee
            task.requested_by = template.requested_by
            task.recurring_task = True
            task.recurring_template = template
            task.recurrence_pattern = template.recurrence_pattern
            task.recurrence_interval = template.recurrence_interval
            task.recurrence_day_of_week = template.day_of_week
            task.recurrence_day_of_month = template.day_of_month
            task.rotating_additional_assignee_count = template.rotating_additional_assignee_count
            task.rotate_additional_assignee = template.rotating_additional_assignee_count > 0
            task.rotating_additional_assignee = rotating_assignees[0] if rotating_assignees else None
            task.board_order = next_order
            task.completed_at = None
            task.save()
            task.additional_assignees.set(preview.fixed_additional_ids)
            task.rotating_additional_assignees.set([user.pk for user in rotating_assignees])
            task.checklist_items.update(is_completed=False)
            TaskAuditService.record_recurring_reopened(task, summary=f'Prepared recurring task for {run_date.isoformat()}.')
            outcome = 'reopened'
        else:
            task = Task.objects.create(
                title=template.title,
                description=template.description,
                priority=template.priority,
                status=TaskStatus.NEW,
                due_date=run_date,
                scheduled_date=run_date if template.scheduled_start_time and template.scheduled_end_time else None,
                scheduled_start_time=template.scheduled_start_time,
                scheduled_end_time=template.scheduled_end_time,
                estimated_minutes=template.estimated_minutes,
                assigned_to=preview.assignee,
                rotating_additional_assignee_count=template.rotating_additional_assignee_count,
                rotating_additional_assignee=rotating_assignees[0] if rotating_assignees else None,
                requested_by=template.requested_by,
                recurring_task=True,
                recurring_template=template,
                recurrence_pattern=template.recurrence_pattern,
                recurrence_interval=template.recurrence_interval,
                recurrence_day_of_week=template.day_of_week,
                recurrence_day_of_month=template.day_of_month,
                rotate_additional_assignee=template.rotating_additional_assignee_count > 0,
                board_order=next_order,
            )
            if preview.fixed_additional_ids:
                task.additional_assignees.set(preview.fixed_additional_ids)
            task.rotating_additional_assignees.set([user.pk for user in rotating_assignees])
            TaskAuditService.record_recurring_reopened(task, summary=f'Generated recurring task for {run_date.isoformat()}.')
            outcome = 'created'

        template.next_run_date = run_date
        template.advance_next_run_date()
        template.save(update_fields=['next_run_date', 'updated_at'])
        return task, outcome

    @staticmethod
    def run_due_templates(*, run_date: date | None = None) -> tuple[int, int]:
        run_date = run_date or timezone.localdate()
        created_count = 0
        reopened_count = 0
        templates = RecurringTaskTemplate.objects.filter(active=True, next_run_date__lte=run_date).prefetch_related('additional_assignees', 'generated_tasks')
        for template in templates:
            _, outcome = RecurringTaskService.run_template(template, run_date=template.next_run_date)
            if outcome == 'created':
                created_count += 1
            elif outcome == 'reopened':
                reopened_count += 1
        return created_count, reopened_count
