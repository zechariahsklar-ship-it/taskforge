from datetime import date, datetime, time, timedelta
import json
from functools import wraps
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Max, Q, Sum
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .audit_service import TaskAuditService
from .forms import (
    AppPasswordChangeForm,
    TaskBoardFilterForm,
    RecurringTaskTemplateForm,
    StudentScheduleOverrideForm,
    StudentWorkerProfileForm,
    SupervisorForm,
    SupervisorStudentPasswordResetForm,
    TaskForm,
    TaskManualForm,
    TaskChecklistItemForm,
    TaskIntakeForm,
    TaskNoteForm,
    TaskAttachmentForm,
    TaskUpdateForm,
    WeeklyAvailabilityForm,
)
from .models import (
    RecurringTaskTemplate,
    StudentAvailability,
    StudentAvailabilityBlock,
    StudentScheduleOverride,
    StudentScheduleOverrideBlock,
    StudentWorkerProfile,
    Task,
    TaskAttachment,
    TaskChecklistItem,
    TaskIntakeDraft,
    TaskIntakeDraftAttachment,
    TaskStatus,
    User,
    UserRole,
    Weekday,
)
from .recurring_service import RecurringTaskService
from .services import TaskAssignmentService, TaskEstimateFeedbackService, TaskParsingService


BOARD_COLUMNS = [
    TaskStatus.NEW,
    TaskStatus.IN_PROGRESS,
    TaskStatus.WAITING,
    TaskStatus.REVIEW,
    TaskStatus.DONE,
]

WEEKLY_SCHEDULE_FIELDS = [
    ("monday", Weekday.MONDAY),
    ("tuesday", Weekday.TUESDAY),
    ("wednesday", Weekday.WEDNESDAY),
    ("thursday", Weekday.THURSDAY),
    ("friday", Weekday.FRIDAY),
    ("saturday", Weekday.SATURDAY),
    ("sunday", Weekday.SUNDAY),
]


def _board_bucket_status(status: str) -> str:
    return TaskStatus.NEW if status == TaskStatus.ASSIGNED else status


def _process_ready_recurring_tasks(request) -> None:
    if getattr(request, "_recurring_rollover_checked", False):
        return
    request._recurring_rollover_checked = True
    if request.method not in {"GET", "HEAD"}:
        return
    if request.resolver_match and request.resolver_match.url_name in {"logout", "password-change"}:
        return
    # Sweep overdue recurring rollovers on normal app traffic when no scheduler is available.
    RecurringTaskService.run_completed_templates_ready_today()


def supervisor_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if request.user.role != UserRole.SUPERVISOR:
            return HttpResponseForbidden("Supervisor access required.")
        _process_ready_recurring_tasks(request)
        return view_func(request, *args, **kwargs)

    return wrapped


def task_editor_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if not request.user.can_edit_tasks:
            return HttpResponseForbidden("Task editor access required.")
        _process_ready_recurring_tasks(request)
        return view_func(request, *args, **kwargs)

    return wrapped


def app_login_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(request, *args, **kwargs):
        if request.user.must_change_password and request.resolver_match and request.resolver_match.url_name not in {
            "password-change",
            "logout",
        }:
            messages.info(request, "You must create a new password before continuing.")
            return redirect("password-change")
        _process_ready_recurring_tasks(request)
        return view_func(request, *args, **kwargs)

    return wrapped


def _normalize_checklist_rows(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]


def _next_checklist_position(task: Task, exclude_pk: int | None = None) -> int:
    queryset = task.checklist_items.all()
    if exclude_pk:
        queryset = queryset.exclude(pk=exclude_pk)
    return (queryset.aggregate(max_position=Max("position")).get("max_position") or 0) + 1


def _resequence_checklist_items(task: Task, ordered_items: list[TaskChecklistItem]) -> None:
    for index, item in enumerate(ordered_items, start=1):
        if item.position != index:
            item.position = index
            item.save(update_fields=["position"])


def _build_checklist_editor_rows(values: list[str]) -> list[str]:
    rows = _normalize_checklist_rows(values)
    return (rows or [""]) + [""]


def _ensure_task_due_date(task: Task) -> Task:
    if task.due_date:
        return task
    if task.scheduled_date:
        task.due_date = task.scheduled_date
        return task
    _, fallback_due_date = TaskParsingService._priority_due_date(task.priority)
    task.due_date = fallback_due_date
    if not task.raw_due_text:
        task.raw_due_text = f"Priority-based default for {task.priority}"
    return task


def _next_board_order(status: str, exclude_pk: int | None = None) -> int:
    queryset = Task.objects.filter(status=status)
    if exclude_pk:
        queryset = queryset.exclude(pk=exclude_pk)
    return (queryset.aggregate(max_order=Max("board_order")).get("max_order") or 0) + 1


def _ordered_status_tasks(status: str, *, exclude_pk: int | None = None) -> list[Task]:
    queryset = Task.objects.filter(status=status)
    if exclude_pk:
        queryset = queryset.exclude(pk=exclude_pk)
    return list(queryset.order_by(F("board_order").asc(nulls_last=True), "due_date", "-created_at", "pk"))


def _append_task_to_status(task: Task, previous_status: str | None = None) -> Task:
    if task.board_order is not None and previous_status == task.status:
        return task
    task.board_order = _next_board_order(task.status, exclude_pk=task.pk)
    return task


def _task_membership_filter(user: User) -> Q:
    return (
        Q(assigned_to=user)
        | Q(additional_assignees=user)
        | Q(rotating_additional_assignees=user)
        | Q(rotating_additional_assignee=user)
    )


def _task_board_queryset():
    return Task.objects.select_related(
        "assigned_to",
        "requested_by",
        "created_by",
        "rotating_additional_assignee",
    ).prefetch_related("additional_assignees", "rotating_additional_assignees")


def _ordered_board_tasks(queryset):
    return list(queryset.order_by("status", F("board_order").asc(nulls_last=True), "due_date", "-created_at", "pk"))


def _group_tasks_for_board(tasks: list[Task]) -> list[dict]:
    for task in tasks:
        task.status_bucket = _board_bucket_status(task.status)
        task.board_due_date = task.due_date or TaskParsingService._priority_due_date(task.priority)[1]
    return [
        {"value": status, "label": TaskStatus(status).label, "tasks": [task for task in tasks if task.status_bucket == status]}
        for status in BOARD_COLUMNS
    ]


def _user_can_access_task(user: User, task: Task) -> bool:
    if user.can_view_full_board:
        return True
    return (
        task.assigned_to_id == user.id
        or task.rotating_additional_assignee_id == user.id
        or task.additional_assignees.filter(pk=user.id).exists()
        or task.rotating_additional_assignees.filter(pk=user.id).exists()
    )


def _resequence_status_tasks(tasks: list[Task], status: str) -> None:
    for index, item in enumerate(tasks, start=1):
        update_fields = []
        if item.status != status:
            item.status = status
            update_fields.append("status")
        if item.board_order != index:
            item.board_order = index
            update_fields.append("board_order")
        if update_fields:
            update_fields.append("updated_at")
            item.save(update_fields=update_fields)


def _close_status_gap(status: str, *, exclude_pk: int) -> None:
    _resequence_status_tasks(_ordered_status_tasks(status, exclude_pk=exclude_pk), status)


def _ordered_recurring_templates(*, exclude_pk: int | None = None) -> list[RecurringTaskTemplate]:
    queryset = RecurringTaskTemplate.objects.all()
    if exclude_pk:
        queryset = queryset.exclude(pk=exclude_pk)
    return list(queryset.order_by(F("display_order").asc(nulls_last=True), "next_run_date", "title", "pk"))


def _resequence_recurring_templates(templates: list[RecurringTaskTemplate]) -> None:
    for index, template in enumerate(templates, start=1):
        if template.display_order != index:
            template.display_order = index
            template.save(update_fields=["display_order", "updated_at"])


def _recurring_assignee_from_task(task: Task) -> User | None:
    assignee = task.assigned_to
    if assignee and assignee.role in UserRole.worker_roles():
        return assignee
    return None


def _current_rotating_additional_user_ids(task: Task) -> list[int]:
    current_ids = list(task.rotating_additional_assignees.order_by("first_name", "last_name", "username").values_list("pk", flat=True))
    if not current_ids and task.rotating_additional_assignee_id:
        current_ids.append(task.rotating_additional_assignee_id)
    return current_ids


# Keep the fixed and rotating teammate rules in one place so task create, edit,
# and recurring generation all stay aligned.
def _choose_rotating_additional_assignees(
    *,
    due_date,
    estimated_minutes,
    count,
    assigned_to_id=None,
    fixed_additional_ids=None,
    preserve_user_ids=None,
    avoid_user_ids=None,
    scheduled_date=None,
    scheduled_start_time=None,
    scheduled_end_time=None,
    exclude_task_id=None,
) -> list[User]:
    excluded_ids = {user_id for user_id in (fixed_additional_ids or []) if user_id}
    if assigned_to_id:
        excluded_ids.add(assigned_to_id)

    return TaskAssignmentService.suggest_worker_assignees(
        due_date=due_date,
        estimated_minutes=estimated_minutes,
        count=count,
        exclude_user_ids=list(excluded_ids),
        preferred_user_ids=preserve_user_ids,
        avoid_user_ids=avoid_user_ids,
        scheduled_date=scheduled_date,
        scheduled_start_time=scheduled_start_time,
        scheduled_end_time=scheduled_end_time,
        exclude_task_id=exclude_task_id,
    )


def _apply_task_additional_assignee_settings(task: Task, *, preserve_existing_rotation: bool = True, previous_rotating_user_ids: list[int] | None = None) -> Task:
    if task.assigned_to_id:
        task.additional_assignees.remove(task.assigned_to_id)

    fixed_additional_ids = list(task.additional_assignees.values_list("pk", flat=True))
    rotation_count = task.rotating_additional_assignee_count or 0
    rotating_assignees = _choose_rotating_additional_assignees(
        due_date=task.due_date,
        estimated_minutes=task.estimated_minutes,
        count=rotation_count,
        assigned_to_id=task.assigned_to_id,
        fixed_additional_ids=fixed_additional_ids,
        preserve_user_ids=_current_rotating_additional_user_ids(task) if preserve_existing_rotation else [],
        avoid_user_ids=previous_rotating_user_ids,
        scheduled_date=task.scheduled_date,
        scheduled_start_time=task.scheduled_start_time,
        scheduled_end_time=task.scheduled_end_time,
        exclude_task_id=task.pk,
    )
    task.rotate_additional_assignee = rotation_count > 0
    task.rotating_additional_assignee = rotating_assignees[0] if rotating_assignees else None
    task.save(update_fields=["rotate_additional_assignee", "rotating_additional_assignee", "updated_at"])
    task.rotating_additional_assignees.set([user.pk for user in rotating_assignees])
    return task


def _next_recurring_run_from_task(task: Task) -> date:
    seed_date = task.scheduled_date or task.due_date or timezone.localdate()
    template = RecurringTaskTemplate(
        recurrence_pattern=task.recurrence_pattern,
        recurrence_interval=task.recurrence_interval or 1,
        day_of_week=task.recurrence_day_of_week,
        day_of_month=task.recurrence_day_of_month,
        start_date=seed_date,
        next_run_date=seed_date,
    )
    template.advance_next_run_date()
    return template.next_run_date


def _sync_task_recurring_template(task: Task) -> Task:
    if not task.recurring_task or not task.recurrence_pattern:
        return task

    seed_date = task.scheduled_date or task.due_date or timezone.localdate()
    desired_next_run = _next_recurring_run_from_task(task)
    assignee = _recurring_assignee_from_task(task)
    fixed_additional_assignee_ids = list(task.additional_assignees.exclude(pk=task.assigned_to_id).values_list("pk", flat=True))
    template = task.recurring_template

    if template is None:
        template = RecurringTaskTemplate.objects.create(
            title=task.title,
            description=task.description or task.raw_message,
            priority=task.priority,
            estimated_minutes=task.estimated_minutes,
            scheduled_start_time=task.scheduled_start_time,
            scheduled_end_time=task.scheduled_end_time,
            assign_to=assignee,
            rotating_additional_assignee_count=task.rotating_additional_assignee_count,
            rotate_additional_assignee=(task.rotating_additional_assignee_count or 0) > 0,
            requested_by=task.requested_by or task.created_by,
            recurrence_pattern=task.recurrence_pattern,
            recurrence_interval=task.recurrence_interval or 1,
            day_of_week=task.recurrence_day_of_week,
            day_of_month=task.recurrence_day_of_month,
            start_date=seed_date,
            next_run_date=desired_next_run,
            active=True,
        )
        template.additional_assignees.set(fixed_additional_assignee_ids)
        task.recurring_template = template
        task.save(update_fields=["recurring_template", "updated_at"])
        return task

    schedule_changed = (
        template.recurrence_pattern != task.recurrence_pattern
        or template.recurrence_interval != (task.recurrence_interval or 1)
        or template.day_of_week != task.recurrence_day_of_week
        or template.day_of_month != task.recurrence_day_of_month
    )
    template.title = task.title
    template.description = task.description or task.raw_message
    template.priority = task.priority
    template.estimated_minutes = task.estimated_minutes
    template.scheduled_start_time = task.scheduled_start_time
    template.scheduled_end_time = task.scheduled_end_time
    template.assign_to = assignee
    template.rotating_additional_assignee_count = task.rotating_additional_assignee_count
    template.rotate_additional_assignee = (task.rotating_additional_assignee_count or 0) > 0
    template.requested_by = task.requested_by or task.created_by or template.requested_by
    template.recurrence_pattern = task.recurrence_pattern
    template.recurrence_interval = task.recurrence_interval or 1
    template.day_of_week = task.recurrence_day_of_week
    template.day_of_month = task.recurrence_day_of_month
    template.active = True
    if schedule_changed or template.next_run_date <= seed_date:
        template.start_date = seed_date
        template.next_run_date = desired_next_run
    template.save()
    template.additional_assignees.set(fixed_additional_assignee_ids)
    return task


def _backfill_orphan_recurring_tasks() -> None:
    orphan_tasks = (
        Task.objects.filter(recurring_task=True, recurring_template__isnull=True)
        .exclude(recurrence_pattern="")
        .select_related("assigned_to", "requested_by", "created_by")
    )
    for task in orphan_tasks:
        _sync_task_recurring_template(task)


def _fallback_blocks_from_hours(hours_value):
    if not hours_value:
        return []
    total_minutes = int(float(hours_value) * 60)
    if total_minutes <= 0 or total_minutes % 30 != 0:
        return []
    start_value = time(9, 0)
    end_value = (datetime.combine(date.today(), start_value) + timedelta(minutes=total_minutes)).time()
    return [(start_value, end_value)]


def _serialize_blocks_for_initial(blocks):
    return json.dumps([[start_time.strftime("%H:%M"), end_time.strftime("%H:%M")] for start_time, end_time in blocks])


def _availability_blocks(availability: StudentAvailability | None) -> list[tuple[time, time]]:
    if availability is None:
        return []
    blocks = [
        (block.start_time, block.end_time)
        for block in availability.blocks.order_by("position", "start_time", "end_time", "pk")
    ]
    if blocks:
        return blocks
    if availability.start_time and availability.end_time:
        return [(availability.start_time, availability.end_time)]
    return _fallback_blocks_from_hours(availability.hours_available)


def _weekly_schedule_initial(profile: StudentWorkerProfile | None = None) -> dict:
    weekly_map = {item.weekday: item for item in profile.weekly_availability.prefetch_related("blocks").all()} if profile else {}
    initial = {}
    for prefix, weekday in WEEKLY_SCHEDULE_FIELDS:
        availability = weekly_map.get(weekday)
        blocks = _availability_blocks(availability)
        initial[f"{prefix}_segments"] = _serialize_blocks_for_initial(blocks)
        initial[f"{prefix}_start"] = blocks[0][0] if blocks else (availability.start_time if availability else None)
        initial[f"{prefix}_end"] = blocks[-1][1] if blocks else (availability.end_time if availability else None)
        initial[f"{prefix}_hours"] = availability.hours_available if availability else 0
    return initial


def _save_weekly_schedule(profile: StudentWorkerProfile, weekly_form: WeeklyAvailabilityForm) -> None:
    for _, weekday in WEEKLY_SCHEDULE_FIELDS:
        defaults = weekly_form.cleaned_data["schedule_windows"][weekday]
        availability, _ = StudentAvailability.objects.update_or_create(
            profile=profile,
            weekday=weekday,
            defaults=defaults,
        )
        availability.blocks.all().delete()
        for position, block in enumerate(weekly_form.cleaned_data["schedule_blocks"][weekday], start=1):
            StudentAvailabilityBlock.objects.create(
                availability=availability,
                start_time=block["start_time"],
                end_time=block["end_time"],
                position=position,
            )


def _save_schedule_override(profile: StudentWorkerProfile, schedule_override_form: StudentScheduleOverrideForm, created_by: User) -> StudentScheduleOverride:
    schedule_override, _ = StudentScheduleOverride.objects.update_or_create(
        profile=profile,
        override_date=schedule_override_form.cleaned_data["override_date"],
        defaults={
            "note": schedule_override_form.cleaned_data.get("note", ""),
            "created_by": created_by,
        },
    )
    schedule_override.blocks.all().delete()
    for position, block in enumerate(schedule_override_form.cleaned_data["schedule_blocks"], start=1):
        StudentScheduleOverrideBlock.objects.create(
            schedule_override=schedule_override,
            start_time=block["start_time"],
            end_time=block["end_time"],
            position=position,
        )
    return schedule_override


def _build_due_date_review_context(initial: dict, due_date_value) -> dict:
    raw_due_text = (initial.get("raw_due_text") or "").strip()
    parsed_due_date = TaskParsingService._parse_due_date(initial.get("due_date"))
    original_due_date = TaskParsingService._parse_due_date(initial.get("due_date_original")) or parsed_due_date
    final_due_date = TaskParsingService._parse_due_date(due_date_value) or parsed_due_date
    is_defaulted = bool(initial.get("due_date_defaulted"))
    weekend_adjusted = bool(initial.get("due_date_weekend_adjusted"))
    source = initial.get("due_date_source") or ("priority_default" if is_defaulted else "parsed" if parsed_due_date else "unconfirmed")
    due_date_confidence = initial.get("due_date_confidence") or ("low" if is_defaulted else "high" if parsed_due_date else "low")
    is_inferred = source == "inferred_from_phrase" or bool(initial.get("due_date_inferred"))
    directly_confirmed = source == "parsed" and due_date_confidence == "high" and not is_defaulted
    source_labels = {
        "parsed": "Direct date from the message",
        "inferred_from_phrase": "Resolved from a relative date phrase",
        "priority_default": "Priority-based fallback",
        "unconfirmed": "Needs supervisor review",
    }
    confidence_labels = {
        "high": "High confidence",
        "medium": "Medium confidence",
        "low": "Low confidence",
    }

    if source == "priority_default" and final_due_date:
        resolution_summary = "No due date was found in the message, so TaskForge set one from the fallback rules."
    elif source == "inferred_from_phrase" and raw_due_text and parsed_due_date:
        resolution_summary = f'The parser read "{raw_due_text}" as a relative date phrase and resolved it to the local date below.'
    elif parsed_due_date:
        resolution_summary = "The parser provided a directly usable due date."
    else:
        resolution_summary = "No due date has been confirmed yet."

    warning = initial.get("due_date_warning") or ""
    if not warning and source in {"priority_default", "inferred_from_phrase"} and final_due_date:
        if source == "priority_default":
            warning = f"This due date was defaulted by the app, not directly confirmed in the message. Please verify {final_due_date.isoformat()} before saving."
        else:
            warning = f"This due date was inferred from the message, not directly confirmed. Please verify {final_due_date.isoformat()} before saving."

    return {
        "raw_due_text": raw_due_text,
        "parsed_due_date": parsed_due_date,
        "original_due_date": original_due_date,
        "final_due_date": final_due_date,
        "due_date_source": source,
        "due_date_source_label": source_labels.get(source, "Needs supervisor review"),
        "due_date_confidence": due_date_confidence,
        "due_date_confidence_label": confidence_labels.get(due_date_confidence, "Needs review"),
        "due_date_resolution_summary": resolution_summary,
        "due_date_warning": warning,
        "due_date_inferred": is_inferred,
        "due_date_defaulted": is_defaulted,
        "due_date_directly_confirmed": directly_confirmed,
        "due_date_weekend_adjusted": weekend_adjusted,
        "fallback_rules": [
            "Urgent: same day",
            "High: 2 days",
            "Medium: 4 days",
            "Low: 7 days",
            "Any weekend due date moves to Monday",
        ],
    }


def _filter_query_url(view_name: str, **params) -> str:
    cleaned = {key: value for key, value in params.items() if value not in {None, ""}}
    base_url = reverse(view_name)
    if not cleaned:
        return base_url
    return f"{base_url}?{urlencode(cleaned)}"


def _task_filter_form(request, *, include_assignee: bool):
    return TaskBoardFilterForm(request.GET or None, user=request.user, include_assignee=include_assignee)


def _active_task_filter_labels(filter_form: TaskBoardFilterForm) -> list[str]:
    if not filter_form.is_bound or not filter_form.is_valid():
        return []
    cleaned = filter_form.cleaned_data
    labels = []
    if cleaned.get("saved_view"):
        labels.append(f"View: {dict(filter_form.fields['saved_view'].choices).get(cleaned['saved_view'], cleaned['saved_view'])}")
    if cleaned.get("q"):
        labels.append(f'Search: "{cleaned["q"]}"')
    if cleaned.get("priority"):
        labels.append(f"Priority: {cleaned['priority'].title()}")
    if cleaned.get("due_scope"):
        labels.append(f"Due date: {dict(filter_form.fields['due_scope'].choices).get(cleaned['due_scope'], cleaned['due_scope'])}")
    assigned_to = cleaned.get("assigned_to")
    if assigned_to:
        labels.append(f"Teammate: {assigned_to.display_label}")
    return labels


def _apply_saved_task_view(queryset, *, saved_view: str, today: date):
    if saved_view == "today":
        return queryset.exclude(status=TaskStatus.DONE).filter(Q(due_date=today) | Q(scheduled_date=today))
    if saved_view == "overdue":
        return queryset.exclude(status=TaskStatus.DONE).filter(due_date__lt=today)
    if saved_view == "waiting":
        return queryset.exclude(status=TaskStatus.DONE).filter(status=TaskStatus.WAITING)
    if saved_view == "recurring":
        return queryset.filter(Q(recurring_task=True) | Q(recurring_template__isnull=False))
    if saved_view == "scheduled":
        return queryset.exclude(status=TaskStatus.DONE).filter(scheduled_date__isnull=False)
    return queryset


def _apply_task_board_filters(queryset, filter_form: TaskBoardFilterForm):
    if not filter_form.is_bound or not filter_form.is_valid():
        return queryset, []

    cleaned = filter_form.cleaned_data
    today = timezone.localdate()
    filtered = queryset

    filtered = _apply_saved_task_view(filtered, saved_view=cleaned.get("saved_view", ""), today=today)

    query_text = (cleaned.get("q") or "").strip()
    if query_text:
        filtered = filtered.filter(
            Q(title__icontains=query_text)
            | Q(description__icontains=query_text)
            | Q(raw_message__icontains=query_text)
            | Q(respond_to_text__icontains=query_text)
        )

    if cleaned.get("priority"):
        filtered = filtered.filter(priority=cleaned["priority"])

    due_scope = cleaned.get("due_scope")
    if due_scope == "overdue":
        filtered = filtered.exclude(status=TaskStatus.DONE).filter(due_date__lt=today)
    elif due_scope == "today":
        filtered = filtered.filter(due_date=today)
    elif due_scope == "week":
        filtered = filtered.filter(due_date__gte=today, due_date__lte=today + timedelta(days=7))
    elif due_scope == "none":
        filtered = filtered.filter(due_date__isnull=True)

    assigned_to = cleaned.get("assigned_to")
    if assigned_to:
        filtered = filtered.filter(_task_membership_filter(assigned_to))

    return filtered.distinct(), _active_task_filter_labels(filter_form)


def _build_due_today_warning(*, task_queryset, current_view: str):
    today = timezone.localdate()
    due_today_count = (
        task_queryset.exclude(status=TaskStatus.DONE)
        .filter(Q(due_date=today) | Q(scheduled_date=today))
        .distinct()
        .count()
    )
    if not due_today_count:
        return None
    return {
        "count": due_today_count,
        "href": _filter_query_url(current_view, saved_view="today"),
        "label": f"{due_today_count} task{'s' if due_today_count != 1 else ''} due or scheduled today",
    }


@app_login_required
def dashboard(request):
    if request.user.can_view_full_board:
        return redirect("board")
    return redirect("my-tasks")


@app_login_required
def board_view(request):
    tasks = _task_board_queryset()
    if not request.user.can_view_full_board:
        tasks = tasks.filter(_task_membership_filter(request.user))
    visible_tasks = tasks.distinct()
    filter_form = _task_filter_form(request, include_assignee=request.user.can_view_full_board)
    filtered_tasks, active_filters = _apply_task_board_filters(visible_tasks, filter_form)
    ordered_tasks = _ordered_board_tasks(filtered_tasks)
    grouped_tasks = _group_tasks_for_board(ordered_tasks)
    return render(
        request,
        "workboard/board.html",
        {
            "grouped_tasks": grouped_tasks,
            "filter_form": filter_form,
            "active_filters": active_filters,
            "task_count": len(ordered_tasks),
            "due_today_warning": _build_due_today_warning(task_queryset=visible_tasks, current_view="board"),
        },
    )


@app_login_required
def board_task_move_view(request, pk):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")

    task = get_object_or_404(_task_board_queryset(), pk=pk)
    if not request.user.can_edit_tasks and not _user_can_access_task(request.user, task):
        return HttpResponseForbidden("You can only move tasks assigned to you.")

    before_snapshot = TaskAuditService.snapshot(task)
    new_status = request.POST.get("status", "").strip()
    if new_status not in BOARD_COLUMNS:
        return HttpResponseBadRequest("Invalid status.")

    before_task_id = request.POST.get("before_task_id", "").strip()
    ordered_target_tasks = _ordered_status_tasks(new_status, exclude_pk=task.pk)
    if before_task_id:
        try:
            before_task_id_int = int(before_task_id)
        except ValueError:
            return HttpResponseBadRequest("Invalid target position.")
        before_task = next((item for item in ordered_target_tasks if item.pk == before_task_id_int), None)
        if before_task is None:
            return HttpResponseBadRequest("Target task not found in destination column.")
        insert_index = ordered_target_tasks.index(before_task)
    else:
        insert_index = len(ordered_target_tasks)

    previous_status = task.status
    ordered_target_tasks.insert(insert_index, task)

    task.status = new_status
    if task.status == TaskStatus.DONE and not task.completed_at:
        task.mark_complete()
    elif task.status != TaskStatus.DONE:
        task.completed_at = None
    task.save(update_fields=["status", "completed_at", "updated_at"])

    _resequence_status_tasks(ordered_target_tasks, new_status)
    if previous_status != new_status:
        _resequence_status_tasks(_ordered_status_tasks(previous_status, exclude_pk=task.pk), previous_status)
    TaskAuditService.record_updated(task, actor=request.user, before_snapshot=before_snapshot)

    return JsonResponse({"ok": True, "status": task.status, "label": TaskStatus(task.status).label})


@app_login_required
def my_tasks_view(request):
    visibility_filter = _task_membership_filter(request.user)
    if request.user.role == UserRole.SUPERVISOR:
        visibility_filter |= Q(status=TaskStatus.WAITING)
    visible_tasks = _task_board_queryset().filter(visibility_filter).distinct()
    filter_form = _task_filter_form(request, include_assignee=False)
    filtered_tasks, active_filters = _apply_task_board_filters(visible_tasks, filter_form)
    ordered_tasks = _ordered_board_tasks(filtered_tasks)
    grouped_tasks = _group_tasks_for_board(ordered_tasks)
    return render(
        request,
        "workboard/my_tasks.html",
        {
            "grouped_tasks": grouped_tasks,
            "filter_form": filter_form,
            "active_filters": active_filters,
            "task_count": len(ordered_tasks),
            "due_today_warning": _build_due_today_warning(task_queryset=visible_tasks, current_view="my-tasks"),
        },
    )


@supervisor_required
def task_intake_view(request):
    if request.method == "POST":
        form = TaskIntakeForm(request.POST, request.FILES)
        if form.is_valid():
            draft = TaskIntakeDraft.objects.create(
                created_by=request.user,
                raw_message=form.cleaned_data["raw_message"],
            )
            uploaded_files = form.cleaned_data["attachments"]
            for uploaded_file in uploaded_files:
                TaskIntakeDraftAttachment.objects.create(
                    draft=draft,
                    file=uploaded_file,
                    original_name=uploaded_file.name,
                )

            parsed = TaskParsingService.parse_request(
                form.cleaned_data["raw_message"],
                attachments=uploaded_files,
                fallback_supervisor=request.user,
            )
            draft.parsed_payload = parsed.to_dict()
            draft.save(update_fields=["parsed_payload", "updated_at"])
            return redirect("task-intake-review", pk=draft.pk)
    else:
        form = TaskIntakeForm()
    return render(request, "workboard/task_intake.html", {"form": form})


@supervisor_required
def task_intake_review_view(request, pk):
    draft = get_object_or_404(TaskIntakeDraft.objects.prefetch_related("attachments"), pk=pk, created_by=request.user)
    initial = draft.parsed_payload or {}
    checklist_values = list(initial.get("checklist_items", []))
    excluded_intake_fields = [
        "recurring_task",
        "recurrence_pattern",
        "recurrence_interval",
        "recurrence_day_of_week",
        "recurrence_day_of_month",
    ]

    if request.method == "POST":
        checklist_values = request.POST.getlist("checklist_items")
        form = TaskForm(request.POST, initial=initial)
        for field_name in excluded_intake_fields:
            form.fields.pop(field_name, None)
        if form.is_valid():
            original_estimated_minutes = initial.get("estimated_minutes")
            task = form.save(commit=False)
            task.created_by = request.user
            task = _ensure_task_due_date(task)
            task = _append_task_to_status(task)
            if not task.assigned_to:
                suggested_user, _, _ = TaskAssignmentService.suggest_assignee(
                    due_date=task.due_date,
                    estimated_minutes=task.estimated_minutes,
                    fallback_supervisor=request.user,
                    scheduled_date=task.scheduled_date,
                    scheduled_start_time=task.scheduled_start_time,
                    scheduled_end_time=task.scheduled_end_time,
                )
                task.assigned_to = suggested_user
            if task.status == TaskStatus.DONE and not task.completed_at:
                task.mark_complete()
            task.save()
            TaskEstimateFeedbackService.record_feedback(
                task=task,
                original_estimated_minutes=original_estimated_minutes,
                corrected_estimated_minutes=task.estimated_minutes,
                corrected_by=request.user,
                source="intake_review",
            )
            form.save_m2m()
            task = _apply_task_additional_assignee_settings(task, preserve_existing_rotation=False)
            for attachment in draft.attachments.all():
                TaskAttachment.objects.create(
                    task=task,
                    file=attachment.file.name,
                    original_name=attachment.original_name,
                )
            checklist_items = TaskParsingService._append_notify_checklist_item(_normalize_checklist_rows(checklist_values), task.respond_to_text)
            for index, title in enumerate(checklist_items, start=1):
                TaskChecklistItem.objects.create(task=task, title=title, position=index)
            TaskAuditService.record_created(task, actor=request.user, source="intake_review")
            messages.success(request, "Task created from intake request.")
            return redirect("task-detail", pk=task.pk)
    else:
        form = TaskForm(initial=initial)
        for field_name in excluded_intake_fields:
            form.fields.pop(field_name, None)

    due_date_review = _build_due_date_review_context(initial, form["due_date"].value())

    return render(
        request,
        "workboard/task_intake_review.html",
        {
            "form": form,
            "page_title": "Review extracted task",
            "draft": draft,
            "assignment_summary": initial.get("assignment_summary", ""),
            "assignment_rationale": initial.get("assignment_rationale", []),
            "checklist_preview": initial.get("checklist_items", []),
            "checklist_editor_rows": _build_checklist_editor_rows(checklist_values),
            "parser_confidence": initial.get("parser_confidence", "medium"),
            "parser_warnings": initial.get("parser_warnings", []),
            "due_date_review": due_date_review,
        },
    )


@supervisor_required
def task_create_view(request):
    if request.method == "POST":
        form = TaskManualForm(request.POST)
        if form.is_valid():
            task = form.save(commit=False)
            task.created_by = request.user
            task = _ensure_task_due_date(task)
            task = _append_task_to_status(task)
            if not task.assigned_to:
                suggested_user, _, _ = TaskAssignmentService.suggest_assignee(
                    due_date=task.due_date,
                    estimated_minutes=task.estimated_minutes,
                    fallback_supervisor=request.user,
                    scheduled_date=task.scheduled_date,
                    scheduled_start_time=task.scheduled_start_time,
                    scheduled_end_time=task.scheduled_end_time,
                )
                task.assigned_to = suggested_user
            if task.status == TaskStatus.DONE and not task.completed_at:
                task.mark_complete()
            task.save()
            form.save_m2m()
            task = _apply_task_additional_assignee_settings(task, preserve_existing_rotation=False)
            task = _sync_task_recurring_template(task)
            TaskAuditService.record_created(task, actor=request.user)
            messages.success(request, "Task created.")
            return redirect("task-detail", pk=task.pk)
    else:
        form = TaskManualForm()
    return render(request, "workboard/task_form.html", {"form": form, "page_title": "Create task"})


@app_login_required
def task_detail_view(request, pk):
    task = get_object_or_404(
        _task_board_queryset().prefetch_related("checklist_items", "attachments", "notes__author"),
        pk=pk,
    )
    if not _user_can_access_task(request.user, task):
        return HttpResponseForbidden("You can only view tasks assigned to you.")

    note_form = TaskNoteForm()
    status_form = TaskUpdateForm(instance=task)
    checklist_form = TaskChecklistItemForm()
    attachment_form = TaskAttachmentForm()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "status":
            before_snapshot = TaskAuditService.snapshot(task)
            status_form = TaskUpdateForm(request.POST, instance=task)
            if status_form.is_valid():
                previous_status = task.status
                updated_task = status_form.save(commit=False)
                if updated_task.status == TaskStatus.DONE and not updated_task.completed_at:
                    updated_task.mark_complete()
                elif updated_task.status != TaskStatus.DONE:
                    updated_task.completed_at = None
                updated_task = _append_task_to_status(updated_task, previous_status=previous_status)
                updated_task.save()
                if previous_status != updated_task.status:
                    _close_status_gap(previous_status, exclude_pk=updated_task.pk)
                TaskAuditService.record_updated(updated_task, actor=request.user, before_snapshot=before_snapshot)
                messages.success(request, "Task status updated.")
                return redirect("task-detail", pk=task.pk)
        elif action == "note":
            note_form = TaskNoteForm(request.POST)
            if note_form.is_valid():
                note = note_form.save(commit=False)
                note.task = task
                note.author = request.user
                note.save()
                TaskAuditService.record_note_added(task, actor=request.user)
                messages.success(request, "Note added.")
                return redirect("task-detail", pk=task.pk)
        elif action == "attachment":
            attachment_form = TaskAttachmentForm(request.POST, request.FILES)
            if attachment_form.is_valid():
                attachment = attachment_form.save(commit=False)
                attachment.task = task
                attachment.original_name = attachment.file.name
                attachment.save()
                TaskAuditService.record_attachment_added(task, actor=request.user, file_name=attachment.original_name)
                messages.success(request, "Attachment added.")
                return redirect("task-detail", pk=task.pk)
        elif action == "checklist" and request.user.can_edit_tasks:
            checklist_form = TaskChecklistItemForm(request.POST)
            if checklist_form.is_valid():
                item = checklist_form.save(commit=False)
                item.task = task
                item.position = _next_checklist_position(task)
                item.save()
                TaskAuditService.record_checklist_updated(task, actor=request.user, summary=f"Added checklist item: {item.title}")
                messages.success(request, "Checklist item added.")
                return redirect("task-detail", pk=task.pk)
        elif action == "checklist_save" and request.user.can_edit_tasks:
            item_ids = request.POST.getlist("checklist_item_ids")
            titles = request.POST.getlist("checklist_item_titles")
            completed_ids = set(request.POST.getlist("checklist_item_completed"))
            delete_item_id = request.POST.get("delete_item_id", "").strip()
            if len(item_ids) != len(titles):
                return HttpResponseBadRequest("Checklist update payload was incomplete.")
            ordered_items = []
            seen_ids = set()
            deleted_any = False
            for index, raw_id in enumerate(item_ids):
                try:
                    item_id = int(raw_id)
                except ValueError:
                    return HttpResponseBadRequest("Invalid checklist item.")
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                item = task.checklist_items.filter(pk=item_id).first()
                if not item:
                    return HttpResponseBadRequest("Checklist item not found.")
                updated_title = titles[index].strip()
                should_delete = not updated_title or delete_item_id == str(item_id)
                if should_delete:
                    item.delete()
                    deleted_any = True
                    continue
                item.updated_title = updated_title
                item.updated_completed = str(item_id) in completed_ids
                ordered_items.append(item)
            for index, item in enumerate(ordered_items, start=1):
                update_fields = []
                if item.title != item.updated_title:
                    item.title = item.updated_title
                    update_fields.append("title")
                if item.is_completed != item.updated_completed:
                    item.is_completed = item.updated_completed
                    update_fields.append("is_completed")
                if item.position != index:
                    item.position = index
                    update_fields.append("position")
                if update_fields:
                    item.save(update_fields=update_fields)
            _resequence_checklist_items(task, ordered_items)
            TaskAuditService.record_checklist_updated(task, actor=request.user)
            messages.success(request, "Checklist updated.")
            return redirect("task-detail", pk=task.pk)
        elif action == "checklist_toggle":
            item_id = request.POST.get("item_id", "").strip()
            try:
                checklist_item = task.checklist_items.get(pk=int(item_id))
            except (ValueError, TaskChecklistItem.DoesNotExist):
                return HttpResponseBadRequest("Checklist item not found.")
            checklist_item.is_completed = request.POST.get("is_completed") == "true"
            checklist_item.save(update_fields=["is_completed"])
            return JsonResponse({"ok": True, "is_completed": checklist_item.is_completed})
        elif action == "checklist_reorder":
            item_ids = request.POST.getlist("item_ids")
            ordered_items = []
            seen_ids = set()
            for raw_id in item_ids:
                try:
                    item_id = int(raw_id)
                except ValueError:
                    return HttpResponseBadRequest("Invalid checklist item.")
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                item = task.checklist_items.filter(pk=item_id).first()
                if not item:
                    return HttpResponseBadRequest("Checklist item not found.")
                ordered_items.append(item)
            existing_items = list(task.checklist_items.all())
            if len(ordered_items) != len(existing_items):
                return HttpResponseBadRequest("Checklist reorder payload was incomplete.")
            _resequence_checklist_items(task, ordered_items)
            return JsonResponse({"ok": True})

    task.detail_due_date = task.due_date or TaskParsingService._priority_due_date(task.priority)[1]
    task.note_items = list(task.notes.order_by("created_at", "id"))
    audit_events = list(task.audit_events.select_related("actor").all()[:25])
    return render(
        request,
        "workboard/task_detail.html",
        {
            "task": task,
            "note_form": note_form,
            "status_form": status_form,
            "checklist_form": checklist_form,
            "attachment_form": attachment_form,
            "audit_events": audit_events,
        },
    )


@login_required
def password_change_view(request):
    if request.method == "POST":
        form = AppPasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            user.must_change_password = False
            user.save(update_fields=["must_change_password"])
            update_session_auth_hash(request, user)
            messages.success(request, "Password changed.")
            return redirect("dashboard")
    else:
        form = AppPasswordChangeForm(request.user)
    return render(
        request,
        "workboard/password_change_form.html",
        {"form": form, "page_title": "Change password", "force_change": request.user.must_change_password},
    )


@task_editor_required
def task_edit_view(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if request.method == "POST":
        previous_status = task.status
        original_estimated_minutes = task.estimated_minutes
        before_snapshot = TaskAuditService.snapshot(task)
        form = TaskManualForm(request.POST, instance=task)
        if form.is_valid():
            updated_task = form.save(commit=False)
            updated_task = _ensure_task_due_date(updated_task)
            updated_task = _append_task_to_status(updated_task, previous_status=previous_status)
            if updated_task.status == TaskStatus.DONE and not updated_task.completed_at:
                updated_task.mark_complete()
            elif updated_task.status != TaskStatus.DONE:
                updated_task.completed_at = None
            updated_task.save()
            TaskEstimateFeedbackService.record_feedback(
                task=updated_task,
                original_estimated_minutes=original_estimated_minutes,
                corrected_estimated_minutes=updated_task.estimated_minutes,
                corrected_by=request.user,
                source="task_edit",
            )
            if previous_status != updated_task.status:
                _close_status_gap(previous_status, exclude_pk=updated_task.pk)
            form.save_m2m()
            updated_task = _apply_task_additional_assignee_settings(updated_task)
            updated_task = _sync_task_recurring_template(updated_task)
            TaskAuditService.record_updated(updated_task, actor=request.user, before_snapshot=before_snapshot)
            messages.success(request, "Task updated.")
            return redirect("task-detail", pk=updated_task.pk)
    else:
        form = TaskManualForm(instance=task)
    return render(request, "workboard/task_form.html", {"form": form, "page_title": "Edit task"})


@supervisor_required
def task_delete_view(request, pk):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")

    task = get_object_or_404(Task, pk=pk)
    previous_status = task.status
    delete_title = task.title
    TaskAuditService.record_deleted(task, actor=request.user)
    task.delete()
    _close_status_gap(_board_bucket_status(previous_status), exclude_pk=0)
    messages.success(request, f'Task "{delete_title}" deleted.')
    return redirect("board")
