from decimal import Decimal

from django import forms
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.db.models import Q

from .models import (
    RecurringTaskTemplate,
    StudentAvailability,
    StudentAvailabilityOverride,
    StudentWorkerProfile,
    Task,
    TaskAttachment,
    TaskChecklistItem,
    TaskIntakeDraft,
    TaskNote,
    User,
    UserRole,
    TaskStatus,
)


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if not data:
            return []
        if isinstance(data, (list, tuple)):
            return [single_file_clean(item, initial) for item in data]
        return [single_file_clean(data, initial)]


class StyledFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
                widget.attrs["class"] = f"{widget.attrs.get('class', '')} form-check-input".strip()
            else:
                widget.attrs["class"] = f"{widget.attrs.get('class', '')} form-control".strip()


def _user_choice_label(user: User) -> str:
    full_name = user.get_full_name().strip()
    return full_name or user.display_label


class StudentWorkerProfileForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = StudentWorkerProfile
        fields = [
            "email",
            "active_status",
            "skill_notes",
        ]
        widgets = {
            "skill_notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["skill_notes"].label = "Notes"
        self.fields["skill_notes"].help_text = "Optional notes about strengths, training, or preferences."

class StudentAvailabilityForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = StudentAvailability
        fields = ["weekday", "hours_available"]


class StudentAvailabilityOverrideForm(StyledFormMixin, forms.ModelForm):
    override_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))

    class Meta:
        model = StudentAvailabilityOverride
        fields = ["override_date", "hours_available", "note"]

    def __init__(self, *args, profile=None, **kwargs):
        self.profile = profile
        super().__init__(*args, **kwargs)
        self.fields["hours_available"].label = "Hour adjustment"
        self.fields["hours_available"].help_text = "Use a positive number to add hours or a negative number to subtract hours for that date."

    def clean(self):
        cleaned_data = super().clean()
        if not self.profile:
            return cleaned_data
        override_date = cleaned_data.get("override_date")
        hour_adjustment = cleaned_data.get("hours_available")
        if override_date is None or hour_adjustment is None:
            return cleaned_data
        baseline = (
            StudentAvailability.objects.filter(profile=self.profile, weekday=override_date.weekday())
            .values_list("hours_available", flat=True)
            .first()
            or Decimal("0")
        )
        adjusted_total = baseline + hour_adjustment
        if adjusted_total < 0:
            self.add_error("hours_available", "This adjustment would reduce the day below 0 hours.")
        return cleaned_data
class WeeklyAvailabilityForm(StyledFormMixin, forms.Form):
    monday_hours = forms.DecimalField(min_value=Decimal("0"), max_digits=4, decimal_places=2)
    tuesday_hours = forms.DecimalField(min_value=Decimal("0"), max_digits=4, decimal_places=2)
    wednesday_hours = forms.DecimalField(min_value=Decimal("0"), max_digits=4, decimal_places=2)
    thursday_hours = forms.DecimalField(min_value=Decimal("0"), max_digits=4, decimal_places=2)
    friday_hours = forms.DecimalField(min_value=Decimal("0"), max_digits=4, decimal_places=2)
    saturday_hours = forms.DecimalField(min_value=Decimal("0"), max_digits=4, decimal_places=2)
    sunday_hours = forms.DecimalField(min_value=Decimal("0"), max_digits=4, decimal_places=2)


class TaskIntakeForm(StyledFormMixin, forms.Form):
    raw_message = forms.CharField(widget=forms.Textarea(attrs={"rows": 12}), label="Paste email or request")
    attachments = MultipleFileField(required=False, label="Optional screenshots or images")


class TaskForm(StyledFormMixin, forms.ModelForm):
    due_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    class Meta:
        model = Task
        fields = [
            "title",
            "raw_message",
            "description",
            "priority",
            "status",
            "due_date",
            "raw_due_text",
            "respond_to_text",
            "estimated_minutes",
            "assigned_to",
            "additional_assignees",
            "rotate_additional_assignee",
            "requested_by",
            "recurring_task",
            "recurrence_pattern",
            "recurrence_interval",
            "recurrence_day_of_week",
            "recurrence_day_of_month",
        ]
        widgets = {
            "raw_message": forms.Textarea(attrs={"rows": 6}),
            "description": forms.Textarea(attrs={"rows": 5}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        worker_users = User.objects.filter(role__in=UserRole.worker_roles()).order_by("first_name", "last_name", "username")
        self.fields["status"].choices = [choice for choice in self.fields["status"].choices if choice[0] != TaskStatus.ASSIGNED]
        self.fields["assigned_to"].queryset = User.objects.filter(Q(role__in=UserRole.worker_roles()) | Q(role=UserRole.SUPERVISOR, assignable_to_tasks=True)).order_by("first_name", "last_name", "username")
        self.fields["additional_assignees"].queryset = worker_users
        self.fields["additional_assignees"].required = False
        self.fields["additional_assignees"].widget = forms.CheckboxSelectMultiple(choices=self.fields["additional_assignees"].choices)
        self.fields["additional_assignees"].label = "Fixed additional assignees"
        self.fields["additional_assignees"].help_text = "Pick any teammates who should always be added to this task."
        self.fields["rotate_additional_assignee"].label = "Also add one rotating teammate"
        self.fields["rotate_additional_assignee"].help_text = "TaskForge will pick one more worker automatically based on availability and rotation."
        self.fields["respond_to_text"].label = "Notify when done"
        self.fields["respond_to_text"].help_text = "Person or office to notify after the task is complete"
        self.fields["requested_by"].queryset = User.objects.order_by("username")
        self.fields["assigned_to"].label_from_instance = _user_choice_label
        self.fields["additional_assignees"].label_from_instance = _user_choice_label
        self.fields["requested_by"].label_from_instance = _user_choice_label
        self.fields["recurring_task"].label = "Repeats on a schedule"
        self.fields["recurring_task"].help_text = "Turn this on only if this task should repeat automatically."
        self.fields["recurrence_pattern"].label = "Repeat cadence"
        self.fields["recurrence_pattern"].help_text = "Choose how often the task should repeat."
        self.fields["recurrence_interval"].label = "Repeat every"
        self.fields["recurrence_interval"].help_text = "Use 1 for every cycle, 2 for every other cycle, and so on."
        self.fields["recurrence_day_of_week"].label = "Weekday to repeat on"
        self.fields["recurrence_day_of_week"].help_text = "Only needed for weekly repeating tasks."
        self.fields["recurrence_day_of_month"].label = "Day of month to repeat on"
        self.fields["recurrence_day_of_month"].help_text = "Only needed for monthly repeating tasks."

    def clean(self):
        cleaned_data = super().clean()
        assigned_to = cleaned_data.get("assigned_to")
        additional_assignees = cleaned_data.get("additional_assignees")
        if assigned_to and additional_assignees:
            cleaned_data["additional_assignees"] = additional_assignees.exclude(pk=assigned_to.pk)

        recurring_task = cleaned_data.get("recurring_task")
        recurrence_pattern = cleaned_data.get("recurrence_pattern")
        recurrence_interval = cleaned_data.get("recurrence_interval")
        recurrence_day_of_week = cleaned_data.get("recurrence_day_of_week")
        recurrence_day_of_month = cleaned_data.get("recurrence_day_of_month")

        if not recurring_task:
            cleaned_data["recurrence_pattern"] = ""
            cleaned_data["recurrence_interval"] = None
            cleaned_data["recurrence_day_of_week"] = None
            cleaned_data["recurrence_day_of_month"] = None
            return cleaned_data

        if not recurrence_pattern:
            self.add_error("recurrence_pattern", "Choose how often this task should repeat.")
        if not recurrence_interval:
            cleaned_data["recurrence_interval"] = 1

        if recurrence_pattern == "weekly":
            if recurrence_day_of_week is None:
                self.add_error("recurrence_day_of_week", "Choose the weekday for this weekly task.")
            cleaned_data["recurrence_day_of_month"] = None
        elif recurrence_pattern == "monthly":
            if not recurrence_day_of_month:
                self.add_error("recurrence_day_of_month", "Choose the day of the month for this monthly task.")
            cleaned_data["recurrence_day_of_week"] = None
        else:
            cleaned_data["recurrence_day_of_week"] = None
            cleaned_data["recurrence_day_of_month"] = None
        return cleaned_data



class TaskManualForm(TaskForm):
    class Meta(TaskForm.Meta):
        fields = [
            "title",
            "description",
            "priority",
            "status",
            "due_date",
            "respond_to_text",
            "estimated_minutes",
            "assigned_to",
            "additional_assignees",
            "rotate_additional_assignee",
            "requested_by",
            "recurring_task",
            "recurrence_pattern",
            "recurrence_interval",
            "recurrence_day_of_week",
            "recurrence_day_of_month",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["description"].label = "Task details"
        self.fields["description"].help_text = "Describe the work that needs to be done."
        self.fields["due_date"].label = "Due date"
        self.fields["due_date"].help_text = "Leave blank if this can be scheduled from priority."

class TaskUpdateForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Task
        fields = ["status"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [choice for choice in self.fields["status"].choices if choice[0] != TaskStatus.ASSIGNED]


class SupervisorForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "assignable_to_tasks"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignable_to_tasks"].label = "Allow tasks to be assigned to this supervisor"
        self.fields["assignable_to_tasks"].help_text = "Turn this off if this supervisor should stay out of the assignment rotation."


class TaskNoteForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TaskNote
        fields = ["body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Add a task note"})}


class TaskChecklistItemForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TaskChecklistItem
        fields = ["title"]
        widgets = {"title": forms.TextInput(attrs={"placeholder": "Add checklist item"})}
        labels = {"title": ""}


class AppPasswordChangeForm(StyledFormMixin, PasswordChangeForm):
    pass


class SupervisorStudentPasswordResetForm(StyledFormMixin, SetPasswordForm):
    pass


class TaskIntakeDraftForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TaskIntakeDraft
        fields = ["raw_message"]


class TaskAttachmentForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TaskAttachment
        fields = ["file"]


class RecurringTaskTemplateForm(StyledFormMixin, forms.ModelForm):
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    next_run_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))

    class Meta:
        model = RecurringTaskTemplate
        fields = [
            "title",
            "description",
            "priority",
            "estimated_minutes",
            "assign_to",
            "additional_assignees",
            "rotate_additional_assignee",
            "requested_by",
            "recurrence_pattern",
            "recurrence_interval",
            "day_of_week",
            "day_of_month",
            "start_date",
            "next_run_date",
            "active",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["description"].label = "Task details"
        self.fields["description"].help_text = "Describe the work that should happen each time this task repeats."
        self.fields["estimated_minutes"].label = "Time estimate"
        self.fields["assign_to"].label = "Default assignee"
        self.fields["assign_to"].help_text = "Choose the main teammate for the first run. Later runs can rotate based on workload."
        self.fields["assign_to"].queryset = User.objects.filter(role__in=UserRole.worker_roles()).order_by("first_name", "last_name", "username")
        self.fields["additional_assignees"].queryset = User.objects.filter(role__in=UserRole.worker_roles()).order_by("first_name", "last_name", "username")
        self.fields["additional_assignees"].required = False
        self.fields["additional_assignees"].widget = forms.CheckboxSelectMultiple(choices=self.fields["additional_assignees"].choices)
        self.fields["additional_assignees"].label = "Fixed additional assignees"
        self.fields["additional_assignees"].help_text = "Pick any teammates who should always join each run of this recurring task."
        self.fields["rotate_additional_assignee"].label = "Also add one rotating teammate"
        self.fields["rotate_additional_assignee"].help_text = "Each generated task can add one more worker automatically based on availability and rotation."
        self.fields["requested_by"].queryset = User.objects.order_by("first_name", "last_name", "username")
        self.fields["assign_to"].label_from_instance = _user_choice_label
        self.fields["additional_assignees"].label_from_instance = _user_choice_label
        self.fields["requested_by"].label_from_instance = _user_choice_label
        self.fields["recurrence_pattern"].label = "Repeat cadence"
        self.fields["recurrence_pattern"].help_text = "Choose how often this recurring task should happen."
        self.fields["recurrence_interval"].label = "Repeat every"
        self.fields["recurrence_interval"].help_text = "Use 1 for every cycle, 2 for every other cycle, and so on."
        self.fields["day_of_week"].label = "Weekday to repeat on"
        self.fields["day_of_week"].help_text = "Only needed for weekly recurring tasks."
        self.fields["day_of_month"].label = "Day of month to repeat on"
        self.fields["day_of_month"].help_text = "Only needed for monthly recurring tasks."
        self.fields["start_date"].label = "Start date"
        self.fields["start_date"].help_text = "The first date this recurring task should begin from."
        self.fields["next_run_date"].label = "Next run date"
        self.fields["next_run_date"].help_text = "The next date the app will create this task."
        self.fields["active"].label = "Recurring task is active"
        self.fields["active"].help_text = "Turn this off to pause future task creation."

    def clean(self):
        cleaned_data = super().clean()
        assign_to = cleaned_data.get("assign_to")
        additional_assignees = cleaned_data.get("additional_assignees")
        if assign_to and additional_assignees:
            cleaned_data["additional_assignees"] = additional_assignees.exclude(pk=assign_to.pk)

        recurrence_pattern = cleaned_data.get("recurrence_pattern")
        day_of_week = cleaned_data.get("day_of_week")
        day_of_month = cleaned_data.get("day_of_month")
        recurrence_interval = cleaned_data.get("recurrence_interval")

        if not recurrence_interval:
            cleaned_data["recurrence_interval"] = 1

        if recurrence_pattern == "weekly":
            if day_of_week is None:
                self.add_error("day_of_week", "Choose the weekday for this weekly recurring task.")
            cleaned_data["day_of_month"] = None
        elif recurrence_pattern == "monthly":
            if not day_of_month:
                self.add_error("day_of_month", "Choose the day of the month for this monthly recurring task.")
            cleaned_data["day_of_week"] = None
        else:
            cleaned_data["day_of_week"] = None
            cleaned_data["day_of_month"] = None

        return cleaned_data




