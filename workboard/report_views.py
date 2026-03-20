from datetime import timedelta

from django.db.models import Sum
from django.shortcuts import render
from django.utils import timezone

from .assignment_service import TaskAssignmentService
from .models import RecurringTaskTemplate, StudentWorkerProfile, Task, TaskAuditAction, TaskAuditEvent, TaskStatus, UserRole
from .task_views import supervisor_required


@supervisor_required
def reports_view(request):
    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)

    open_tasks = Task.objects.exclude(status=TaskStatus.DONE)
    completed_this_week = Task.objects.filter(completed_at__date__gte=week_start, completed_at__date__lte=week_end).count()
    overdue_open = open_tasks.filter(due_date__lt=today).count()
    waiting_open = open_tasks.filter(status=TaskStatus.WAITING).count()
    active_recurring_templates = RecurringTaskTemplate.objects.filter(active=True).count()
    recurring_due_soon = RecurringTaskTemplate.objects.filter(active=True, next_run_date__lte=today + timedelta(days=2)).count()
    recurring_runs_this_week = TaskAuditEvent.objects.filter(
        action=TaskAuditAction.RECURRING_RUN,
        created_at__date__gte=week_start,
        created_at__date__lte=week_end,
    ).count()

    workload_rows = []
    total_scheduled_minutes = 0
    total_assigned_minutes = 0
    profiles = StudentWorkerProfile.objects.select_related('user').filter(user__role__in=UserRole.worker_roles(), active_status=True).order_by('display_name')
    for profile in profiles:
        open_work = TaskAssignmentService.active_task_queryset_for_user(profile.user)
        open_estimated_minutes = open_work.aggregate(total=Sum('estimated_minutes')).get('total') or 0
        scheduled_minutes = TaskAssignmentService.scheduled_minutes_for_range(profile, week_start, week_end)
        completed_count = Task.objects.filter(
            TaskAssignmentService.task_membership_filter(profile.user),
            completed_at__date__gte=week_start,
            completed_at__date__lte=week_end,
        ).distinct().count()
        total_scheduled_minutes += scheduled_minutes
        total_assigned_minutes += open_estimated_minutes
        workload_rows.append(
            {
                'profile': profile,
                'open_tasks': open_work.count(),
                'open_estimated_minutes': open_estimated_minutes,
                'open_estimated_hours': round(open_estimated_minutes / 60, 1),
                'scheduled_hours': round(scheduled_minutes / 60, 1),
                'completed_this_week': completed_count,
                'load_percent': round((open_estimated_minutes / scheduled_minutes) * 100) if scheduled_minutes else None,
            }
        )

    summary_cards = [
        {'label': 'Completed this week', 'value': completed_this_week},
        {'label': 'Open overdue tasks', 'value': overdue_open},
        {'label': 'Waiting / blocked', 'value': waiting_open},
        {'label': 'Active recurring templates', 'value': active_recurring_templates},
        {'label': 'Recurring runs this week', 'value': recurring_runs_this_week},
        {'label': 'Scheduled hours this week', 'value': round(total_scheduled_minutes / 60, 1)},
    ]

    return render(
        request,
        'workboard/reports.html',
        {
            'today': today,
            'week_start': week_start,
            'week_end': week_end,
            'summary_cards': summary_cards,
            'workload_rows': workload_rows,
            'recurring_due_soon': recurring_due_soon,
            'total_assigned_hours': round(total_assigned_minutes / 60, 1),
        },
    )
