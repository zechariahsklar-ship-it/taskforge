from calendar import monthrange
from datetime import date, timedelta

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


def _format_clock_time(value):
    return value.strftime("%I:%M %p").lstrip("0")


def _format_time_window(start_time, end_time):
    return f"{_format_clock_time(start_time)} - {_format_clock_time(end_time)}"


def _format_calendar_date(value):
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def _rotation_count_label(count):
    return f"Rotation x{count or 1}"


def _rotation_user_label(user):
    return f"{user.display_label} (rotation)"


def _ordered_block_summary(block_manager):
    blocks = list(block_manager.order_by("position", "start_time", "end_time", "pk"))
    if not blocks:
        return "Not scheduled"
    return ", ".join(_format_time_window(block.start_time, block.end_time) for block in blocks)


class UserRole(models.TextChoices):
    SUPERVISOR = "supervisor", "Supervisor"
    STUDENT_SUPERVISOR = "student_supervisor", "Student Supervisor"
    STUDENT_WORKER = "student_worker", "Student Worker"

    @classmethod
    def worker_roles(cls):
        return [cls.STUDENT_WORKER, cls.STUDENT_SUPERVISOR]


class Priority(models.TextChoices):
    URGENT = "urgent", "Urgent"
    HIGH = "high", "High"
    MEDIUM = "medium", "Medium"
    LOW = "low", "Low"


class TaskStatus(models.TextChoices):
    NEW = "new", "New Requests"
    ASSIGNED = "assigned", "Assigned"
    IN_PROGRESS = "in_progress", "In Progress"
    WAITING = "waiting", "Waiting / Blocked"
    REVIEW = "review", "Review / Follow Up"
    DONE = "done", "Done"


class TaskAuditAction(models.TextChoices):
    CREATED = "created", "Created"
    UPDATED = "updated", "Updated"
    STATUS_CHANGED = "status_changed", "Status Changed"
    NOTE_ADDED = "note_added", "Note Added"
    ATTACHMENT_ADDED = "attachment_added", "Attachment Added"
    CHECKLIST_UPDATED = "checklist_updated", "Checklist Updated"
    DELETED = "deleted", "Deleted"
    RECURRING_RUN = "recurring_run", "Recurring Run"


class RecurrencePattern(models.TextChoices):
    DAILY = "daily", "Daily"
    WEEKLY = "weekly", "Weekly"
    MONTHLY = "monthly", "Monthly"


class Weekday(models.IntegerChoices):
    MONDAY = 0, "Monday"
    TUESDAY = 1, "Tuesday"
    WEDNESDAY = 2, "Wednesday"
    THURSDAY = 3, "Thursday"
    FRIDAY = 4, "Friday"
    SATURDAY = 5, "Saturday"
    SUNDAY = 6, "Sunday"


class ScheduleAdjustmentRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPLIED = "applied", "Applied"
    DECLINED = "declined", "Declined"


class Team(models.Model):
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name

    @classmethod
    def get_default_team(cls):
        return cls.objects.get_or_create(
            name="General",
            defaults={"description": "Default team for existing TaskForge data."},
        )[0]


class WorkerTag(models.Model):
    team = models.ForeignKey("Team", null=True, blank=True, on_delete=models.SET_NULL, related_name="worker_tags")
    name = models.CharField(max_length=80)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "pk"]
        unique_together = ("team", "name")

    def save(self, *args, **kwargs):
        if not self.team_id:
            self.team = _resolved_team_or_default()
        self.name = self.name.strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


def _resolved_team_or_default(*candidates):
    for candidate in candidates:
        if candidate is None:
            continue
        team = candidate if isinstance(candidate, Team) else getattr(candidate, "team", None)
        if team is not None:
            return team
    return Team.get_default_team()


class User(AbstractUser):
    role = models.CharField(max_length=32, choices=UserRole.choices, default=UserRole.STUDENT_WORKER)
    team = models.ForeignKey("Team", null=True, blank=True, on_delete=models.SET_NULL, related_name="members")
    must_change_password = models.BooleanField(default=False)
    assignable_to_tasks = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        if not self.team_id and not self.is_superuser:
            self.team = _resolved_team_or_default()
        super().save(*args, **kwargs)

    @property
    def is_admin(self):
        return self.is_superuser

    @property
    def is_supervisor(self):
        return self.is_admin or self.role == UserRole.SUPERVISOR

    @property
    def is_student_supervisor(self):
        return (not self.is_admin) and self.role == UserRole.STUDENT_SUPERVISOR

    @property
    def is_worker_role(self):
        return (not self.is_admin) and self.role in UserRole.worker_roles()

    @property
    def can_view_full_board(self):
        return self.is_admin or self.role in {UserRole.SUPERVISOR, UserRole.STUDENT_SUPERVISOR}

    @property
    def can_edit_tasks(self):
        return self.is_admin or self.role in {UserRole.SUPERVISOR, UserRole.STUDENT_SUPERVISOR}

    @property
    def display_label(self):
        try:
            return self.worker_profile.display_name
        except StudentWorkerProfile.DoesNotExist:
            full_name = self.get_full_name().strip()
            return full_name or self.username


class StudentWorkerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="worker_profile")
    display_name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    active_status = models.BooleanField(default=True)
    normal_shift_availability = models.TextField(blank=True)
    skill_notes = models.TextField(blank=True)
    tags = models.ManyToManyField("WorkerTag", blank=True, related_name="workers")

    def __str__(self):
        return self.display_name

    @property
    def tag_labels(self):
        labels = list(self.tags.order_by("name", "pk").values_list("name", flat=True))
        return ", ".join(labels) if labels else "None"


class StudentAvailability(models.Model):
    profile = models.ForeignKey(StudentWorkerProfile, on_delete=models.CASCADE, related_name="weekly_availability")
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    hours_available = models.DecimalField(max_digits=4, decimal_places=2, default=0)

    class Meta:
        ordering = ["weekday"]
        unique_together = ("profile", "weekday")

    def __str__(self):
        return f"{self.profile.display_name} - {self.get_weekday_display()}"

    @property
    def block_summary(self):
        return _ordered_block_summary(self.blocks)


class StudentAvailabilityBlock(models.Model):
    availability = models.ForeignKey(StudentAvailability, on_delete=models.CASCADE, related_name="blocks")
    start_time = models.TimeField()
    end_time = models.TimeField()
    position = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["availability__weekday", "position", "start_time", "end_time", "pk"]

    def __str__(self):
        return f"{self.availability.profile.display_name} - {self.display_label}"

    @property
    def display_label(self):
        return _format_time_window(self.start_time, self.end_time)


class StudentAvailabilityOverride(models.Model):
    profile = models.ForeignKey(StudentWorkerProfile, on_delete=models.CASCADE, related_name="availability_overrides")
    override_date = models.DateField()
    hours_available = models.DecimalField(max_digits=4, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_availability_overrides")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["override_date", "profile__display_name"]
        unique_together = ("profile", "override_date")

    def __str__(self):
        return f"{self.profile.display_name} override for {self.override_date}"

    @property
    def hours_delta_display(self):
        if self.hours_available > 0:
            return f"+{self.hours_available}"
        return str(self.hours_available)


class StudentScheduleOverride(models.Model):
    profile = models.ForeignKey(StudentWorkerProfile, on_delete=models.CASCADE, related_name="schedule_overrides")
    override_date = models.DateField()
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_schedule_overrides")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["override_date", "profile__display_name"]
        unique_together = ("profile", "override_date")

    def __str__(self):
        return f"{self.profile.display_name} schedule override for {self.override_date}"

    @property
    def block_summary(self):
        return _ordered_block_summary(self.blocks)


class StudentScheduleOverrideBlock(models.Model):
    schedule_override = models.ForeignKey(StudentScheduleOverride, on_delete=models.CASCADE, related_name="blocks")
    start_time = models.TimeField()
    end_time = models.TimeField()
    position = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["schedule_override__override_date", "position", "start_time", "end_time", "pk"]

    def __str__(self):
        return f"{self.schedule_override.profile.display_name} - {self.display_label}"

    @property
    def display_label(self):
        return _format_time_window(self.start_time, self.end_time)


class ScheduleAdjustmentRequest(models.Model):
    profile = models.ForeignKey(StudentWorkerProfile, on_delete=models.CASCADE, related_name="schedule_adjustment_requests")
    team = models.ForeignKey("Team", null=True, blank=True, on_delete=models.SET_NULL, related_name="schedule_adjustment_requests")
    requested_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="schedule_adjustment_requests")
    requested_date = models.DateField()
    note = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=16,
        choices=ScheduleAdjustmentRequestStatus.choices,
        default=ScheduleAdjustmentRequestStatus.PENDING,
    )
    reviewed_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_schedule_adjustment_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    applied_override = models.ForeignKey(
        StudentScheduleOverride,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applied_schedule_adjustment_requests",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "requested_date", "-created_at", "-pk"]

    def save(self, *args, **kwargs):
        if not self.team_id:
            self.team = _resolved_team_or_default(self.profile.user, self.requested_by)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.profile.display_name} request for {self.requested_date}"

    @property
    def block_summary(self):
        return _ordered_block_summary(self.blocks)


class ScheduleAdjustmentRequestBlock(models.Model):
    schedule_request = models.ForeignKey(ScheduleAdjustmentRequest, on_delete=models.CASCADE, related_name="blocks")
    start_time = models.TimeField()
    end_time = models.TimeField()
    position = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["schedule_request__requested_date", "position", "start_time", "end_time", "pk"]

    def __str__(self):
        return f"{self.schedule_request.profile.display_name} - {self.display_label}"

    @property
    def display_label(self):
        return _format_time_window(self.start_time, self.end_time)


class RecurringTaskTemplate(models.Model):
    team = models.ForeignKey("Team", null=True, blank=True, on_delete=models.SET_NULL, related_name="recurring_templates")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    priority = models.CharField(max_length=16, choices=Priority.choices, default=Priority.MEDIUM)
    estimated_minutes = models.PositiveIntegerField(null=True, blank=True)
    scheduled_start_time = models.TimeField(null=True, blank=True)
    scheduled_end_time = models.TimeField(null=True, blank=True)
    required_worker_tags = models.ManyToManyField("WorkerTag", blank=True, related_name="recurring_templates")
    assign_to = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="recurring_templates",
        limit_choices_to={"role__in": UserRole.worker_roles()},
    )
    additional_assignees = models.ManyToManyField(
        User,
        blank=True,
        related_name="additional_recurring_templates",
        limit_choices_to={"role__in": UserRole.worker_roles()},
    )
    rotating_additional_assignee_count = models.PositiveSmallIntegerField(default=0)
    rotate_additional_assignee = models.BooleanField(default=False)
    requested_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="requested_recurring_templates",
    )
    recurrence_pattern = models.CharField(max_length=16, choices=RecurrencePattern.choices)
    recurrence_interval = models.PositiveIntegerField(default=1)
    day_of_week = models.PositiveSmallIntegerField(null=True, blank=True)
    day_of_month = models.PositiveSmallIntegerField(null=True, blank=True)
    start_date = models.DateField(default=timezone.localdate)
    next_run_date = models.DateField(default=timezone.localdate)
    active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "next_run_date", "title", "pk"]

    def save(self, *args, **kwargs):
        if not self.team_id:
            self.team = _resolved_team_or_default(
                self.requested_by if self.requested_by_id else None,
                self.assign_to if self.assign_to_id else None,
            )
        if self.display_order is None:
            max_order = (
                RecurringTaskTemplate.objects.exclude(pk=self.pk)
                .filter(team=self.team)
                .aggregate(max_order=models.Max("display_order"))
                .get("max_order")
                or 0
            )
            self.display_order = max_order + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    @property
    def scheduled_window_display(self):
        if not self.scheduled_start_time or not self.scheduled_end_time:
            return ""
        return _format_time_window(self.scheduled_start_time, self.scheduled_end_time)

    @property
    def additional_assignee_labels(self):
        labels = [user.display_label for user in self.additional_assignees.all()]
        if self.rotating_additional_assignee_count:
            labels.append(_rotation_count_label(self.rotating_additional_assignee_count))
        elif self.rotate_additional_assignee:
            labels.append(_rotation_count_label(1))
        return ", ".join(labels) if labels else "None"

    @property
    def required_worker_tag_labels(self):
        labels = list(self.required_worker_tags.order_by("name", "pk").values_list("name", flat=True))
        return ", ".join(labels) if labels else "None"

    def advance_next_run_date(self):
        if self.recurrence_pattern == RecurrencePattern.DAILY:
            self.next_run_date = self.next_run_date + timedelta(days=self.recurrence_interval)
        elif self.recurrence_pattern == RecurrencePattern.WEEKLY:
            self.next_run_date = self.next_run_date + timedelta(weeks=self.recurrence_interval)
        else:
            month_index = self.next_run_date.month - 1 + self.recurrence_interval
            year = self.next_run_date.year + month_index // 12
            month = month_index % 12 + 1
            day = self.day_of_month or self.next_run_date.day
            self.next_run_date = date(year, month, min(day, monthrange(year, month)[1]))


class Task(models.Model):
    team = models.ForeignKey("Team", null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks")
    title = models.CharField(max_length=255)
    raw_message = models.TextField(blank=True)
    description = models.TextField(blank=True)
    priority = models.CharField(max_length=16, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=16, choices=TaskStatus.choices, default=TaskStatus.NEW)
    due_date = models.DateField(null=True, blank=True)
    scheduled_date = models.DateField(null=True, blank=True)
    scheduled_start_time = models.TimeField(null=True, blank=True)
    scheduled_end_time = models.TimeField(null=True, blank=True)
    raw_due_text = models.CharField(max_length=255, blank=True)
    waiting_person = models.CharField(max_length=255, blank=True)
    respond_to_text = models.CharField(max_length=255, blank=True)
    estimated_minutes = models.PositiveIntegerField(null=True, blank=True)
    required_worker_tags = models.ManyToManyField("WorkerTag", blank=True, related_name="tasks")
    assigned_to = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_tasks",
    )
    additional_assignees = models.ManyToManyField(
        User,
        blank=True,
        related_name="collaborative_tasks",
        limit_choices_to={"role__in": UserRole.worker_roles()},
    )
    rotating_additional_assignee_count = models.PositiveSmallIntegerField(default=0)
    rotating_additional_assignees = models.ManyToManyField(
        User,
        blank=True,
        related_name="rotating_collaborative_task_memberships",
        limit_choices_to={"role__in": UserRole.worker_roles()},
    )
    # Keep the legacy single-rotation fields so older rows can migrate forward safely.
    rotate_additional_assignee = models.BooleanField(default=False)
    rotating_additional_assignee = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rotating_collaborative_tasks",
        limit_choices_to={"role__in": UserRole.worker_roles()},
    )
    requested_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="requested_tasks",
    )
    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_tasks",
    )
    recurring_task = models.BooleanField(default=False)
    recurring_template = models.ForeignKey(
        RecurringTaskTemplate,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generated_tasks",
    )
    recurrence_pattern = models.CharField(max_length=16, choices=RecurrencePattern.choices, blank=True)
    recurrence_interval = models.PositiveIntegerField(null=True, blank=True)
    recurrence_day_of_week = models.PositiveSmallIntegerField(null=True, blank=True)
    recurrence_day_of_month = models.PositiveSmallIntegerField(null=True, blank=True)
    board_order = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["board_order", "due_date", "-created_at"]

    def save(self, *args, **kwargs):
        if not self.team_id:
            self.team = _resolved_team_or_default(
                self.recurring_template.team if self.recurring_template_id else None,
                self.created_by if self.created_by_id else None,
                self.requested_by if self.requested_by_id else None,
                self.assigned_to if self.assigned_to_id else None,
            )
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    @property
    def scheduled_window_display(self):
        # Prefer persisted multi-block schedule rows, then fall back to the
        # legacy single-window fields when older tasks do not have blocks yet.
        scheduled_blocks = list(self.scheduled_blocks.order_by("work_date", "position", "start_time", "end_time", "pk"))
        if scheduled_blocks:
            grouped_labels = {}
            for block in scheduled_blocks:
                grouped_labels.setdefault(block.work_date, []).append(
                    _format_time_window(block.start_time, block.end_time)
                )
            return "; ".join(
                f"{_format_calendar_date(work_date)}: {', '.join(labels)}"
                for work_date, labels in grouped_labels.items()
            )
        if not self.scheduled_date or not self.scheduled_start_time or not self.scheduled_end_time:
            return ""
        return f"{self.scheduled_date.isoformat()} | {_format_time_window(self.scheduled_start_time, self.scheduled_end_time)}"

    @property
    def scheduled_window_latest_date(self):
        latest_block = self.scheduled_blocks.order_by("-work_date", "-position", "-end_time", "-pk").first()
        if latest_block:
            return latest_block.work_date
        return self.scheduled_date

    def mark_complete(self):
        self.status = TaskStatus.DONE
        self.completed_at = timezone.now()

    @property
    def assignee_labels(self):
        labels = []
        seen_ids = set()
        if self.assigned_to:
            labels.append(self.assigned_to.display_label)
            seen_ids.add(self.assigned_to_id)
        for user in self.additional_assignees.all():
            if user.pk not in seen_ids:
                labels.append(user.display_label)
                seen_ids.add(user.pk)
        for user in self.rotating_additional_assignees.all():
            if user.pk not in seen_ids:
                labels.append(_rotation_user_label(user))
                seen_ids.add(user.pk)
        if self.rotating_additional_assignee and self.rotating_additional_assignee_id not in seen_ids:
            labels.append(_rotation_user_label(self.rotating_additional_assignee))
            seen_ids.add(self.rotating_additional_assignee_id)
        if not any(label.endswith("(rotation)") for label in labels) and self.rotating_additional_assignee_count:
            labels.append(_rotation_count_label(self.rotating_additional_assignee_count))
        return ", ".join(labels) if labels else "Unassigned"

    @property
    def additional_assignee_labels(self):
        labels = [user.display_label for user in self.additional_assignees.all()]
        rotating_labels = [_rotation_user_label(user) for user in self.rotating_additional_assignees.all()]
        if rotating_labels:
            labels.extend(rotating_labels)
        elif self.rotating_additional_assignee:
            labels.append(_rotation_user_label(self.rotating_additional_assignee))
        elif self.rotating_additional_assignee_count:
            labels.append(_rotation_count_label(self.rotating_additional_assignee_count))
        elif self.rotate_additional_assignee:
            labels.append(_rotation_count_label(1))
        return ", ".join(labels) if labels else "None"

    @property
    def required_worker_tag_labels(self):
        labels = list(self.required_worker_tags.order_by("name", "pk").values_list("name", flat=True))
        return ", ".join(labels) if labels else "None"


class TaskScheduleBlock(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="scheduled_blocks")
    work_date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    position = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["work_date", "position", "start_time", "end_time", "pk"]

    def __str__(self):
        return f"{self.task.title} - {_format_calendar_date(self.work_date)} {self.display_label}"

    @property
    def display_label(self):
        return _format_time_window(self.start_time, self.end_time)


class TaskNote(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Note for {self.task_id}"


class TaskChecklistItem(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="checklist_items")
    title = models.CharField(max_length=255)
    is_completed = models.BooleanField(default=False)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.title


class TaskEstimateFeedback(models.Model):
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="estimate_feedback")
    raw_message = models.TextField(blank=True)
    task_title = models.CharField(max_length=255, blank=True)
    original_estimated_minutes = models.PositiveIntegerField(null=True, blank=True)
    corrected_estimated_minutes = models.PositiveIntegerField()
    corrected_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="estimate_feedback_entries")
    source = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Estimate feedback for {self.task_title or self.task_id}"


class TaskAttachment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="task_attachments/%Y/%m/%d")
    original_name = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at", "id"]

    def __str__(self):
        return self.original_name


class TaskAuditEvent(models.Model):
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_events")
    task_title = models.CharField(max_length=255)
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="task_audit_events")
    action = models.CharField(max_length=32, choices=TaskAuditAction.choices)
    summary = models.CharField(max_length=255)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.task_title} | {self.get_action_display()}"

    @property
    def actor_label(self):
        if self.actor:
            return self.actor.display_label
        return "System"


class TaskIntakeDraft(models.Model):
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="intake_drafts")
    raw_message = models.TextField()
    parsed_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Draft {self.pk}"


class TaskIntakeDraftAttachment(models.Model):
    draft = models.ForeignKey(TaskIntakeDraft, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="intake_attachments/%Y/%m/%d")
    original_name = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at", "id"]

    def __str__(self):
        return self.original_name

