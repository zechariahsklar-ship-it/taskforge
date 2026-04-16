import csv
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django.db.models import Min, Q, Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from .models import RecurringTaskTemplate, StudentWorkerProfile, Task, TaskAuditAction, TaskAuditEvent, UserRole
from .task_views import _scope_queryset_to_user_team, supervisor_required
from .assignment_service import TaskAssignmentService


REPORT_PERIOD_WEEK = "week"
REPORT_PERIOD_MONTH = "month"

def _week_bounds(anchor):
    week_start = anchor - timedelta(days=anchor.weekday())
    return week_start, week_start + timedelta(days=6)


def _month_bounds(anchor):
    month_start = anchor.replace(day=1)
    return month_start, anchor.replace(day=monthrange(anchor.year, anchor.month)[1])


def _period_bounds(period, anchor):
    if period == REPORT_PERIOD_MONTH:
        return _month_bounds(anchor)
    return _week_bounds(anchor)


def _normalize_period_anchor(period, anchor):
    return _period_bounds(period, anchor)[0]


def _shift_period_anchor(anchor, period, step):
    if period == REPORT_PERIOD_MONTH:
        month_index = anchor.month - 1 + step
        year = anchor.year + month_index // 12
        month = month_index % 12 + 1
        return date(year, month, 1)
    return anchor + timedelta(weeks=step)


def _parse_period(request):
    raw_period = request.GET.get("period", REPORT_PERIOD_WEEK).strip().lower()
    if raw_period in {REPORT_PERIOD_WEEK, REPORT_PERIOD_MONTH}:
        return raw_period
    return REPORT_PERIOD_WEEK


def _parse_anchor_date(request, today):
    raw_anchor = request.GET.get("anchor", "").strip()
    if not raw_anchor:
        return today
    try:
        return date.fromisoformat(raw_anchor)
    except ValueError:
        return today


def _period_end_datetime(period_end):
    return timezone.make_aware(datetime.combine(period_end, time.max))


def _period_unit_label(period):
    return "month" if period == REPORT_PERIOD_MONTH else "week"


def _period_title(period):
    return "Monthly" if period == REPORT_PERIOD_MONTH else "Weekly"


def _coerce_date_value(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return timezone.localtime(value).date()
    return value


def _period_start_label(period, period_start):
    return period_start.strftime("%b %d, %Y").replace(" 0", " ")


def _report_anchor_options(*, request_user, period, today, selected_anchor):
    visible_tasks = _scope_queryset_to_user_team(Task.objects.all(), request_user)
    task_dates = visible_tasks.aggregate(
        earliest_due=Min("due_date"),
        earliest_created=Min("created_at"),
        earliest_completed=Min("completed_at"),
    )
    audit_dates = _scope_queryset_to_user_team(
        TaskAuditEvent.objects.all(),
        request_user,
        field_name="task__team",
    ).aggregate(earliest_created=Min("created_at"))

    date_candidates = [
        today,
        selected_anchor,
        _coerce_date_value(task_dates["earliest_due"]),
        _coerce_date_value(task_dates["earliest_created"]),
        _coerce_date_value(task_dates["earliest_completed"]),
        _coerce_date_value(audit_dates["earliest_created"]),
    ]
    earliest_date = min(candidate for candidate in date_candidates if candidate is not None)
    earliest_period_start = _normalize_period_anchor(period, earliest_date)
    current_period_start = _normalize_period_anchor(period, today)

    options = []
    cursor = current_period_start
    while cursor >= earliest_period_start:
        options.append(
            {
                "value": cursor.isoformat(),
                "label": _period_start_label(period, cursor),
            }
        )
        cursor = _shift_period_anchor(cursor, period, -1)
    return options


def _hours_from_minutes(minutes):
    return round(minutes / 60, 1)


def _report_href(*, period, anchor, export=None):
    params = {"period": period, "anchor": anchor.isoformat()}
    if export:
        params["export"] = export
    return f"{reverse('reports')}?{urlencode(params)}"


def _tasks_open_at_period_end(queryset, period_end_at):
    return queryset.filter(created_at__lte=period_end_at).filter(Q(completed_at__isnull=True) | Q(completed_at__gt=period_end_at))


def _worker_workload_rows(*, request_user, period_start, period_end, period_end_at):
    workload_rows = []
    total_scheduled_minutes = 0
    total_assigned_minutes = 0
    profiles = StudentWorkerProfile.objects.select_related("user").filter(user__role__in=UserRole.worker_roles(), active_status=True)
    profiles = _scope_queryset_to_user_team(profiles, request_user, field_name="user__team").order_by("display_name")
    for profile in profiles:
        member_tasks = _scope_queryset_to_user_team(
            Task.objects.filter(TaskAssignmentService.task_membership_filter(profile.user)).distinct(),
            request_user,
        )
        open_work = _tasks_open_at_period_end(member_tasks, period_end_at)
        open_estimated_minutes = open_work.aggregate(total=Sum("estimated_minutes")).get("total") or 0
        scheduled_minutes = TaskAssignmentService.scheduled_minutes_for_range(profile, period_start, period_end)
        completed_count = member_tasks.filter(completed_at__date__gte=period_start, completed_at__date__lte=period_end).count()
        total_scheduled_minutes += scheduled_minutes
        total_assigned_minutes += open_estimated_minutes
        workload_rows.append(
            {
                "profile": profile,
                "open_tasks": open_work.count(),
                "open_estimated_minutes": open_estimated_minutes,
                "open_estimated_hours": _hours_from_minutes(open_estimated_minutes),
                "scheduled_hours": _hours_from_minutes(scheduled_minutes),
                "completed_in_period": completed_count,
                "load_percent": round((open_estimated_minutes / scheduled_minutes) * 100) if scheduled_minutes else None,
            }
        )
    return workload_rows, total_scheduled_minutes, total_assigned_minutes


def _build_report_context(request, *, today, period, selected_anchor):
    period_start, period_end = _period_bounds(period, selected_anchor)
    period_end_at = _period_end_datetime(period_end)
    period_unit = _period_unit_label(period)
    period_title = _period_title(period)
    visible_tasks = _scope_queryset_to_user_team(Task.objects.all(), request.user)
    open_tasks_at_end = _tasks_open_at_period_end(visible_tasks, period_end_at)
    completed_in_period = visible_tasks.filter(completed_at__date__gte=period_start, completed_at__date__lte=period_end).count()
    created_in_period = visible_tasks.filter(created_at__date__gte=period_start, created_at__date__lte=period_end).count()
    due_in_period = visible_tasks.filter(due_date__gte=period_start, due_date__lte=period_end).count()
    open_due_by_period_end = open_tasks_at_end.filter(due_date__isnull=False, due_date__lte=period_end).count()
    recurring_runs_in_period = _scope_queryset_to_user_team(
        TaskAuditEvent.objects.filter(
            action=TaskAuditAction.RECURRING_RUN,
            created_at__date__gte=period_start,
            created_at__date__lte=period_end,
        ),
        request.user,
        field_name="task__team",
    ).count()
    recurring_generated_in_period = visible_tasks.filter(
        recurring_task=True,
        created_at__date__gte=period_start,
        created_at__date__lte=period_end,
    ).count()
    active_recurring_templates = _scope_queryset_to_user_team(RecurringTaskTemplate.objects.filter(active=True), request.user).count()

    workload_rows, total_scheduled_minutes, total_assigned_minutes = _worker_workload_rows(
        request_user=request.user,
        period_start=period_start,
        period_end=period_end,
        period_end_at=period_end_at,
    )

    summary_cards = [
        {"label": f"Completed this {period_unit}", "value": completed_in_period},
        {"label": f"Created this {period_unit}", "value": created_in_period},
        {"label": f"Due this {period_unit}", "value": due_in_period},
        {"label": f"Open due by {period_unit} end", "value": open_due_by_period_end},
        {"label": f"Recurring runs this {period_unit}", "value": recurring_runs_in_period},
        {"label": f"Scheduled hours this {period_unit}", "value": _hours_from_minutes(total_scheduled_minutes)},
    ]

    current_period_start = _normalize_period_anchor(period, today)
    newer_period_start = _shift_period_anchor(period_start, period, 1)
    older_period_start = _shift_period_anchor(period_start, period, -1)

    return {
        "today": today,
        "period": period,
        "period_title": period_title,
        "period_unit": period_unit,
        "period_start": period_start,
        "period_end": period_end,
        "anchor_value": period_start.isoformat(),
        "anchor_options": _report_anchor_options(
            request_user=request.user,
            period=period,
            today=today,
            selected_anchor=period_start,
        ),
        "period_options": [
            {"value": REPORT_PERIOD_WEEK, "label": "Weekly"},
            {"value": REPORT_PERIOD_MONTH, "label": "Monthly"},
        ],
        "summary_cards": summary_cards,
        "workload_rows": workload_rows,
        "total_assigned_hours": _hours_from_minutes(total_assigned_minutes),
        "recurring_generated_in_period": recurring_generated_in_period,
        "active_recurring_templates": active_recurring_templates,
        "export_url": _report_href(period=period, anchor=period_start, export="csv"),
        "current_period_url": _report_href(period=period, anchor=current_period_start),
        "older_period_url": _report_href(period=period, anchor=older_period_start),
        "newer_period_url": _report_href(period=period, anchor=newer_period_start) if newer_period_start <= current_period_start else "",
        "showing_current_period": period_start == current_period_start,
    }


def _export_report_csv(context):
    filename = f"taskforge-{context['period']}-report-{context['period_start'].isoformat()}-to-{context['period_end'].isoformat()}.csv"
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(["TaskForge report"])
    writer.writerow(["Report type", context["period_title"]])
    writer.writerow(["Period start", context["period_start"].isoformat()])
    writer.writerow(["Period end", context["period_end"].isoformat()])
    writer.writerow([])
    writer.writerow(["Summary metrics"])
    writer.writerow(["Metric", "Value"])
    for card in context["summary_cards"]:
        writer.writerow([card["label"], card["value"]])
    writer.writerow([])
    writer.writerow(["Report details"])
    writer.writerow(["Metric", "Value"])
    writer.writerow(["Recurring tasks created in period", context["recurring_generated_in_period"]])
    writer.writerow(["Active recurring templates now", context["active_recurring_templates"]])
    writer.writerow(["Open assigned hours at period end", context["total_assigned_hours"]])
    writer.writerow(["Report generated", context["today"].isoformat()])
    writer.writerow([])
    writer.writerow(["Worker workload"])
    writer.writerow(["Teammate", "Role", "Open tasks at end", "Open hours at end", "Scheduled hours", "Load", "Completed in period"])
    for row in context["workload_rows"]:
        writer.writerow(
            [
                row["profile"].display_name,
                row["profile"].user.get_role_display(),
                row["open_tasks"],
                row["open_estimated_hours"],
                row["scheduled_hours"],
                f"{row['load_percent']}%" if row["load_percent"] is not None else "-",
                row["completed_in_period"],
            ]
        )
    return response


@supervisor_required
def reports_view(request):
    today = timezone.localdate()
    period = _parse_period(request)
    selected_anchor = _normalize_period_anchor(period, _parse_anchor_date(request, today))
    current_period_start = _normalize_period_anchor(period, today)
    if selected_anchor > current_period_start:
        selected_anchor = current_period_start
    context = _build_report_context(request, today=today, period=period, selected_anchor=selected_anchor)
    if request.GET.get("export") == "csv":
        return _export_report_csv(context)
    return render(request, "workboard/reports.html", context)
