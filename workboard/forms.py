from decimal import Decimal

from django import forms
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm

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
            widget.attrs["class"] = f"{widget.attrs.get('class', '')} form-control".strip()


class StudentWorkerProfileForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = StudentWorkerProfile
        fields = [
            "display_name",
            "email",
            "active_status",
            "normal_shift_availability",
            "max_hours_per_day",
            "skill_notes",
        ]
        widgets = {
            "normal_shift_availability": forms.Textarea(attrs={"rows": 3}),
            "skill_notes": forms.Textarea(attrs={"rows": 3}),
        }


class StudentAvailabilityForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = StudentAvailability
        fields = ["weekday", "hours_available"]


class StudentAvailabilityOverrideForm(StyledFormMixin, forms.ModelForm):
    override_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))

    class Meta:
        model = StudentAvailabilityOverride
        fields = ["override_date", "hours_available", "note"]


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
        student_users = User.objects.filter(role=UserRole.STUDENT_WORKER).order_by("username")
        self.fields["status"].choices = [choice for choice in self.fields["status"].choices if choice[0] != TaskStatus.ASSIGNED]
        self.fields["assigned_to"].queryset = User.objects.order_by("role", "username")
        self.fields["additional_assignees"].queryset = student_users
        self.fields["additional_assignees"].required = False
        self.fields["additional_assignees"].widget = forms.SelectMultiple(attrs={"class": "form-control", "size": 6})
        self.fields["respond_to_text"].label = "Notify when done"
        self.fields["respond_to_text"].help_text = "Person or office to notify after the task is complete"
        self.fields["requested_by"].queryset = User.objects.order_by("username")

    def clean(self):
        cleaned_data = super().clean()
        assigned_to = cleaned_data.get("assigned_to")
        additional_assignees = cleaned_data.get("additional_assignees")
        if assigned_to and additional_assignees:
            cleaned_data["additional_assignees"] = additional_assignees.exclude(pk=assigned_to.pk)
        return cleaned_data


class TaskUpdateForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Task
        fields = ["status"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [choice for choice in self.fields["status"].choices if choice[0] != TaskStatus.ASSIGNED]


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
        self.fields["assign_to"].queryset = User.objects.filter(role=UserRole.STUDENT_WORKER).order_by("username")
        self.fields["requested_by"].queryset = User.objects.order_by("username")
