from datetime import date, timedelta

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class UserRole(models.TextChoices):
    SUPERVISOR = "supervisor", "Supervisor"
    STUDENT_WORKER = "student_worker", "Student Worker"


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


class User(AbstractUser):
    role = models.CharField(max_length=32, choices=UserRole.choices, default=UserRole.STUDENT_WORKER)
    must_change_password = models.BooleanField(default=False)
    assignable_to_tasks = models.BooleanField(default=True)

    @property
    def is_supervisor(self):
        return self.role == UserRole.SUPERVISOR

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
    email = models.EmailField()
    active_status = models.BooleanField(default=True)
    normal_shift_availability = models.TextField(blank=True)
    max_hours_per_day = models.DecimalField(max_digits=4, decimal_places=2, default=4)
    skill_notes = models.TextField(blank=True)


    def __str__(self):
        return self.display_name


class StudentAvailability(models.Model):
    profile = models.ForeignKey(StudentWorkerProfile, on_delete=models.CASCADE, related_name="weekly_availability")
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    hours_available = models.DecimalField(max_digits=4, decimal_places=2, default=0)

    class Meta:
        ordering = ["weekday"]
        unique_together = ("profile", "weekday")

    def __str__(self):
        return f"{self.profile.display_name} - {self.get_weekday_display()}"


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


class RecurringTaskTemplate(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    priority = models.CharField(max_length=16, choices=Priority.choices, default=Priority.MEDIUM)
    estimated_minutes = models.PositiveIntegerField(null=True, blank=True)
    assign_to = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="recurring_templates",
        limit_choices_to={"role": UserRole.STUDENT_WORKER},
    )
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
        if self.display_order is None:
            max_order = RecurringTaskTemplate.objects.exclude(pk=self.pk).aggregate(max_order=models.Max("display_order")).get("max_order") or 0
            self.display_order = max_order + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    def advance_next_run_date(self):
        if self.recurrence_pattern == RecurrencePattern.DAILY:
            self.next_run_date = self.next_run_date + timedelta(days=self.recurrence_interval)
        elif self.recurrence_pattern == RecurrencePattern.WEEKLY:
            self.next_run_date = self.next_run_date + timedelta(weeks=self.recurrence_interval)
        else:
            month = self.next_run_date.month - 1 + self.recurrence_interval
            year = self.next_run_date.year + month // 12
            month = month % 12 + 1
            day = self.day_of_month or self.next_run_date.day
            last_day = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
            self.next_run_date = date(year, month, min(day, last_day))


class Task(models.Model):
    title = models.CharField(max_length=255)
    raw_message = models.TextField(blank=True)
    description = models.TextField(blank=True)
    priority = models.CharField(max_length=16, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=16, choices=TaskStatus.choices, default=TaskStatus.NEW)
    due_date = models.DateField(null=True, blank=True)
    raw_due_text = models.CharField(max_length=255, blank=True)
    waiting_person = models.CharField(max_length=255, blank=True)
    respond_to_text = models.CharField(max_length=255, blank=True)
    estimated_minutes = models.PositiveIntegerField(null=True, blank=True)
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
        limit_choices_to={"role": UserRole.STUDENT_WORKER},
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

    def __str__(self):
        return self.title

    def mark_complete(self):
        self.status = TaskStatus.DONE
        self.completed_at = timezone.now()

    @property
    def assignee_labels(self):
        labels = []
        if self.assigned_to:
            labels.append(self.assigned_to.display_label)
        for user in self.additional_assignees.all():
            if user != self.assigned_to:
                labels.append(user.display_label)
        return ", ".join(labels) if labels else "Unassigned"


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

