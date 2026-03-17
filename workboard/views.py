from datetime import date
from functools import wraps

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Max, Prefetch, Q, Sum
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import (
    AppPasswordChangeForm,
    RecurringTaskTemplateForm,
    StudentAvailabilityOverrideForm,
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
    StudentAvailabilityOverride,
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
from .services import TaskAssignmentService, TaskEstimateFeedbackService, TaskParsingService


BOARD_COLUMNS = [
    TaskStatus.NEW,
    TaskStatus.IN_PROGRESS,
    TaskStatus.WAITING,
    TaskStatus.REVIEW,
    TaskStatus.DONE,
]


def _board_bucket_status(status: str) -> str:
    return TaskStatus.NEW if status == TaskStatus.ASSIGNED else status


def supervisor_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if request.user.role != UserRole.SUPERVISOR:
            return HttpResponseForbidden("Supervisor access required.")
        return view_func(request, *args, **kwargs)

    return wrapped


def task_editor_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if not request.user.can_edit_tasks:
            return HttpResponseForbidden("Task editor access required.")
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


def _choose_rotating_additional_assignee(*, due_date, estimated_minutes, assigned_to_id=None, fixed_additional_ids=None, preserve_user_id=None, previous_rotating_user_id=None) -> User | None:
    excluded_ids = {user_id for user_id in (fixed_additional_ids or []) if user_id}
    if assigned_to_id:
        excluded_ids.add(assigned_to_id)

    for candidate_user_id in [preserve_user_id, previous_rotating_user_id]:
        if not candidate_user_id or candidate_user_id in excluded_ids:
            continue
        candidate = User.objects.filter(
            pk=candidate_user_id,
            role__in=UserRole.worker_roles(),
            worker_profile__active_status=True,
        ).first()
        if candidate:
            return candidate

    return TaskAssignmentService.suggest_worker_assignee(
        due_date=due_date,
        estimated_minutes=estimated_minutes,
        exclude_user_ids=list(excluded_ids),
    )


def _apply_task_additional_assignee_settings(task: Task, *, preserve_existing_rotation: bool = True, previous_rotating_user_id: int | None = None) -> Task:
    if task.assigned_to_id:
        task.additional_assignees.remove(task.assigned_to_id)

    fixed_additional_ids = list(task.additional_assignees.values_list("pk", flat=True))
    rotating_assignee = None
    if task.rotate_additional_assignee:
        rotating_assignee = _choose_rotating_additional_assignee(
            due_date=task.due_date,
            estimated_minutes=task.estimated_minutes,
            assigned_to_id=task.assigned_to_id,
            fixed_additional_ids=fixed_additional_ids,
            preserve_user_id=task.rotating_additional_assignee_id if preserve_existing_rotation else None,
            previous_rotating_user_id=previous_rotating_user_id,
        )

    if task.rotating_additional_assignee_id != (rotating_assignee.pk if rotating_assignee else None):
        task.rotating_additional_assignee = rotating_assignee
        task.save(update_fields=["rotating_additional_assignee", "updated_at"])
    return task


def _next_recurring_run_from_task(task: Task) -> date:
    seed_date = task.due_date or timezone.localdate()
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

    seed_date = task.due_date or timezone.localdate()
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
            assign_to=assignee,
            rotate_additional_assignee=task.rotate_additional_assignee,
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
    template.assign_to = assignee
    template.rotate_additional_assignee = task.rotate_additional_assignee
    template.requested_by = task.requested_by or task.created_by
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


@app_login_required
def dashboard(request):
    if request.user.can_view_full_board:
        return redirect("board")
    return redirect("my-tasks")


@app_login_required
def board_view(request):
    tasks = Task.objects.select_related("assigned_to", "requested_by", "rotating_additional_assignee").prefetch_related("additional_assignees")
    if not request.user.can_view_full_board:
        tasks = tasks.filter(Q(assigned_to=request.user) | Q(additional_assignees=request.user) | Q(rotating_additional_assignee=request.user)).distinct()
    tasks = list(tasks.order_by("status", F("board_order").asc(nulls_last=True), "due_date", "-created_at", "pk"))
    for task in tasks:
        task.status_bucket = _board_bucket_status(task.status)
        task.board_due_date = task.due_date or TaskParsingService._priority_due_date(task.priority)[1]
    grouped_tasks = [
        {"value": status, "label": TaskStatus(status).label, "tasks": [task for task in tasks if task.status_bucket == status]}
        for status in BOARD_COLUMNS
    ]
    return render(request, "workboard/board.html", {"grouped_tasks": grouped_tasks})


@app_login_required
def board_task_move_view(request, pk):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")

    task = get_object_or_404(Task.objects.select_related("rotating_additional_assignee").prefetch_related("additional_assignees"), pk=pk)
    if not request.user.can_edit_tasks and task.assigned_to_id != request.user.id and task.rotating_additional_assignee_id != request.user.id and not task.additional_assignees.filter(pk=request.user.id).exists():
        return HttpResponseForbidden("You can only move tasks assigned to you.")

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

    return JsonResponse({"ok": True, "status": task.status, "label": TaskStatus(task.status).label})


@app_login_required
def my_tasks_view(request):
    if request.user.role == UserRole.SUPERVISOR:
        tasks = Task.objects.filter(Q(assigned_to=request.user) | Q(additional_assignees=request.user) | Q(rotating_additional_assignee=request.user) | Q(status=TaskStatus.WAITING))
    else:
        tasks = Task.objects.filter(Q(assigned_to=request.user) | Q(additional_assignees=request.user) | Q(rotating_additional_assignee=request.user))
    tasks = (
        tasks.select_related("requested_by", "assigned_to", "rotating_additional_assignee")
        .prefetch_related("additional_assignees")
        .distinct()
        .order_by("status", F("board_order").asc(nulls_last=True), "due_date", "-created_at", "pk")
    )
    tasks = list(tasks)
    for task in tasks:
        task.status_bucket = _board_bucket_status(task.status)
        task.board_due_date = task.due_date or TaskParsingService._priority_due_date(task.priority)[1]
    grouped_tasks = [
        {"value": status, "label": TaskStatus(status).label, "tasks": [task for task in tasks if task.status_bucket == status]}
        for status in BOARD_COLUMNS
    ]
    return render(request, "workboard/my_tasks.html", {"grouped_tasks": grouped_tasks})


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
                )
                task.assigned_to = suggested_user
            if task.status == TaskStatus.DONE and not task.completed_at:
                task.mark_complete()
            task.save()
            form.save_m2m()
            task = _apply_task_additional_assignee_settings(task, preserve_existing_rotation=False)
            task = _sync_task_recurring_template(task)
            messages.success(request, "Task created.")
            return redirect("task-detail", pk=task.pk)
    else:
        form = TaskManualForm()
    return render(request, "workboard/task_form.html", {"form": form, "page_title": "Create task"})


@app_login_required
def task_detail_view(request, pk):
    task = get_object_or_404(
        Task.objects.select_related("assigned_to", "requested_by", "created_by", "rotating_additional_assignee")
        .prefetch_related("additional_assignees", "checklist_items", "attachments", "notes__author"),
        pk=pk,
    )
    if not request.user.can_view_full_board and task.assigned_to_id != request.user.id and task.rotating_additional_assignee_id != request.user.id and not task.additional_assignees.filter(pk=request.user.id).exists():
        return HttpResponseForbidden("You can only view tasks assigned to you.")

    note_form = TaskNoteForm()
    status_form = TaskUpdateForm(instance=task)
    checklist_form = TaskChecklistItemForm()
    attachment_form = TaskAttachmentForm()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "status":
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
                messages.success(request, "Task status updated.")
                return redirect("task-detail", pk=task.pk)
        elif action == "note":
            note_form = TaskNoteForm(request.POST)
            if note_form.is_valid():
                note = note_form.save(commit=False)
                note.task = task
                note.author = request.user
                note.save()
                messages.success(request, "Note added.")
                return redirect("task-detail", pk=task.pk)
        elif action == "attachment":
            attachment_form = TaskAttachmentForm(request.POST, request.FILES)
            if attachment_form.is_valid():
                attachment = attachment_form.save(commit=False)
                attachment.task = task
                attachment.original_name = attachment.file.name
                attachment.save()
                messages.success(request, "Attachment added.")
                return redirect("task-detail", pk=task.pk)
        elif action == "checklist" and request.user.can_edit_tasks:
            checklist_form = TaskChecklistItemForm(request.POST)
            if checklist_form.is_valid():
                item = checklist_form.save(commit=False)
                item.task = task
                item.position = _next_checklist_position(task)
                item.save()
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
    return render(
        request,
        "workboard/task_detail.html",
        {
            "task": task,
            "note_form": note_form,
            "status_form": status_form,
            "checklist_form": checklist_form,
            "attachment_form": attachment_form,
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
    task.delete()
    _close_status_gap(_board_bucket_status(previous_status), exclude_pk=0)
    messages.success(request, f'Task "{delete_title}" deleted.')
    return redirect("board")


@supervisor_required
def recurring_template_list_view(request):
    _backfill_orphan_recurring_tasks()
    templates = (
        RecurringTaskTemplate.objects.select_related("assign_to", "requested_by")
        .prefetch_related("additional_assignees")
        .annotate(generated_task_count=Count("generated_tasks"))
        .order_by(F("display_order").asc(nulls_last=True), "next_run_date", "title", "pk")
    )
    return render(request, "workboard/recurring_template_list.html", {"templates": templates})


@supervisor_required
def recurring_template_detail_view(request, pk):
    template = get_object_or_404(
        RecurringTaskTemplate.objects.select_related("assign_to", "requested_by").prefetch_related("additional_assignees", "generated_tasks"),
        pk=pk,
    )
    recent_tasks = list(template.generated_tasks.order_by("-created_at")[:10])
    return render(
        request,
        "workboard/recurring_template_detail.html",
        {"template": template, "recent_tasks": recent_tasks},
    )


@supervisor_required
def recurring_template_move_view(request, pk):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")

    template = get_object_or_404(RecurringTaskTemplate, pk=pk)
    before_template_id = request.POST.get("before_template_id", "").strip()
    ordered_templates = _ordered_recurring_templates(exclude_pk=template.pk)
    if before_template_id:
        try:
            before_template_id_int = int(before_template_id)
        except ValueError:
            return HttpResponseBadRequest("Invalid target position.")
        before_template = next((item for item in ordered_templates if item.pk == before_template_id_int), None)
        if before_template is None:
            return HttpResponseBadRequest("Target recurring task not found.")
        insert_index = ordered_templates.index(before_template)
    else:
        insert_index = len(ordered_templates)

    ordered_templates.insert(insert_index, template)
    _resequence_recurring_templates(ordered_templates)
    return JsonResponse({"ok": True})



@supervisor_required
def recurring_template_edit_view(request, pk):
    template = get_object_or_404(RecurringTaskTemplate, pk=pk)
    if request.method == "POST":
        form = RecurringTaskTemplateForm(request.POST, instance=template)
        if form.is_valid():
            form.save()
            messages.success(request, "Recurring task updated.")
            return redirect("recurring-detail", pk=template.pk)
    else:
        form = RecurringTaskTemplateForm(instance=template)
    return render(
        request,
        "workboard/recurring_template_form.html",
        {"form": form, "page_title": "Edit recurring task", "submit_label": "Save changes"},
    )

@supervisor_required
def worker_list_view(request):
    worker_profiles = list(
        StudentWorkerProfile.objects.select_related("user")
        .filter(user__role__in=UserRole.worker_roles())
        .order_by("display_name")
    )
    worker_workload = {
        worker.pk: worker
        for worker in User.objects.filter(role__in=UserRole.worker_roles())
        .annotate(
            active_tasks=Count("assigned_tasks", filter=~Q(assigned_tasks__status=TaskStatus.DONE)),
            estimated_minutes=Sum("assigned_tasks__estimated_minutes", filter=~Q(assigned_tasks__status=TaskStatus.DONE)),
        )
    }
    students = [
        {"profile": profile, "workload": worker_workload.get(profile.user_id)}
        for profile in worker_profiles
        if profile.user.role == UserRole.STUDENT_WORKER
    ]
    student_supervisors = [
        {"profile": profile, "workload": worker_workload.get(profile.user_id)}
        for profile in worker_profiles
        if profile.user.role == UserRole.STUDENT_SUPERVISOR
    ]
    supervisors = (
        User.objects.filter(role=UserRole.SUPERVISOR)
        .annotate(
            active_tasks=Count("assigned_tasks", filter=~Q(assigned_tasks__status=TaskStatus.DONE)),
            estimated_minutes=Sum("assigned_tasks__estimated_minutes", filter=~Q(assigned_tasks__status=TaskStatus.DONE)),
        )
        .order_by("username")
    )
    return render(
        request,
        "workboard/worker_list.html",
        {"students": students, "student_supervisors": student_supervisors, "supervisors": supervisors},
    )


@supervisor_required
def worker_availability_view(request, pk):
    profile = get_object_or_404(StudentWorkerProfile.objects.select_related("user"), pk=pk)
    weekly_map = {item.weekday: item for item in profile.weekly_availability.all()}
    initial = {
        "monday_hours": weekly_map.get(Weekday.MONDAY).hours_available if weekly_map.get(Weekday.MONDAY) else 0,
        "tuesday_hours": weekly_map.get(Weekday.TUESDAY).hours_available if weekly_map.get(Weekday.TUESDAY) else 0,
        "wednesday_hours": weekly_map.get(Weekday.WEDNESDAY).hours_available if weekly_map.get(Weekday.WEDNESDAY) else 0,
        "thursday_hours": weekly_map.get(Weekday.THURSDAY).hours_available if weekly_map.get(Weekday.THURSDAY) else 0,
        "friday_hours": weekly_map.get(Weekday.FRIDAY).hours_available if weekly_map.get(Weekday.FRIDAY) else 0,
        "saturday_hours": weekly_map.get(Weekday.SATURDAY).hours_available if weekly_map.get(Weekday.SATURDAY) else 0,
        "sunday_hours": weekly_map.get(Weekday.SUNDAY).hours_available if weekly_map.get(Weekday.SUNDAY) else 0,
    }
    worker_form = StudentWorkerProfileForm(instance=profile)
    weekly_form = WeeklyAvailabilityForm(initial=initial)
    override_form = StudentAvailabilityOverrideForm(profile=profile)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "worker":
            worker_form = StudentWorkerProfileForm(request.POST, instance=profile)
            weekly_form = WeeklyAvailabilityForm(request.POST)
            if worker_form.is_valid() and weekly_form.is_valid():
                profile = worker_form.save(commit=False)
                user = profile.user
                user.username = request.POST.get("username", "").strip() or user.username
                user.first_name = request.POST.get("first_name", "").strip()
                user.last_name = request.POST.get("last_name", "").strip()
                user.email = worker_form.cleaned_data["email"]
                user.save()
                profile.email = worker_form.cleaned_data["email"]
                profile.display_name = user.get_full_name().strip() or user.username
                profile.save()
                field_map = {
                    Weekday.MONDAY: "monday_hours",
                    Weekday.TUESDAY: "tuesday_hours",
                    Weekday.WEDNESDAY: "wednesday_hours",
                    Weekday.THURSDAY: "thursday_hours",
                    Weekday.FRIDAY: "friday_hours",
                    Weekday.SATURDAY: "saturday_hours",
                    Weekday.SUNDAY: "sunday_hours",
                }
                for weekday, field_name in field_map.items():
                    StudentAvailability.objects.update_or_create(
                        profile=profile,
                        weekday=weekday,
                        defaults={"hours_available": weekly_form.cleaned_data[field_name]},
                    )
                messages.success(request, "Worker updated.")
                return redirect("worker-availability", pk=profile.pk)
        elif action == "override":
            worker_form = StudentWorkerProfileForm(instance=profile)
            weekly_form = WeeklyAvailabilityForm(initial=initial)
            override_form = StudentAvailabilityOverrideForm(request.POST, profile=profile)
            if override_form.is_valid():
                override = override_form.save(commit=False)
                override.profile = profile
                override.created_by = request.user
                override.save()
                messages.success(request, "Temporary hour override saved.")
                return redirect("worker-availability", pk=profile.pk)
        elif action == "delete_override":
            override = get_object_or_404(StudentAvailabilityOverride, pk=request.POST.get("override_id"), profile=profile)
            override.delete()
            messages.success(request, "Temporary override removed.")
            return redirect("worker-availability", pk=profile.pk)

    return render(
        request,
        "workboard/worker_availability.html",
        {
            "profile": profile,
            "worker_form": worker_form,
            "weekly_form": weekly_form,
            "override_form": override_form,
            "overrides": profile.availability_overrides.all(),
        },
    )


@supervisor_required
def supervisor_create_view(request):
    form = SupervisorForm(request.POST or None)
    context = {"form": form, "page_title": "Add supervisor", "submit_label": "Create supervisor"}
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip() or "changeme123"
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()
        email = request.POST.get("email", "").strip()
        if not username:
            messages.error(request, "Username is required.")
            return render(request, "workboard/supervisor_form.html", context)
        user = User.objects.create_user(
            username=username,
            password=password,
            first_name=first_name,
            last_name=last_name,
            email=email,
            role=UserRole.SUPERVISOR,
            must_change_password=True,
            assignable_to_tasks=True,
        )
        form = SupervisorForm(request.POST, instance=user)
        context["form"] = form
        if form.is_valid():
            form.save()
            messages.success(request, "Supervisor created.")
            return redirect("worker-list")
        user.delete()
    return render(request, "workboard/supervisor_form.html", context)


@supervisor_required
def student_supervisor_create_view(request):
    form = StudentWorkerProfileForm(request.POST or None)
    weekly_form = WeeklyAvailabilityForm(request.POST or None, initial={
        "monday_hours": 0,
        "tuesday_hours": 0,
        "wednesday_hours": 0,
        "thursday_hours": 0,
        "friday_hours": 0,
        "saturday_hours": 0,
        "sunday_hours": 0,
    })
    context = {
        "form": form,
        "weekly_form": weekly_form,
        "page_title": "Add student supervisor",
        "submit_label": "Create student supervisor",
        "worker_type_label": "student supervisor",
    }
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip() or "changeme123"
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()
        email = request.POST.get("email", "").strip()
        if not username:
            messages.error(request, "Username is required.")
            return render(request, "workboard/worker_form.html", context)
        if form.is_valid() and weekly_form.is_valid():
            user = User.objects.create_user(
                username=username,
                password=password,
                first_name=first_name,
                last_name=last_name,
                email=email,
                role=UserRole.STUDENT_SUPERVISOR,
                must_change_password=True,
            )
            profile = form.save(commit=False)
            profile.user = user
            profile.email = profile.email or email
            profile.display_name = user.get_full_name().strip() or user.username
            profile.save()
            field_map = {
                Weekday.MONDAY: "monday_hours",
                Weekday.TUESDAY: "tuesday_hours",
                Weekday.WEDNESDAY: "wednesday_hours",
                Weekday.THURSDAY: "thursday_hours",
                Weekday.FRIDAY: "friday_hours",
                Weekday.SATURDAY: "saturday_hours",
                Weekday.SUNDAY: "sunday_hours",
            }
            for weekday, field_name in field_map.items():
                StudentAvailability.objects.update_or_create(
                    profile=profile,
                    weekday=weekday,
                    defaults={"hours_available": weekly_form.cleaned_data[field_name]},
                )
            messages.success(request, "Student supervisor created.")
            return redirect("worker-list")
    return render(request, "workboard/worker_form.html", context)


@supervisor_required
def worker_profile_create_view(request):
    form = StudentWorkerProfileForm(request.POST or None)
    weekly_form = WeeklyAvailabilityForm(request.POST or None, initial={
        "monday_hours": 0,
        "tuesday_hours": 0,
        "wednesday_hours": 0,
        "thursday_hours": 0,
        "friday_hours": 0,
        "saturday_hours": 0,
        "sunday_hours": 0,
    })
    context = {"form": form, "weekly_form": weekly_form, "page_title": "Add student", "submit_label": "Create student"}
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip() or "changeme123"
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()
        email = request.POST.get("email", "").strip()
        if not username:
            messages.error(request, "Username is required.")
            return render(request, "workboard/worker_form.html", context)
        if form.is_valid() and weekly_form.is_valid():
            user = User.objects.create_user(
                username=username,
                password=password,
                first_name=first_name,
                last_name=last_name,
                email=email,
                role=UserRole.STUDENT_WORKER,
                must_change_password=True,
            )
            profile = form.save(commit=False)
            profile.user = user
            profile.email = profile.email or email
            profile.display_name = user.get_full_name().strip() or user.username
            profile.save()
            field_map = {
                Weekday.MONDAY: "monday_hours",
                Weekday.TUESDAY: "tuesday_hours",
                Weekday.WEDNESDAY: "wednesday_hours",
                Weekday.THURSDAY: "thursday_hours",
                Weekday.FRIDAY: "friday_hours",
                Weekday.SATURDAY: "saturday_hours",
                Weekday.SUNDAY: "sunday_hours",
            }
            for weekday, field_name in field_map.items():
                StudentAvailability.objects.update_or_create(
                    profile=profile,
                    weekday=weekday,
                    defaults={"hours_available": weekly_form.cleaned_data[field_name]},
                )
            messages.success(request, "Student worker created.")
            return redirect("worker-list")
    return render(request, "workboard/worker_form.html", context)


@supervisor_required
def worker_profile_delete_view(request, pk):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")

    worker = get_object_or_404(User.objects.select_related("worker_profile"), pk=pk, role__in=UserRole.worker_roles())
    Task.objects.filter(assigned_to=worker).update(assigned_to=request.user, updated_at=timezone.now())
    Task.objects.filter(rotating_additional_assignee=worker).update(rotating_additional_assignee=None, updated_at=timezone.now())
    Task.additional_assignees.through.objects.filter(user_id=worker.pk).delete()
    RecurringTaskTemplate.objects.filter(assign_to=worker).update(assign_to=request.user, updated_at=timezone.now())
    RecurringTaskTemplate.additional_assignees.through.objects.filter(user_id=worker.pk).delete()
    worker_name = worker.display_label
    worker.delete()
    messages.success(request, f"Removed {worker_name}. Any assigned tasks were reassigned to you.")
    return redirect("worker-list")


@supervisor_required
def supervisor_delete_view(request, pk):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")

    supervisor = get_object_or_404(User, pk=pk, role=UserRole.SUPERVISOR)
    if supervisor.pk == request.user.pk:
        messages.error(request, "You cannot remove your own supervisor account while logged in.")
        return redirect("worker-list")

    Task.objects.filter(assigned_to=supervisor).update(assigned_to=request.user, updated_at=timezone.now())
    RecurringTaskTemplate.objects.filter(assign_to=supervisor).update(assign_to=request.user, updated_at=timezone.now())
    supervisor_name = supervisor.display_label
    supervisor.delete()
    messages.success(request, f"Removed {supervisor_name}. Any assigned tasks were reassigned to you.")
    return redirect("worker-list")


@supervisor_required
def supervisor_edit_view(request, pk):
    supervisor = get_object_or_404(User, pk=pk, role=UserRole.SUPERVISOR)
    form = SupervisorForm(request.POST or None, instance=supervisor)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Supervisor updated.")
        return redirect("worker-list")
    return render(
        request,
        "workboard/supervisor_form.html",
        {"form": form, "page_title": f"Edit supervisor: {supervisor.display_label}", "supervisor": supervisor},
    )


@supervisor_required
def worker_password_reset_view(request, pk):
    person = get_object_or_404(User, pk=pk, role__in=[UserRole.STUDENT_WORKER, UserRole.STUDENT_SUPERVISOR, UserRole.SUPERVISOR])
    if request.method == "POST":
        form = SupervisorStudentPasswordResetForm(person, request.POST)
        if form.is_valid():
            form.save()
            person.must_change_password = True
            person.save(update_fields=["must_change_password"])
            messages.success(request, f"Password reset for {person.display_label}. They must create a new password at next login.")
            return redirect("worker-list")
    else:
        form = SupervisorStudentPasswordResetForm(person)
    return render(
        request,
        "workboard/worker_password_reset_form.html",
        {"form": form, "person": person},
    )







