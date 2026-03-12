from functools import wraps

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from .forms import (
    AppPasswordChangeForm,
    RecurringTaskTemplateForm,
    StudentAvailabilityOverrideForm,
    StudentWorkerProfileForm,
    SupervisorStudentPasswordResetForm,
    TaskForm,
    TaskChecklistItemForm,
    TaskIntakeForm,
    TaskNoteForm,
    TaskUpdateForm,
    WeeklyAvailabilityForm,
)
from .models import (
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
from .services import TaskAssignmentService, TaskParsingService


BOARD_COLUMNS = [
    TaskStatus.NEW,
    TaskStatus.ASSIGNED,
    TaskStatus.IN_PROGRESS,
    TaskStatus.WAITING,
    TaskStatus.REVIEW,
    TaskStatus.DONE,
]


def supervisor_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if request.user.role != UserRole.SUPERVISOR:
            return HttpResponseForbidden("Supervisor access required.")
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


@app_login_required
def dashboard(request):
    if request.user.role == UserRole.SUPERVISOR:
        return redirect("board")
    return redirect("my-tasks")


@app_login_required
def board_view(request):
    tasks = Task.objects.select_related("assigned_to", "requested_by").all()
    if request.user.role == UserRole.STUDENT_WORKER:
        tasks = tasks.filter(assigned_to=request.user)
    grouped_tasks = [
        {"value": status, "label": TaskStatus(status).label, "tasks": tasks.filter(status=status)}
        for status in BOARD_COLUMNS
    ]
    return render(request, "workboard/board.html", {"grouped_tasks": grouped_tasks})


@app_login_required
def my_tasks_view(request):
    tasks = Task.objects.filter(assigned_to=request.user).select_related("requested_by")
    return render(request, "workboard/my_tasks.html", {"tasks": tasks})


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
                attachments=list(draft.attachments.all()),
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

    if request.method == "POST":
        form = TaskForm(request.POST, initial=initial)
        if form.is_valid():
            task = form.save(commit=False)
            task.created_by = request.user
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
            for attachment in draft.attachments.all():
                TaskAttachment.objects.create(
                    task=task,
                    file=attachment.file.name,
                    original_name=attachment.original_name,
                )
            for index, title in enumerate(initial.get("checklist_items", []), start=1):
                TaskChecklistItem.objects.create(task=task, title=title, sort_order=index)
            messages.success(request, "Task created from intake request.")
            return redirect("task-detail", pk=task.pk)
    else:
        form = TaskForm(initial=initial)

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
        },
    )


@supervisor_required
def task_create_view(request):
    if request.method == "POST":
        form = TaskForm(request.POST)
        if form.is_valid():
            task = form.save(commit=False)
            task.created_by = request.user
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
            messages.success(request, "Task created.")
            return redirect("task-detail", pk=task.pk)
    else:
        form = TaskForm()
    return render(request, "workboard/task_form.html", {"form": form, "page_title": "Create task"})


@app_login_required
def task_detail_view(request, pk):
    task = get_object_or_404(Task.objects.select_related("assigned_to", "requested_by", "created_by"), pk=pk)
    if request.user.role == UserRole.STUDENT_WORKER and task.assigned_to_id != request.user.id:
        return HttpResponseForbidden("You can only view tasks assigned to you.")

    note_form = TaskNoteForm()
    status_form = TaskUpdateForm(instance=task)
    checklist_form = TaskChecklistItemForm()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "status":
            status_form = TaskUpdateForm(request.POST, instance=task)
            if status_form.is_valid():
                updated_task = status_form.save(commit=False)
                if updated_task.status == TaskStatus.DONE and not updated_task.completed_at:
                    updated_task.mark_complete()
                elif updated_task.status != TaskStatus.DONE:
                    updated_task.completed_at = None
                updated_task.save()
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
        elif action == "checklist" and request.user.role == UserRole.SUPERVISOR:
            checklist_form = TaskChecklistItemForm(request.POST)
            if checklist_form.is_valid():
                item = checklist_form.save(commit=False)
                item.task = task
                item.save()
                messages.success(request, "Checklist item added.")
                return redirect("task-detail", pk=task.pk)

    return render(
        request,
        "workboard/task_detail.html",
        {"task": task, "note_form": note_form, "status_form": status_form, "checklist_form": checklist_form},
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


@supervisor_required
def task_edit_view(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if request.method == "POST":
        form = TaskForm(request.POST, instance=task)
        if form.is_valid():
            updated_task = form.save(commit=False)
            if updated_task.status == TaskStatus.DONE and not updated_task.completed_at:
                updated_task.mark_complete()
            elif updated_task.status != TaskStatus.DONE:
                updated_task.completed_at = None
            updated_task.save()
            messages.success(request, "Task updated.")
            return redirect("task-detail", pk=updated_task.pk)
    else:
        form = TaskForm(instance=task)
    return render(request, "workboard/task_form.html", {"form": form, "page_title": "Edit task"})


@supervisor_required
def recurring_template_create_view(request):
    if request.method == "POST":
        form = RecurringTaskTemplateForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Recurring template created.")
            return redirect("board")
    else:
        form = RecurringTaskTemplateForm()
    return render(request, "workboard/recurring_template_form.html", {"form": form})


@supervisor_required
def worker_list_view(request):
    profiles = StudentWorkerProfile.objects.select_related("user").order_by("display_name")
    workload = (
        User.objects.filter(role=UserRole.STUDENT_WORKER)
        .annotate(
            active_tasks=Count("assigned_tasks", filter=~Q(assigned_tasks__status=TaskStatus.DONE)),
            estimated_minutes=Sum("assigned_tasks__estimated_minutes", filter=~Q(assigned_tasks__status=TaskStatus.DONE)),
        )
        .order_by("username")
    )
    return render(request, "workboard/worker_list.html", {"profiles": profiles, "workload": workload})


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
    weekly_form = WeeklyAvailabilityForm(initial=initial)
    override_form = StudentAvailabilityOverrideForm()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "weekly":
            weekly_form = WeeklyAvailabilityForm(request.POST)
            if weekly_form.is_valid():
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
                messages.success(request, "Weekly hours updated.")
                return redirect("worker-availability", pk=profile.pk)
        elif action == "override":
            override_form = StudentAvailabilityOverrideForm(request.POST)
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
            "weekly_form": weekly_form,
            "override_form": override_form,
            "overrides": profile.availability_overrides.all(),
        },
    )


@supervisor_required
def worker_profile_create_view(request):
    form = StudentWorkerProfileForm(request.POST or None)
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip() or "changeme123"
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()
        email = request.POST.get("email", "").strip()
        if not username:
            messages.error(request, "Username is required.")
            return render(request, "workboard/worker_form.html", {"form": form})
        user = User.objects.create_user(
            username=username,
            password=password,
            first_name=first_name,
            last_name=last_name,
            email=email,
            role=UserRole.STUDENT_WORKER,
            must_change_password=True,
        )
        profile = StudentWorkerProfile(user=user, email=email, display_name=username)
        form = StudentWorkerProfileForm(request.POST, instance=profile)
        if form.is_valid():
            profile = form.save()
            for weekday in Weekday.values:
                StudentAvailability.objects.get_or_create(profile=profile, weekday=weekday, defaults={"hours_available": 0})
            messages.success(request, "Student worker created.")
            return redirect("worker-list")
        user.delete()
    return render(request, "workboard/worker_form.html", {"form": form})


@supervisor_required
def worker_password_reset_view(request, pk):
    student = get_object_or_404(User, pk=pk, role=UserRole.STUDENT_WORKER)
    if request.method == "POST":
        form = SupervisorStudentPasswordResetForm(student, request.POST)
        if form.is_valid():
            form.save()
            student.must_change_password = True
            student.save(update_fields=["must_change_password"])
            messages.success(request, f"Password reset for {student.display_label}. They must create a new password at next login.")
            return redirect("worker-list")
    else:
        form = SupervisorStudentPasswordResetForm(student)
    return render(
        request,
        "workboard/worker_password_reset_form.html",
        {"form": form, "student": student},
    )
