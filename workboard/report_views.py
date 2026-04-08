from datetime import timedelta

from django.db.models import Sum
from django.shortcuts import render
from django.utils import timezone

from .assignment_service import TaskAssignmentService
from .models import RecurringTaskTemplate, StudentWorkerProfile, Task, TaskAuditAction, TaskAuditEvent, TaskStatus, UserRole
from .task_views import _scope_queryset_to_user_team, supervisor_required


def _current_week_bounds(today):
    week_start = today - timedelta(days=today.weekday())
    return week_start, week_start + timedelta(days=6)


def _hours_from_minutes(minutes):
    return round(minutes / 60, 1)


@supervisor_required
def reports_view(request):
    today = timezone.localdate()
    week_start, week_end = _current_week_bounds(today)

    visible_tasks = _scope_queryset_to_user_team(Task.objects.all(), request.user)
    open_tasks = visible_tasks.exclude(status=TaskStatus.DONE)
    completed_this_week = visible_tasks.filter(completed_at__date__gte=week_start, completed_at__date__lte=week_end).count()
    overdue_open = open_tasks.filter(due_date__lt=today).count()
    waiting_open = open_tasks.filter(status=TaskStatus.WAITING).count()
    active_recurring_templates = _scope_queryset_to_user_team(RecurringTaskTemplate.objects.filter(active=True), request.user).count()
    recurring_due_soon = _scope_queryset_to_user_team(
        RecurringTaskTemplate.objects.filter(active=True, next_run_date__lte=today + timedelta(days=2)),
        request.user,
    ).count()
    recurring_runs_this_week = _scope_queryset_to_user_team(
        TaskAuditEvent.objects.filter(
            action=TaskAuditAction.RECURRING_RUN,
            created_at__date__gte=week_start,
            created_at__date__lte=week_end,
        ),
        request.user,
        field_name="task__team",
    ).count()

    workload_rows = []
    total_scheduled_minutes = 0
    total_assigned_minutes = 0
    profiles = StudentWorkerProfile.objects.select_related("user").filter(user__role__in=UserRole.worker_roles(), active_status=True)
    profiles = _scope_queryset_to_user_team(profiles, request.user, field_name="user__team").order_by("display_name")
    for profile in profiles:
        open_work = _scope_queryset_to_user_team(TaskAssignmentService.active_task_queryset_for_user(profile.user), request.user)
        open_estimated_minutes = open_work.aggregate(total=Sum("estimated_minutes")).get("total") or 0
        scheduled_minutes = TaskAssignmentService.scheduled_minutes_for_range(profile, week_start, week_end)
        completed_count = _scope_queryset_to_user_team(
            Task.objects.filter(
                TaskAssignmentService.task_membership_filter(profile.user),
                completed_at__date__gte=week_start,
                completed_at__date__lte=week_end,
            ).distinct(),
            request.user,
        ).count()
        total_scheduled_minutes += scheduled_minutes
        total_assigned_minutes += open_estimated_minutes
        workload_rows.append(
            {
                "profile": profile,
                "open_tasks": open_work.count(),
                "open_estimated_minutes": open_estimated_minutes,
                "open_estimated_hours": _hours_from_minutes(open_estimated_minutes),
                "scheduled_hours": _hours_from_minutes(scheduled_minutes),
                "completed_this_week": completed_count,
                "load_percent": round((open_estimated_minutes / scheduled_minutes) * 100) if scheduled_minutes else None,
            }
        )

    summary_cards = [
        {"label": "Completed this week", "value": completed_this_week},
        {"label": "Open overdue tasks", "value": overdue_open},
        {"label": "Waiting / blocked", "value": waiting_open},
        {"label": "Active recurring templates", "value": active_recurring_templates},
        {"label": "Recurring runs this week", "value": recurring_runs_this_week},
        {"label": "Scheduled hours this week", "value": _hours_from_minutes(total_scheduled_minutes)},
    ]

    return render(
        request,
        "workboard/reports.html",
        {
            "today": today,
            "week_start": week_start,
            "week_end": week_end,
            "summary_cards": summary_cards,
            "workload_rows": workload_rows,
            "recurring_due_soon": recurring_due_soon,
            "total_assigned_hours": _hours_from_minutes(total_assigned_minutes),
        },
    )
