from datetime import date

from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.crypto import get_random_string

from .forms import (
    DAY_FIELD_CONFIG,
    ScheduleAdjustmentRequestForm,
    StudentScheduleOverrideForm,
    StudentWorkerProfileForm,
    SupervisorForm,
    WorkerTagForm,
    SupervisorStudentPasswordResetForm,
    TeamForm,
    WeeklyAvailabilityForm,
)
from .models import (
    RecurringTaskTemplate,
    ScheduleAdjustmentRequest,
    ScheduleAdjustmentRequestBlock,
    ScheduleAdjustmentRequestStatus,
    StudentScheduleOverride,
    StudentScheduleOverrideBlock,
    StudentWorkerProfile,
    Task,
    TaskStatus,
    Team,
    User,
    WorkerTag,
    UserRole,
    _format_time_window,
)
from .task_views import (
    _save_schedule_override,
    _save_weekly_schedule,
    _scope_queryset_to_user_team,
    _serialize_blocks_for_initial,
    _weekly_schedule_initial,
    admin_required,
    app_login_required,
    supervisor_required,
)


def _scoped_worker_profiles(user):
    queryset = StudentWorkerProfile.objects.select_related("user").prefetch_related("tags")
    return _scope_queryset_to_user_team(queryset, user, field_name="user__team")


def _scoped_users(user, queryset=None):
    return _scope_queryset_to_user_team(queryset or User.objects.all(), user)


def _scoped_worker_tags(user, queryset=None):
    return _scope_queryset_to_user_team(queryset or WorkerTag.objects.all(), user)


def _scoped_supervisors(user):
    return _scoped_users(user, User.objects.filter(role=UserRole.SUPERVISOR))


def _scoped_schedule_requests(user, queryset=None):
    if queryset is None:
        queryset = ScheduleAdjustmentRequest.objects.all()
    return _scope_queryset_to_user_team(queryset, user)


def _auto_decline_expired_schedule_requests(queryset=None, *, now=None):
    if queryset is None:
        queryset = ScheduleAdjustmentRequest.objects.all()
    review_time = now or timezone.now()
    expired_request_ids = list(
        queryset.filter(
            status=ScheduleAdjustmentRequestStatus.PENDING,
            requested_date__lt=timezone.localdate(review_time),
        ).values_list("pk", flat=True)
    )
    if not expired_request_ids:
        return 0
    return ScheduleAdjustmentRequest.objects.filter(pk__in=expired_request_ids).update(
        status=ScheduleAdjustmentRequestStatus.DECLINED,
        reviewed_by=None,
        reviewed_at=review_time,
    )


def _reassignment_user_for_removed_account(request_user, removed_user):
    if not request_user.is_admin and request_user.is_supervisor and request_user.team_id == removed_user.team_id:
        return request_user
    fallback = (
        User.objects.filter(
            role=UserRole.SUPERVISOR,
            team=removed_user.team,
            assignable_to_tasks=True,
        )
        .exclude(pk=removed_user.pk)
        .order_by("first_name", "last_name", "username", "pk")
        .first()
    )
    if fallback:
        return fallback
    if request_user.is_admin:
        return request_user
    return None


@supervisor_required
def worker_list_view(request):
    worker_profiles = list(
        _scoped_worker_profiles(request.user)
        .filter(user__role__in=UserRole.worker_roles())
        .order_by("display_name")
    )
    worker_workload = {
        worker.pk: worker
        for worker in _scoped_users(request.user, User.objects.filter(role__in=UserRole.worker_roles()))
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
        _scoped_supervisors(request.user)
        .annotate(
            active_tasks=Count("assigned_tasks", filter=~Q(assigned_tasks__status=TaskStatus.DONE)),
            estimated_minutes=Sum("assigned_tasks__estimated_minutes", filter=~Q(assigned_tasks__status=TaskStatus.DONE)),
        )
        .order_by("username")
    )
    worker_tags = (
        _scoped_worker_tags(
            request.user,
            WorkerTag.objects.select_related("team").annotate(
                worker_count=Count("workers", distinct=True),
                task_count=Count("tasks", distinct=True),
                recurring_count=Count("recurring_templates", distinct=True),
            ),
        )
        .order_by("name", "pk")
    )
    teams = []
    if request.user.is_admin:
        teams = Team.objects.annotate(
            member_count=Count("members", distinct=True),
            task_count=Count("tasks", distinct=True),
            recurring_count=Count("recurring_templates", distinct=True),
        ).order_by("name", "pk")
    return render(
        request,
        "workboard/worker_list.html",
        {
            "students": students,
            "student_supervisors": student_supervisors,
            "supervisors": supervisors,
            "teams": teams,
            "worker_tags": worker_tags,
            "show_team_column": request.user.is_admin,
            "show_tag_team_column": request.user.is_admin,
        },
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


def _username_error_message(username: str, *, exclude_user: User | None = None) -> str:
    if not username:
        return "Username is required."
    queryset = User.objects.all()
    if exclude_user is not None:
        queryset = queryset.exclude(pk=exclude_user.pk)
    if queryset.filter(username=username).exists():
        return "That username is already in use."
    return ""


def _reassignment_destination_label(user: User | None) -> str:
    return user.display_label if user else "the team board"


def _reassign_owned_work_for_removed_user(removed_user: User, *, reassignment_user: User | None) -> None:
    Task.objects.filter(assigned_to=removed_user).update(assigned_to=reassignment_user, updated_at=timezone.now())
    RecurringTaskTemplate.objects.filter(assign_to=removed_user).update(assign_to=reassignment_user, updated_at=timezone.now())


def _remove_worker_from_collaboration_slots(removed_user: User) -> None:
    Task.objects.filter(rotating_additional_assignee=removed_user).update(rotating_additional_assignee=None, updated_at=timezone.now())
    Task.additional_assignees.through.objects.filter(user_id=removed_user.pk).delete()
    Task.rotating_additional_assignees.through.objects.filter(user_id=removed_user.pk).delete()
    RecurringTaskTemplate.additional_assignees.through.objects.filter(user_id=removed_user.pk).delete()


def _save_worker_profile_details(profile: StudentWorkerProfile, worker_form: StudentWorkerProfileForm, post_data, *, actor: User) -> bool:
    user = profile.user
    username = post_data.get("username", "").strip()
    username_error = _username_error_message(username, exclude_user=user)
    if username_error:
        worker_form.add_error(None, username_error)
        return False

    user.username = username
    user.first_name = post_data.get("first_name", "").strip()
    user.last_name = post_data.get("last_name", "").strip()
    user.email = worker_form.cleaned_data["email"]
    user.team = worker_form.cleaned_data["team"]
    user.save()

    updated_profile = worker_form.save(commit=False)
    updated_profile.user = user
    updated_profile.email = worker_form.cleaned_data["email"]
    updated_profile.display_name = user.get_full_name().strip() or user.username
    updated_profile.save()
    worker_form.save_m2m()
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
    form = StudentWorkerProfileForm(request.POST or None, actor=request.user)
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
        username_error = _username_error_message(account["username"])
        if username_error:
            messages.error(request, username_error)
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
                team=form.cleaned_data["team"],
                must_change_password=True,
            )
            profile = form.save(commit=False)
            profile.user = user
            profile.email = profile.email or account["email"]
            profile.display_name = user.get_full_name().strip() or user.username
            profile.save()
            form.save_m2m()
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


def _format_schedule_override_date(value: date) -> str:
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def _block_minutes(start_time, end_time) -> int:
    return ((end_time.hour * 60) + end_time.minute) - ((start_time.hour * 60) + start_time.minute)


def _hours_summary_label(total_minutes: int) -> str:
    hours = total_minutes / 60
    label = str(int(hours)) if hours.is_integer() else f"{hours:.1f}".rstrip("0").rstrip(".")
    suffix = "hr" if label == "1" else "hrs"
    return f"{label} {suffix}"


def _schedule_blocks_summary(blocks, *, empty_label: str) -> str:
    if not blocks:
        return empty_label
    total_minutes = sum(_block_minutes(block.start_time, block.end_time) for block in blocks)
    block_labels = ", ".join(block.display_label for block in blocks)
    return f"{block_labels} ({_hours_summary_label(total_minutes)})"


def _schedule_override_summary(blocks) -> str:
    return _schedule_blocks_summary(blocks, empty_label="Off (0 hrs)")


def _weekly_schedule_summary(availability) -> str:
    if availability is None:
        return "Not scheduled"
    blocks = list(availability.blocks.order_by("position", "start_time", "end_time", "pk"))
    if blocks:
        return _schedule_blocks_summary(blocks, empty_label="Not scheduled")
    if availability.start_time and availability.end_time:
        total_minutes = _block_minutes(availability.start_time, availability.end_time)
        return f"{_format_time_window(availability.start_time, availability.end_time)} ({_hours_summary_label(total_minutes)})"
    if availability.hours_available:
        return _hours_summary_label(int(float(availability.hours_available) * 60))
    return "Not scheduled"


def _weekly_schedule_rows(profile: StudentWorkerProfile, schedule_overrides) -> list[dict[str, object]]:
    weekly_map = {
        item.weekday: item
        for item in profile.weekly_availability.prefetch_related("blocks").all()
    }
    override_summary_map = _weekly_override_summary_map(schedule_overrides)
    rows = []
    for prefix, label, weekday in DAY_FIELD_CONFIG:
        rows.append(
            {
                "label": label,
                "summary_label": _weekly_schedule_summary(weekly_map.get(weekday)),
                "override_entries": override_summary_map.get(prefix, []),
            }
        )
    return rows


def _weekly_override_summary_map(schedule_overrides) -> dict[str, list[dict[str, str]]]:
    weekday_prefix_map = {weekday: prefix for prefix, _label, weekday in DAY_FIELD_CONFIG}
    summary_map: dict[str, list[dict[str, str]]] = {}
    for schedule_override in schedule_overrides:
        prefix = weekday_prefix_map.get(schedule_override.override_date.weekday())
        if prefix is None:
            continue
        blocks = sorted(
            schedule_override.blocks.all(),
            key=lambda block: (block.position, block.start_time, block.end_time, block.pk),
        )
        summary_map.setdefault(prefix, []).append(
            {
                "date_label": _format_schedule_override_date(schedule_override.override_date),
                "summary_label": _schedule_override_summary(blocks),
            }
        )
    return summary_map

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


def _current_worker_profile_for_request(request):
    if request.user.role not in UserRole.worker_roles():
        return None
    return StudentWorkerProfile.objects.select_related("user").filter(user=request.user).first()


def _save_schedule_adjustment_request(adjustment_request: ScheduleAdjustmentRequest, request_form: ScheduleAdjustmentRequestForm) -> ScheduleAdjustmentRequest:
    adjustment_request.blocks.all().delete()
    for position, block in enumerate(request_form.cleaned_data["schedule_blocks"], start=1):
        ScheduleAdjustmentRequestBlock.objects.create(
            schedule_request=adjustment_request,
            start_time=block["start_time"],
            end_time=block["end_time"],
            position=position,
        )
    return adjustment_request


def _apply_schedule_adjustment_request(adjustment_request: ScheduleAdjustmentRequest, *, acted_by: User) -> StudentScheduleOverride:
    note_prefix = f"Applied from request by {adjustment_request.requested_by.display_label}"
    override_note = f"{note_prefix}: {adjustment_request.note}" if adjustment_request.note else note_prefix
    schedule_override, _ = StudentScheduleOverride.objects.update_or_create(
        profile=adjustment_request.profile,
        override_date=adjustment_request.requested_date,
        defaults={
            "note": override_note,
            "created_by": acted_by,
        },
    )
    schedule_override.blocks.all().delete()
    for position, block in enumerate(adjustment_request.blocks.order_by("position", "start_time", "end_time", "pk"), start=1):
        StudentScheduleOverrideBlock.objects.create(
            schedule_override=schedule_override,
            start_time=block.start_time,
            end_time=block.end_time,
            position=position,
        )
    adjustment_request.status = ScheduleAdjustmentRequestStatus.APPLIED
    adjustment_request.reviewed_by = acted_by
    adjustment_request.reviewed_at = timezone.now()
    adjustment_request.applied_override = schedule_override
    adjustment_request.save(update_fields=["status", "reviewed_by", "reviewed_at", "applied_override", "updated_at"])
    return schedule_override


@app_login_required
def schedule_adjustment_request_view(request):
    profile = _current_worker_profile_for_request(request)
    if profile is None:
        return HttpResponseForbidden("Student worker access required.")

    _auto_decline_expired_schedule_requests(profile.schedule_adjustment_requests.all())
    request_form = ScheduleAdjustmentRequestForm(request.POST or None)
    if request.method == "POST" and request_form.is_valid():
        adjustment_request = request_form.save(commit=False)
        adjustment_request.profile = profile
        adjustment_request.requested_by = request.user
        adjustment_request.save()
        _save_schedule_adjustment_request(adjustment_request, request_form)
        messages.success(request, f"Schedule adjustment request submitted for {adjustment_request.requested_date}.")
        return redirect("schedule-adjustment-request")

    submitted_requests = profile.schedule_adjustment_requests.select_related("reviewed_by", "applied_override").prefetch_related("blocks").all()
    return render(
        request,
        "workboard/schedule_adjustment_request.html",
        {
            "profile": profile,
            "request_form": request_form,
            "weekly_form": request_form,
            "submitted_requests": submitted_requests,
        },
    )


@supervisor_required
def schedule_adjustment_request_list_view(request):
    _auto_decline_expired_schedule_requests(_scoped_schedule_requests(request.user))
    if request.method == "POST":
        adjustment_request = get_object_or_404(
            _scoped_schedule_requests(
                request.user,
                ScheduleAdjustmentRequest.objects.select_related("profile", "requested_by", "applied_override").prefetch_related("blocks"),
            ),
            pk=request.POST.get("schedule_request_id"),
        )
        if adjustment_request.status != ScheduleAdjustmentRequestStatus.PENDING:
            messages.error(request, "That schedule request has already been handled.")
            return redirect("schedule-adjustment-requests")

        action = request.POST.get("action")
        if action == "apply_request":
            schedule_override = _apply_schedule_adjustment_request(adjustment_request, acted_by=request.user)
            messages.success(request, f"Applied the requested schedule for {adjustment_request.profile.display_name} on {schedule_override.override_date}.")
            return redirect("schedule-adjustment-requests")
        if action == "decline_request":
            adjustment_request.status = ScheduleAdjustmentRequestStatus.DECLINED
            adjustment_request.reviewed_by = request.user
            adjustment_request.reviewed_at = timezone.now()
            adjustment_request.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
            messages.success(request, f"Declined the request for {adjustment_request.profile.display_name} on {adjustment_request.requested_date}.")
            return redirect("schedule-adjustment-requests")
        return HttpResponseBadRequest("Unknown action.")

    pending_requests = list(
        _scoped_schedule_requests(
            request.user,
            ScheduleAdjustmentRequest.objects.select_related("profile", "requested_by", "reviewed_by", "applied_override")
            .prefetch_related("blocks")
            .filter(status=ScheduleAdjustmentRequestStatus.PENDING)
        )
        .order_by("requested_date", "created_at", "pk")
    )
    recent_requests = list(
        _scoped_schedule_requests(
            request.user,
            ScheduleAdjustmentRequest.objects.select_related("profile", "requested_by", "reviewed_by", "applied_override")
            .prefetch_related("blocks")
            .filter(status=ScheduleAdjustmentRequestStatus.APPLIED)
        )
        .order_by("-reviewed_at", "-updated_at", "-pk")[:20]
    )
    return render(
        request,
        "workboard/schedule_adjustment_request_list.html",
        {
            "pending_requests": pending_requests,
            "recent_requests": recent_requests,
        },
    )


@supervisor_required
def worker_edit_view(request, pk):
    profile = get_object_or_404(_scoped_worker_profiles(request.user), pk=pk)
    context_labels = _worker_role_page_context(profile)
    worker_form = StudentWorkerProfileForm(request.POST or None, instance=profile, actor=request.user)

    if request.method == "POST" and worker_form.is_valid() and _save_worker_profile_details(profile, worker_form, request.POST, actor=request.user):
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


@app_login_required
def self_schedule_view(request):
    profile = _current_worker_profile_for_request(request)
    if profile is None:
        return HttpResponseForbidden("Student worker access required.")

    schedule_overrides = list(profile.schedule_overrides.prefetch_related("blocks").all())
    weekly_form = WeeklyAvailabilityForm(initial=_weekly_schedule_initial(profile))
    weekly_form.override_summary_map = _weekly_override_summary_map(schedule_overrides)
    return render(
        request,
        "workboard/self_schedule.html",
        {
            "profile": profile,
            "weekly_form": weekly_form,
            "schedule_overrides": schedule_overrides,
        },
    )


@supervisor_required
def worker_schedule_view(request, pk):
    profile = get_object_or_404(_scoped_worker_profiles(request.user), pk=pk)
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

    schedule_overrides = list(profile.schedule_overrides.prefetch_related("blocks").all())
    weekly_form.override_summary_map = _weekly_override_summary_map(schedule_overrides)

    return render(
        request,
        "workboard/worker_availability.html",
        {
            "profile": profile,
            "weekly_form": weekly_form,
            "schedule_override_form": schedule_override_form,
            "schedule_overrides": schedule_overrides,
            "selected_override_date": selected_override_date,
            **context_labels,
        },
    )


@supervisor_required
def supervisor_create_view(request):
    form = SupervisorForm(request.POST or None, actor=request.user)
    context = {"form": form, "page_title": "Add supervisor", "submit_label": "Create supervisor"}
    if request.method == "POST":
        account = _posted_account_details(request.POST)
        username_error = _username_error_message(account["username"])
        if username_error:
            messages.error(request, username_error)
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
        form = SupervisorForm(request.POST, instance=user, actor=request.user)
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

    worker = get_object_or_404(
        _scoped_users(request.user, User.objects.select_related("worker_profile").filter(role__in=UserRole.worker_roles())),
        pk=pk,
    )
    reassignment_user = _reassignment_user_for_removed_account(request.user, worker)
    _reassign_owned_work_for_removed_user(worker, reassignment_user=reassignment_user)
    _remove_worker_from_collaboration_slots(worker)
    worker_name = worker.display_label
    worker.delete()
    messages.success(
        request,
        f"Removed {worker_name}. Any assigned tasks were reassigned to {_reassignment_destination_label(reassignment_user)}.",
    )
    return redirect("worker-list")


@supervisor_required
def supervisor_delete_view(request, pk):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")

    supervisor = get_object_or_404(_scoped_supervisors(request.user), pk=pk)
    if supervisor.pk == request.user.pk:
        messages.error(request, "You cannot remove your own supervisor account while logged in.")
        return redirect("worker-list")

    reassignment_user = _reassignment_user_for_removed_account(request.user, supervisor)
    _reassign_owned_work_for_removed_user(supervisor, reassignment_user=reassignment_user)
    supervisor_name = supervisor.display_label
    supervisor.delete()
    messages.success(
        request,
        f"Removed {supervisor_name}. Any assigned tasks were reassigned to {_reassignment_destination_label(reassignment_user)}.",
    )
    return redirect("worker-list")


@supervisor_required
def supervisor_edit_view(request, pk):
    supervisor = get_object_or_404(_scoped_supervisors(request.user), pk=pk)
    form = SupervisorForm(request.POST or None, instance=supervisor, actor=request.user)
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
    person = get_object_or_404(_scoped_users(request.user, User.objects.filter(role__in=[UserRole.STUDENT_WORKER, UserRole.STUDENT_SUPERVISOR, UserRole.SUPERVISOR])), pk=pk)
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


@supervisor_required
def worker_tag_create_view(request):
    form = WorkerTagForm(request.POST or None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        worker_tag = form.save()
        messages.success(request, f'Worker tag "{worker_tag.name}" created.')
        return redirect("worker-list")
    return render(
        request,
        "workboard/worker_tag_form.html",
        {"form": form, "page_title": "Add worker tag", "submit_label": "Create tag"},
    )


@supervisor_required
def worker_tag_edit_view(request, pk):
    worker_tag = get_object_or_404(_scoped_worker_tags(request.user), pk=pk)
    form = WorkerTagForm(request.POST or None, instance=worker_tag, actor=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Worker tag updated.")
        return redirect("worker-list")
    return render(
        request,
        "workboard/worker_tag_form.html",
        {
            "form": form,
            "page_title": f"Edit worker tag: {worker_tag.name}",
            "submit_label": "Save changes",
            "worker_tag": worker_tag,
        },
    )


@supervisor_required
def worker_tag_delete_view(request, pk):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")

    worker_tag = get_object_or_404(_scoped_worker_tags(request.user), pk=pk)
    worker_tag_name = worker_tag.name
    worker_tag.delete()
    messages.success(request, f"Removed the {worker_tag_name} worker tag.")
    return redirect("worker-list")


@admin_required
def team_create_view(request):
    form = TeamForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Team created.")
        return redirect("worker-list")
    return render(request, "workboard/team_form.html", {"form": form, "page_title": "Add team", "submit_label": "Create team"})


@admin_required
def team_edit_view(request, pk):
    team = get_object_or_404(Team, pk=pk)
    form = TeamForm(request.POST or None, instance=team)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Team updated.")
        return redirect("worker-list")
    return render(
        request,
        "workboard/team_form.html",
        {
            "form": form,
            "page_title": f"Edit Team: {team.name}",
            "submit_label": "Save changes",
            "team": team,
        },
    )


@admin_required
def team_delete_view(request, pk):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")

    team = get_object_or_404(Team, pk=pk)
    if team.members.exists() or team.tasks.exists() or team.recurring_templates.exists() or team.schedule_adjustment_requests.exists():
        messages.error(request, "This team still has people or work attached to it. Reassign them before deleting the team.")
        return redirect("worker-list")

    team_name = team.name
    team.delete()
    messages.success(request, f"Removed the {team_name} team.")
    return redirect("worker-list")
