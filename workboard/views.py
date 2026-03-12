from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from .forms import RecurringTaskTemplateForm, StudentWorkerProfileForm, TaskForm, TaskIntakeForm, TaskNoteForm, TaskUpdateForm
from .models import StudentWorkerProfile, Task, TaskStatus, User, UserRole
from .services import TaskParsingService


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


@login_required
def dashboard(request):
    if request.user.role == UserRole.SUPERVISOR:
        return redirect("board")
    return redirect("my-tasks")


@login_required
def board_view(request):
    tasks = Task.objects.select_related("assigned_to", "requested_by").all()
    if request.user.role == UserRole.STUDENT_WORKER:
        tasks = tasks.filter(assigned_to=request.user)
    grouped_tasks = [
        {"value": status, "label": TaskStatus(status).label, "tasks": tasks.filter(status=status)}
        for status in BOARD_COLUMNS
    ]
    return render(request, "workboard/board.html", {"grouped_tasks": grouped_tasks})


@login_required
def my_tasks_view(request):
    tasks = Task.objects.filter(assigned_to=request.user).select_related("requested_by")
    return render(request, "workboard/my_tasks.html", {"tasks": tasks})


@supervisor_required
def task_intake_view(request):
    if request.method == "POST":
        form = TaskIntakeForm(request.POST)
        if form.is_valid():
            request.session["task_intake_data"] = TaskParsingService.parse_request(form.cleaned_data["raw_message"]).__dict__
            return redirect("task-intake-review")
    else:
        form = TaskIntakeForm()
    return render(request, "workboard/task_intake.html", {"form": form})


@supervisor_required
def task_intake_review_view(request):
    initial = request.session.get("task_intake_data")
    if not initial:
        messages.error(request, "Start with the intake form first.")
        return redirect("task-intake")

    if request.method == "POST":
        form = TaskForm(request.POST, initial=initial)
        if form.is_valid():
            task = form.save(commit=False)
            task.created_by = request.user
            if task.status == TaskStatus.DONE and not task.completed_at:
                task.mark_complete()
            task.save()
            request.session.pop("task_intake_data", None)
            messages.success(request, "Task created from intake request.")
            return redirect("task-detail", pk=task.pk)
    else:
        form = TaskForm(initial=initial)

    return render(request, "workboard/task_form.html", {"form": form, "page_title": "Review extracted task"})


@supervisor_required
def task_create_view(request):
    if request.method == "POST":
        form = TaskForm(request.POST)
        if form.is_valid():
            task = form.save(commit=False)
            task.created_by = request.user
            if task.status == TaskStatus.DONE and not task.completed_at:
                task.mark_complete()
            task.save()
            messages.success(request, "Task created.")
            return redirect("task-detail", pk=task.pk)
    else:
        form = TaskForm()
    return render(request, "workboard/task_form.html", {"form": form, "page_title": "Create task"})


@login_required
def task_detail_view(request, pk):
    task = get_object_or_404(Task.objects.select_related("assigned_to", "requested_by", "created_by"), pk=pk)
    if request.user.role == UserRole.STUDENT_WORKER and task.assigned_to_id != request.user.id:
        return HttpResponseForbidden("You can only view tasks assigned to you.")

    note_form = TaskNoteForm()
    status_form = TaskUpdateForm(instance=task)

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

    return render(request, "workboard/task_detail.html", {"task": task, "note_form": note_form, "status_form": status_form})


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
        )
        profile = StudentWorkerProfile(user=user, email=email, display_name=username)
        form = StudentWorkerProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, "Student worker created.")
            return redirect("worker-list")
        user.delete()
    return render(request, "workboard/worker_form.html", {"form": form})
