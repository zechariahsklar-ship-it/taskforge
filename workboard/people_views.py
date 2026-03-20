from datetime import date

from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.crypto import get_random_string

from .forms import StudentScheduleOverrideForm, StudentWorkerProfileForm, SupervisorForm, SupervisorStudentPasswordResetForm, WeeklyAvailabilityForm
from .models import RecurringTaskTemplate, StudentWorkerProfile, StudentScheduleOverride, Task, TaskStatus, User, UserRole
from .task_views import _save_schedule_override, _save_weekly_schedule, _serialize_blocks_for_initial, _weekly_schedule_initial, supervisor_required


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


def _posted_account_details(post_data) -> dict:
    return {
        "username": post_data.get("username", "").strip(),
        "password": post_data.get("password", "").strip(),
        "first_name": post_data.get("first_name", "").strip(),
        "last_name": post_data.get("last_name", "").strip(),
        "email": post_data.get("email", "").strip(),
    }


def _resolved_account_password(account: dict) -> tuple[str, bool]:
    if account["password"]:
        return account["password"], False
    return get_random_string(18), True


def _queue_temporary_password_notice(request, *, display_name: str, password: str) -> None:
    messages.warning(
        request,
        f"No password was entered for {display_name}. TaskForge generated a temporary password: {password}",
    )


def _save_worker_profile_details(profile: StudentWorkerProfile, worker_form: StudentWorkerProfileForm, post_data) -> bool:
    user = profile.user
    username = post_data.get("username", "").strip()
    if not username:
        worker_form.add_error(None, "Username is required.")
        return False
    if User.objects.exclude(pk=user.pk).filter(username=username).exists():
        worker_form.add_error(None, "That username is already in use.")
        return False

    user.username = username
    user.first_name = post_data.get("first_name", "").strip()
    user.last_name = post_data.get("last_name", "").strip()
    user.email = worker_form.cleaned_data["email"]
    user.save()

    updated_profile = worker_form.save(commit=False)
    updated_profile.user = user
    updated_profile.email = worker_form.cleaned_data["email"]
    updated_profile.display_name = user.get_full_name().strip() or user.username
    updated_profile.save()
    return True


def _create_worker_profile_account(
    request,
    *,
    role: str,
    page_title: str,
    submit_label: str,
    success_message: str,
    worker_type_label: str,
):
    form = StudentWorkerProfileForm(request.POST or None)
    weekly_form = WeeklyAvailabilityForm(request.POST or None)
    context = {
        "form": form,
        "weekly_form": weekly_form,
        "page_title": page_title,
        "submit_label": submit_label,
        "worker_type_label": worker_type_label,
    }
    if request.method == "POST":
        account = _posted_account_details(request.POST)
        if not account["username"]:
            messages.error(request, "Username is required.")
            return render(request, "workboard/worker_form.html", context)
        if User.objects.filter(username=account["username"]).exists():
            messages.error(request, "That username is already in use.")
            return render(request, "workboard/worker_form.html", context)
        if form.is_valid() and weekly_form.is_valid():
            password, generated_password = _resolved_account_password(account)
            user = User.objects.create_user(
                username=account["username"],
                password=password,
                first_name=account["first_name"],
                last_name=account["last_name"],
                email=account["email"],
                role=role,
                must_change_password=True,
            )
            profile = form.save(commit=False)
            profile.user = user
            profile.email = profile.email or account["email"]
            profile.display_name = user.get_full_name().strip() or user.username
            profile.save()
            _save_weekly_schedule(profile, weekly_form)
            messages.success(request, success_message)
            if generated_password:
                _queue_temporary_password_notice(request, display_name=profile.display_name, password=password)
            return redirect("worker-list")
    return render(request, "workboard/worker_form.html", context)


def _parse_override_date(raw_value: str) -> date | None:
    if not raw_value:
        return None
    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        return None


def _schedule_override_form_initial(profile: StudentWorkerProfile, override_date_value: date | None) -> tuple[StudentScheduleOverride | None, dict]:
    if not override_date_value:
        return None, {}

    existing_override = profile.schedule_overrides.prefetch_related("blocks").filter(override_date=override_date_value).first()
    if existing_override:
        return existing_override, {"override_date": override_date_value}

    availability = profile.weekly_availability.prefetch_related("blocks").filter(weekday=override_date_value.weekday()).first()
    initial = {"override_date": override_date_value}
    if availability:
        blocks = [
            (block.start_time, block.end_time)
            for block in availability.blocks.order_by("position", "start_time", "end_time", "pk")
        ]
        if blocks:
            initial["override_segments"] = _serialize_blocks_for_initial(blocks)
    return None, initial


def _build_schedule_override_form(profile: StudentWorkerProfile, *, override_date_value: date | None = None, data=None):
    instance, initial = _schedule_override_form_initial(profile, override_date_value)
    return StudentScheduleOverrideForm(data=data, instance=instance, initial=initial, profile=profile)


def _worker_role_page_context(profile: StudentWorkerProfile) -> dict:
    if profile.user.role == UserRole.STUDENT_SUPERVISOR:
        return {
            "page_title": f"Edit student supervisor: {profile.display_name}",
            "schedule_title": f"Edit schedule: {profile.display_name}",
            "role_label": "student supervisor",
            "remove_label": "Remove student supervisor",
            "remove_confirm": "Remove this student supervisor? Assigned tasks will be reassigned to you.",
            "success_label": "Student supervisor updated.",
        }
    return {
        "page_title": f"Edit worker: {profile.display_name}",
        "schedule_title": f"Edit schedule: {profile.display_name}",
        "role_label": "worker",
        "remove_label": "Remove student",
        "remove_confirm": "Remove this student? Assigned tasks will be reassigned to you.",
        "success_label": "Worker updated.",
    }


@supervisor_required
def worker_edit_view(request, pk):
    profile = get_object_or_404(StudentWorkerProfile.objects.select_related("user"), pk=pk)
    context_labels = _worker_role_page_context(profile)
    worker_form = StudentWorkerProfileForm(request.POST or None, instance=profile)

    if request.method == "POST" and worker_form.is_valid() and _save_worker_profile_details(profile, worker_form, request.POST):
        messages.success(request, context_labels["success_label"])
        return redirect("worker-edit", pk=profile.pk)

    return render(
        request,
        "workboard/worker_edit.html",
        {
            "profile": profile,
            "worker_form": worker_form,
            **context_labels,
        },
    )


@supervisor_required
def worker_schedule_view(request, pk):
    profile = get_object_or_404(StudentWorkerProfile.objects.select_related("user"), pk=pk)
    context_labels = _worker_role_page_context(profile)
    initial = _weekly_schedule_initial(profile)
    selected_override_date = _parse_override_date(request.GET.get("override_date", ""))
    weekly_form = WeeklyAvailabilityForm(initial=initial)
    schedule_override_form = _build_schedule_override_form(profile, override_date_value=selected_override_date)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "weekly":
            weekly_form = WeeklyAvailabilityForm(request.POST)
            schedule_override_form = _build_schedule_override_form(profile, override_date_value=selected_override_date)
            if weekly_form.is_valid():
                _save_weekly_schedule(profile, weekly_form)
                messages.success(request, "Weekly schedule updated.")
                return redirect("worker-schedule", pk=profile.pk)
        elif action == "schedule_override":
            weekly_form = WeeklyAvailabilityForm(initial=initial)
            selected_override_date = _parse_override_date(request.POST.get("override_date", ""))
            schedule_override_form = _build_schedule_override_form(
                profile,
                override_date_value=selected_override_date,
                data=request.POST,
            )
            if schedule_override_form.is_valid():
                schedule_override = _save_schedule_override(profile, schedule_override_form, request.user)
                messages.success(request, f"Temporary schedule saved for {schedule_override.override_date}.")
                return redirect("worker-schedule", pk=profile.pk)
        elif action == "delete_schedule_override":
            schedule_override = get_object_or_404(StudentScheduleOverride, pk=request.POST.get("schedule_override_id"), profile=profile)
            schedule_override.delete()
            messages.success(request, "Temporary schedule removed.")
            return redirect("worker-schedule", pk=profile.pk)

    return render(
        request,
        "workboard/worker_availability.html",
        {
            "profile": profile,
            "weekly_form": weekly_form,
            "schedule_override_form": schedule_override_form,
            "schedule_overrides": profile.schedule_overrides.prefetch_related("blocks").all(),
            "selected_override_date": selected_override_date,
            **context_labels,
        },
    )


@supervisor_required
def supervisor_create_view(request):
    form = SupervisorForm(request.POST or None)
    context = {"form": form, "page_title": "Add supervisor", "submit_label": "Create supervisor"}
    if request.method == "POST":
        account = _posted_account_details(request.POST)
        if not account["username"]:
            messages.error(request, "Username is required.")
            return render(request, "workboard/supervisor_form.html", context)
        if User.objects.filter(username=account["username"]).exists():
            messages.error(request, "That username is already in use.")
            return render(request, "workboard/supervisor_form.html", context)
        password, generated_password = _resolved_account_password(account)
        user = User.objects.create_user(
            username=account["username"],
            password=password,
            first_name=account["first_name"],
            last_name=account["last_name"],
            email=account["email"],
            role=UserRole.SUPERVISOR,
            must_change_password=True,
            assignable_to_tasks=True,
        )
        form = SupervisorForm(request.POST, instance=user)
        context["form"] = form
        if form.is_valid():
            form.save()
            messages.success(request, "Supervisor created.")
            if generated_password:
                _queue_temporary_password_notice(request, display_name=user.get_full_name().strip() or user.username, password=password)
            return redirect("worker-list")
        user.delete()
    return render(request, "workboard/supervisor_form.html", context)


@supervisor_required
def student_supervisor_create_view(request):
    return _create_worker_profile_account(
        request,
        role=UserRole.STUDENT_SUPERVISOR,
        page_title="Add student supervisor",
        submit_label="Create student supervisor",
        success_message="Student supervisor created.",
        worker_type_label="student supervisor",
    )


@supervisor_required
def worker_profile_create_view(request):
    return _create_worker_profile_account(
        request,
        role=UserRole.STUDENT_WORKER,
        page_title="Add student",
        submit_label="Create student",
        success_message="Student worker created.",
        worker_type_label="student",
    )


@supervisor_required
def worker_profile_delete_view(request, pk):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")

    worker = get_object_or_404(User.objects.select_related("worker_profile"), pk=pk, role__in=UserRole.worker_roles())
    Task.objects.filter(assigned_to=worker).update(assigned_to=request.user, updated_at=timezone.now())
    Task.objects.filter(rotating_additional_assignee=worker).update(rotating_additional_assignee=None, updated_at=timezone.now())
    Task.additional_assignees.through.objects.filter(user_id=worker.pk).delete()
    Task.rotating_additional_assignees.through.objects.filter(user_id=worker.pk).delete()
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
        return redirect("supervisor-edit", pk=supervisor.pk)
    return render(
        request,
        "workboard/supervisor_form.html",
        {
            "form": form,
            "page_title": f"Edit supervisor: {supervisor.display_label}",
            "submit_label": "Save changes",
            "supervisor": supervisor,
            "remove_label": "Remove supervisor",
            "remove_confirm": "Remove this supervisor? Assigned tasks will be reassigned to you.",
        },
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
