from django import forms
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm

from .models import (
    RecurringTaskTemplate,
    StudentAvailability,
    StudentAvailabilityOverride,
    StudentWorkerProfile,
    Task,
    TaskChecklistItem,
    TaskNote,
    User,
    UserRole,
)


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


class TaskIntakeForm(StyledFormMixin, forms.Form):
    raw_message = forms.CharField(widget=forms.Textarea(attrs={"rows": 10}), label="Paste request")


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
            "waiting_person",
            "respond_to_text",
            "estimated_minutes",
            "assigned_to",
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
        self.fields["assigned_to"].queryset = User.objects.filter(role=UserRole.STUDENT_WORKER).order_by("username")
        self.fields["requested_by"].queryset = User.objects.order_by("username")


class TaskUpdateForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Task
        fields = ["status"]


class TaskNoteForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TaskNote
        fields = ["body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Add a task note"})}


class TaskChecklistItemForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TaskChecklistItem
        fields = ["title", "is_completed", "sort_order"]


class AppPasswordChangeForm(StyledFormMixin, PasswordChangeForm):
    pass


class SupervisorStudentPasswordResetForm(StyledFormMixin, SetPasswordForm):
    pass


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
