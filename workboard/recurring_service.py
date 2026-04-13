from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.db.models import Max
from django.utils import timezone

from .audit_service import TaskAuditService
from .models import RecurringTaskTemplate, Task, TaskChecklistItem, TaskStatus, User, UserRole
from .services import TaskAssignmentService

RECURRING_RELEASE_TIME = time(18, 0)


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
    def _release_deadline(run_date: date) -> datetime:
        return timezone.make_aware(datetime.combine(run_date, RECURRING_RELEASE_TIME))

    @staticmethod
    def _previous_run_date(template: RecurringTaskTemplate, run_date: date) -> date:
        if template.recurrence_pattern == "daily":
            return run_date - timedelta(days=template.recurrence_interval)
        if template.recurrence_pattern == "weekly":
            return run_date - timedelta(weeks=template.recurrence_interval)
        month_index = run_date.month - 1 - template.recurrence_interval
        year = run_date.year + month_index // 12
        month = month_index % 12 + 1
        day = template.day_of_month or run_date.day
        return date(year, month, min(day, monthrange(year, month)[1]))

    @staticmethod
    def _run_is_ready(template: RecurringTaskTemplate, *, local_now: datetime) -> bool:
        # next_run_date stores the due date of the upcoming cycle. Release that
        # cycle once the prior scheduled due day has crossed the evening cutoff.
        release_date = RecurringTaskService._previous_run_date(template, template.next_run_date)
        return local_now >= RecurringTaskService._release_deadline(release_date)

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
    def _exclude_recent_primary_assignee(last_generated_task, *, team=None) -> list[int]:
        if not last_generated_task or not last_generated_task.assigned_to_id:
            return []
        queryset = User.objects.filter(role__in=UserRole.worker_roles(), worker_profile__active_status=True)
        if team is not None:
            queryset = queryset.filter(team=team)
        if not queryset.exclude(pk=last_generated_task.assigned_to_id).exists():
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
    def _scheduled_run_date(template: RecurringTaskTemplate, run_date: date) -> date | None:
        if template.scheduled_start_time and template.scheduled_end_time:
            return run_date
        return None

    @staticmethod
    def _next_new_task_order(*, team) -> int:
        return (
            Task.objects.filter(status=TaskStatus.NEW, team=team)
            .aggregate(max_order=Max('board_order'))
            .get('max_order')
            or 0
        ) + 1

    @staticmethod
    def _copy_source_task_context(source_task: Task | None, task: Task, *, template: RecurringTaskTemplate) -> None:
        task.created_by = source_task.created_by if source_task and source_task.created_by_id else template.requested_by
        task.raw_message = source_task.raw_message if source_task else ""
        task.waiting_person = source_task.waiting_person if source_task else ""
        task.respond_to_text = source_task.respond_to_text if source_task else ""

    @staticmethod
    def _copy_checklist_items(source_task: Task | None, task: Task) -> None:
        if source_task is None:
            return
        for item in source_task.checklist_items.order_by('position', 'pk'):
            TaskChecklistItem.objects.create(
                task=task,
                title=item.title,
                is_completed=False,
                position=item.position,
            )

    @staticmethod
    def _apply_template_to_task(
        task: Task,
        template: RecurringTaskTemplate,
        *,
        run_date: date,
        assignee: User | None,
        rotating_assignees: list[User],
        board_order: int,
        source_task: Task | None = None,
    ) -> Task:
        task.team = template.team
        task.title = template.title
        task.description = template.description
        task.priority = template.priority
        task.status = TaskStatus.NEW
        task.due_date = run_date
        task.scheduled_date = RecurringTaskService._scheduled_run_date(template, run_date)
        task.scheduled_start_time = template.scheduled_start_time
        task.scheduled_end_time = template.scheduled_end_time
        task.estimated_minutes = template.estimated_minutes
        task.assigned_to = assignee
        task.requested_by = template.requested_by
        RecurringTaskService._copy_source_task_context(source_task, task, template=template)
        task.recurring_task = True
        task.recurring_template = template
        task.recurrence_pattern = template.recurrence_pattern
        task.recurrence_interval = template.recurrence_interval
        task.recurrence_day_of_week = template.day_of_week
        task.recurrence_day_of_month = template.day_of_month
        task.rotating_additional_assignee_count = template.rotating_additional_assignee_count
        task.rotate_additional_assignee = template.rotating_additional_assignee_count > 0
        task.rotating_additional_assignee = rotating_assignees[0] if rotating_assignees else None
        task.board_order = board_order
        task.completed_at = None
        return task

    @staticmethod
    def _sync_generated_task_memberships(task: Task, *, fixed_additional_ids: list[int], rotating_assignees: list[User]) -> None:
        task.additional_assignees.set(fixed_additional_ids)
        task.rotating_additional_assignees.set([user.pk for user in rotating_assignees])

    @staticmethod
    def preview_next_run(template: RecurringTaskTemplate, *, run_date: date | None = None) -> RecurringRunPreview:
        run_date = run_date or template.next_run_date
        last_generated_task = RecurringTaskService._last_generated_task(template)
        current_task_open = bool(last_generated_task and last_generated_task.status != TaskStatus.DONE)

        assigned_to = template.assign_to
        assignee_summary = ''
        scheduled_date = RecurringTaskService._scheduled_run_date(template, run_date)

        if assigned_to:
            if scheduled_date and not TaskAssignmentService.user_is_available_for_window(
                assigned_to,
                scheduled_date=scheduled_date,
                scheduled_start_time=template.scheduled_start_time,
                scheduled_end_time=template.scheduled_end_time,
            ):
                assignee_summary = f'{assigned_to.display_label} is the fixed assignee, but that scheduled window is currently unavailable.'
            else:
                assignee_summary = f'{assigned_to.display_label} is the fixed assignee for the next run.'
        else:
            excluded_user_ids = RecurringTaskService._exclude_recent_primary_assignee(last_generated_task, team=template.team)
            suggested_user, summary, _ = TaskAssignmentService.suggest_assignee(
                due_date=run_date,
                estimated_minutes=template.estimated_minutes,
                fallback_supervisor=template.requested_by,
                exclude_user_ids=excluded_user_ids,
                scheduled_date=scheduled_date,
                scheduled_start_time=template.scheduled_start_time,
                scheduled_end_time=template.scheduled_end_time,
                team=template.team,
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
            team=template.team,
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
    def run_template(template: RecurringTaskTemplate, *, run_date: date | None = None):
        run_date = run_date or template.next_run_date
        last_generated_task = RecurringTaskService._last_generated_task(template)
        preview = RecurringTaskService.preview_next_run(template, run_date=run_date)
        next_order = RecurringTaskService._next_new_task_order(team=template.team)
        rotating_assignees = preview.rotating_assignees

        # Each recurring cycle becomes its own task so unfinished runs can stay
        # visible while the next scheduled cycle is still released on time.
        task = RecurringTaskService._apply_template_to_task(
            Task(),
            template,
            run_date=run_date,
            assignee=preview.assignee,
            rotating_assignees=rotating_assignees,
            board_order=next_order,
            source_task=last_generated_task,
        )
        task.save()
        RecurringTaskService._sync_generated_task_memberships(
            task,
            fixed_additional_ids=preview.fixed_additional_ids,
            rotating_assignees=rotating_assignees,
        )
        RecurringTaskService._copy_checklist_items(last_generated_task, task)
        TaskAuditService.record_recurring_reopened(task, summary=f'Generated recurring task for {run_date.isoformat()}.')

        template.next_run_date = run_date
        template.advance_next_run_date()
        template.save(update_fields=['next_run_date', 'updated_at'])
        return task, 'created'

    @staticmethod
    def run_templates_ready_today(*, now=None) -> tuple[int, int]:
        local_now = timezone.localtime(now or timezone.now())
        created_count = 0
        reopened_count = 0
        templates = (
            RecurringTaskTemplate.objects.filter(active=True)
            .prefetch_related('additional_assignees', 'generated_tasks')
            .distinct()
        )
        for template in templates:
            while RecurringTaskService._run_is_ready(template, local_now=local_now):
                _, outcome = RecurringTaskService.run_template(template, run_date=template.next_run_date)
                if outcome == 'created':
                    created_count += 1
                elif outcome == 'reopened':
                    reopened_count += 1
        return created_count, reopened_count

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
