from datetime import date, datetime, time, timedelta
from pathlib import Path
import json
from decimal import Decimal

from django import forms
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.db.models import Q
from django.utils import timezone

from .models import (
    Priority,
    RecurringTaskTemplate,
    ScheduleAdjustmentRequest,
    StudentScheduleOverride,
    StudentWorkerProfile,
    Task,
    TaskAttachment,
    TaskChecklistItem,
    TaskNote,
    TaskStatus,
    TaskScheduleBlock,
    Team,
    User,
    UserRole,
    Weekday,
)
from .services import TaskAssignmentService


DAY_FIELD_CONFIG = [
    ("monday", "Monday", Weekday.MONDAY),
    ("tuesday", "Tuesday", Weekday.TUESDAY),
    ("wednesday", "Wednesday", Weekday.WEDNESDAY),
    ("thursday", "Thursday", Weekday.THURSDAY),
    ("friday", "Friday", Weekday.FRIDAY),
    ("saturday", "Saturday", Weekday.SATURDAY),
    ("sunday", "Sunday", Weekday.SUNDAY),
]

TASK_WINDOW_DAY_CONFIG = [
    (f"task_window_day_{offset}", offset)
    for offset in range(7)
]

TASK_SAVED_VIEW_CHOICES = [
    ("", "All visible tasks"),
    ("today", "Today's focus"),
    ("overdue", "Overdue"),
    ("waiting", "Waiting / blocked"),
    ("recurring", "Recurring work"),
    ("scheduled", "Scheduled work"),
]

TASK_DUE_SCOPE_CHOICES = [
    ("", "Any due date"),
    ("overdue", "Overdue"),
    ("today", "Due today"),
    ("week", "Due in the next 7 days"),
    ("none", "No due date"),
]

WEEKDAY_SELECT_CHOICES = [("", "Select a weekday"), *Weekday.choices]


def _format_time_label(value: time) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _format_short_date_label(value: date) -> str:
    return f"{value.strftime('%a')} {value.strftime('%b')} {value.day}"


SCHEDULE_DAY_START_MINUTES = 7 * 60
SCHEDULE_DAY_END_MINUTES = 18 * 60
SCHEDULE_DAY_START_TIME = time(7, 0)
SCHEDULE_DAY_END_TIME = time(18, 0)


HALF_HOUR_CHOICES = [("", "Not scheduled")]
for minutes in range(SCHEDULE_DAY_START_MINUTES, SCHEDULE_DAY_END_MINUTES + 1, 30):
    hour, minute = divmod(minutes, 60)
    value = time(hour, minute)
    HALF_HOUR_CHOICES.append((value.strftime("%H:%M"), _format_time_label(value)))

WEEKLY_CALENDAR_SLOTS = []
for index, choice in enumerate(HALF_HOUR_CHOICES[1:-1]):
    value, full_label = choice
    WEEKLY_CALENDAR_SLOTS.append(
        {
            "index": index,
            "value": value,
            "end_value": HALF_HOUR_CHOICES[index + 2][0],
            "label": full_label if value.endswith(":00") else "",
            "full_label": full_label,
        }
    )

MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".txt",
    ".webp",
    ".xls",
    ".xlsx",
}


def _validate_uploaded_file(uploaded_file):
    extension = Path(getattr(uploaded_file, "name", "")).suffix.lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        raise forms.ValidationError(
            "Unsupported file type. Upload a PDF, image, spreadsheet, text file, or Office document."
        )
    if getattr(uploaded_file, "size", 0) > MAX_UPLOAD_SIZE_BYTES:
        raise forms.ValidationError("Each attachment must be 10 MB or smaller.")
    return uploaded_file


class HalfHourSelect(forms.Select):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("choices", HALF_HOUR_CHOICES)
        super().__init__(*args, **kwargs)


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if not data:
            return []
        if isinstance(data, (list, tuple)):
            return [_validate_uploaded_file(single_file_clean(item, initial)) for item in data]
        return [_validate_uploaded_file(single_file_clean(data, initial))]


class StyledFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.HiddenInput):
                continue
            if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
                widget.attrs["class"] = f"{widget.attrs.get('class', '')} form-check-input".strip()
            else:
                widget.attrs["class"] = f"{widget.attrs.get('class', '')} form-control".strip()


class HalfHourTimeField(forms.TimeField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("input_formats", ["%H:%M"])
        kwargs.setdefault("widget", HalfHourSelect())
        super().__init__(*args, **kwargs)


def _user_choice_label(user: User) -> str:
    full_name = user.get_full_name().strip()
    return full_name or user.display_label


def _window_minutes(start_value: time, end_value: time) -> int:
    start_dt = datetime.combine(datetime.today(), start_value)
    end_dt = datetime.combine(datetime.today(), end_value)
    return int((end_dt - start_dt).total_seconds() // 60)

def _schedule_block_within_workday(start_value: time, end_value: time) -> bool:
    return start_value >= SCHEDULE_DAY_START_TIME and end_value <= SCHEDULE_DAY_END_TIME


def _legacy_hours_to_window(hours_value: Decimal | None) -> tuple[time | None, time | None, Decimal]:
    if not hours_value:
        return None, None, Decimal("0")
    total_minutes = int(hours_value * 60)
    if total_minutes % 30 != 0:
        raise ValueError("Legacy schedule hours must be in 30-minute increments.")
    start_value = time(9, 0)
    end_dt = datetime.combine(datetime.today(), start_value) + timedelta(minutes=total_minutes)
    return start_value, end_dt.time(), hours_value


def _start_of_week(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _parse_date_value(raw_value: str | None) -> date | None:
    if not raw_value:
        return None
    try:
        return datetime.strptime(raw_value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _worker_user_queryset(*, include_assignable_supervisors: bool = False, team=None):
    worker_filter = Q(role__in=UserRole.worker_roles())
    if include_assignable_supervisors:
        worker_filter |= Q(role=UserRole.SUPERVISOR, assignable_to_tasks=True)
    queryset = User.objects.filter(worker_filter)
    if team is not None:
        queryset = queryset.filter(team=team)
    return queryset.order_by("first_name", "last_name", "username")


def _team_queryset():
    return Team.objects.order_by("name", "pk")


def _configure_additional_assignees_field(field, *, help_text: str, team=None) -> None:
    field.queryset = _worker_user_queryset(team=team)
    field.required = False
    field.widget = forms.CheckboxSelectMultiple(choices=field.choices)
    field.label = "Fixed additional assignees"
    field.help_text = help_text
    field.label_from_instance = _user_choice_label


def _configure_rotation_count_field(field, *, count: int, help_text: str) -> None:
    field.min_value = 0
    field.required = False
    field.initial = count
    field.label = "Number of rotating teammates"
    field.help_text = help_text
    field.widget.attrs.update({"min": "0", "step": "1"})


class StudentWorkerProfileForm(StyledFormMixin, forms.ModelForm):
    team = forms.ModelChoiceField(queryset=Team.objects.none(), required=False)

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

    def __init__(self, *args, actor=None, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        self.fields["email"].required = False
        self.fields["email"].help_text = ""
        if not getattr(self.instance, "pk", None):
            self.fields["email"].help_text = "Optional. Leave blank if you do not want to store an email address for this person."
        self.fields["skill_notes"].label = "Notes"
        self.fields["skill_notes"].help_text = "Optional notes about strengths, training, or preferences."
        self.fields["team"].queryset = _team_queryset()
        self.fields["team"].label = "Team"
        self.fields["team"].required = True
        if getattr(self.instance, "pk", None):
            self.initial.setdefault("team", self.instance.user.team_id)
        if actor and not actor.is_admin:
            self.fields["team"].widget = forms.HiddenInput()
            self.fields["team"].required = False
            self.initial["team"] = actor.team_id
        else:
            self.fields["team"].help_text = "Choose which team this person belongs to."

    def clean_team(self):
        if self.actor and not self.actor.is_admin:
            return self.actor.team
        team = self.cleaned_data.get("team")
        if not team:
            raise forms.ValidationError("Choose a team for this person.")
        return team


def _serialize_schedule_segments(blocks):
    return json.dumps([[start_time.strftime("%H:%M"), end_time.strftime("%H:%M")] for start_time, end_time in blocks])


def _aggregate_schedule_blocks(blocks):
    if not blocks:
        return None, None, Decimal("0")
    total_minutes = sum(_window_minutes(start_time, end_time) for start_time, end_time in blocks)
    return blocks[0][0], blocks[-1][1], Decimal(total_minutes) / Decimal("60")


class BaseScheduleBlocksForm(StyledFormMixin, forms.Form):
    schedule_day_config = []

    def day_rows(self):
        override_summary_map = getattr(self, "override_summary_map", {})
        rows = []
        for prefix, label, *rest in self.schedule_day_config:
            rows.append(
                {
                    "prefix": prefix,
                    "label": label,
                    "segments": self[f"{prefix}_segments"],
                    "weekday": rest[0] if rest else "",
                    "override_entries": override_summary_map.get(prefix, []),
                }
            )
        return rows

    def calendar_slots(self):
        return WEEKLY_CALENDAR_SLOTS

    def _parse_segments(self, raw_value, *, field_name, label):
        if not raw_value:
            return []
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError:
            self.add_error(field_name, f"{label} has an invalid schedule payload.")
            return None
        if not isinstance(payload, list):
            self.add_error(field_name, f"{label} has an invalid schedule payload.")
            return None

        parsed_blocks = []
        for item in payload:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                self.add_error(field_name, f"{label} has an invalid schedule block.")
                return None
            start_raw, end_raw = item
            try:
                start_value = datetime.strptime(str(start_raw), "%H:%M").time()
                end_value = datetime.strptime(str(end_raw), "%H:%M").time()
            except ValueError:
                self.add_error(field_name, f"{label} must use 30-minute times.")
                return None
            if end_value <= start_value:
                self.add_error(field_name, f"{label} end time must be after the start time.")
                return None
            if _window_minutes(start_value, end_value) % 30 != 0:
                self.add_error(field_name, f"{label} must use 30-minute increments.")
                return None
            if not _schedule_block_within_workday(start_value, end_value):
                self.add_error(field_name, f"{label} must stay between 7:00 AM and 6:00 PM.")
                return None
            parsed_blocks.append((start_value, end_value))

        parsed_blocks.sort(key=lambda block: (block[0], block[1]))
        normalized_blocks = []
        for start_value, end_value in parsed_blocks:
            if not normalized_blocks:
                normalized_blocks.append([start_value, end_value])
                continue
            last_start, last_end = normalized_blocks[-1]
            if start_value <= last_end:
                normalized_blocks[-1][1] = max(last_end, end_value)
            else:
                normalized_blocks.append([start_value, end_value])
        return [(start_value, end_value) for start_value, end_value in normalized_blocks]

    def _legacy_blocks_for_day(self, cleaned_data, *, prefix, label):
        start_value = cleaned_data.get(f"{prefix}_start")
        end_value = cleaned_data.get(f"{prefix}_end")
        legacy_hours = cleaned_data.get(f"{prefix}_hours")

        if start_value or end_value:
            if not start_value:
                self.add_error(f"{prefix}_start", f"Choose a start time for {label}.")
                return None
            if not end_value:
                self.add_error(f"{prefix}_end", f"Choose an end time for {label}.")
                return None
            if end_value <= start_value:
                self.add_error(f"{prefix}_end", f"{label} end time must be after the start time.")
                return None
            if _window_minutes(start_value, end_value) % 30 != 0:
                self.add_error(f"{prefix}_end", f"{label} must use 30-minute increments.")
                return None
            if not _schedule_block_within_workday(start_value, end_value):
                self.add_error(f"{prefix}_end", f"{label} must stay between 7:00 AM and 6:00 PM.")
                return None
            return [(start_value, end_value)]

        try:
            start_value, end_value, hours_value = _legacy_hours_to_window(legacy_hours)
        except ValueError:
            self.add_error(f"{prefix}_start", f"{label} must use 30-minute increments.")
            return None
        if start_value and end_value:
            return [(start_value, end_value)]
        return []


class WeeklyAvailabilityForm(BaseScheduleBlocksForm):
    schedule_day_config = DAY_FIELD_CONFIG

    monday_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    monday_start = HalfHourTimeField(label="Monday start", widget=forms.HiddenInput())
    monday_end = HalfHourTimeField(label="Monday end", widget=forms.HiddenInput())
    monday_hours = forms.DecimalField(required=False, min_value=Decimal("0"), max_digits=4, decimal_places=2, widget=forms.HiddenInput())
    tuesday_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    tuesday_start = HalfHourTimeField(label="Tuesday start", widget=forms.HiddenInput())
    tuesday_end = HalfHourTimeField(label="Tuesday end", widget=forms.HiddenInput())
    tuesday_hours = forms.DecimalField(required=False, min_value=Decimal("0"), max_digits=4, decimal_places=2, widget=forms.HiddenInput())
    wednesday_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    wednesday_start = HalfHourTimeField(label="Wednesday start", widget=forms.HiddenInput())
    wednesday_end = HalfHourTimeField(label="Wednesday end", widget=forms.HiddenInput())
    wednesday_hours = forms.DecimalField(required=False, min_value=Decimal("0"), max_digits=4, decimal_places=2, widget=forms.HiddenInput())
    thursday_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    thursday_start = HalfHourTimeField(label="Thursday start", widget=forms.HiddenInput())
    thursday_end = HalfHourTimeField(label="Thursday end", widget=forms.HiddenInput())
    thursday_hours = forms.DecimalField(required=False, min_value=Decimal("0"), max_digits=4, decimal_places=2, widget=forms.HiddenInput())
    friday_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    friday_start = HalfHourTimeField(label="Friday start", widget=forms.HiddenInput())
    friday_end = HalfHourTimeField(label="Friday end", widget=forms.HiddenInput())
    friday_hours = forms.DecimalField(required=False, min_value=Decimal("0"), max_digits=4, decimal_places=2, widget=forms.HiddenInput())
    saturday_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    saturday_start = HalfHourTimeField(label="Saturday start", widget=forms.HiddenInput())
    saturday_end = HalfHourTimeField(label="Saturday end", widget=forms.HiddenInput())
    saturday_hours = forms.DecimalField(required=False, min_value=Decimal("0"), max_digits=4, decimal_places=2, widget=forms.HiddenInput())
    sunday_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    sunday_start = HalfHourTimeField(label="Sunday start", widget=forms.HiddenInput())
    sunday_end = HalfHourTimeField(label="Sunday end", widget=forms.HiddenInput())
    sunday_hours = forms.DecimalField(required=False, min_value=Decimal("0"), max_digits=4, decimal_places=2, widget=forms.HiddenInput())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.schedule_blocks = {}
        self.schedule_windows = {}

    def clean(self):
        cleaned_data = super().clean()
        schedule_blocks = {}
        schedule_windows = {}
        for prefix, label, weekday in self.schedule_day_config:
            raw_segments = cleaned_data.get(f"{prefix}_segments")
            if raw_segments:
                blocks = self._parse_segments(raw_segments, field_name=f"{prefix}_segments", label=label)
            else:
                blocks = self._legacy_blocks_for_day(cleaned_data, prefix=prefix, label=label)
            if blocks is None:
                continue

            start_value, end_value, hours_value = _aggregate_schedule_blocks(blocks)
            cleaned_data[f"{prefix}_segments"] = _serialize_schedule_segments(blocks)
            cleaned_data[f"{prefix}_start"] = start_value
            cleaned_data[f"{prefix}_end"] = end_value
            cleaned_data[f"{prefix}_hours"] = hours_value
            schedule_blocks[weekday] = [
                {"start_time": start_block, "end_time": end_block}
                for start_block, end_block in blocks
            ]
            schedule_windows[weekday] = {
                "start_time": start_value,
                "end_time": end_value,
                "hours_available": hours_value,
            }

        cleaned_data["schedule_blocks"] = schedule_blocks
        cleaned_data["schedule_windows"] = schedule_windows
        self.schedule_blocks = schedule_blocks
        self.schedule_windows = schedule_windows
        return cleaned_data


class StudentScheduleOverrideForm(BaseScheduleBlocksForm, forms.ModelForm):
    schedule_day_config = [("override", "Override schedule", None)]

    override_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    override_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    override_start = HalfHourTimeField(label="Override start", widget=forms.HiddenInput())
    override_end = HalfHourTimeField(label="Override end", widget=forms.HiddenInput())
    override_hours = forms.DecimalField(required=False, min_value=Decimal("0"), max_digits=4, decimal_places=2, widget=forms.HiddenInput())

    class Meta:
        model = StudentScheduleOverride
        fields = ["override_date", "note"]

    def __init__(self, *args, profile=None, **kwargs):
        self.profile = profile
        super().__init__(*args, **kwargs)
        self.fields["note"].help_text = "Optional note about why this date should use a different schedule."
        if self.instance.pk and not self.initial.get("override_segments"):
            blocks = [
                (block.start_time, block.end_time)
                for block in self.instance.blocks.order_by("position", "start_time", "end_time", "pk")
            ]
            self.initial["override_segments"] = _serialize_schedule_segments(blocks)
            start_value, end_value, hours_value = _aggregate_schedule_blocks(blocks)
            self.initial["override_start"] = start_value
            self.initial["override_end"] = end_value
            self.initial["override_hours"] = hours_value

    def clean(self):
        cleaned_data = super().clean()
        blocks = self._parse_segments(cleaned_data.get("override_segments"), field_name="override_segments", label="Temporary schedule")
        if blocks is None:
            return cleaned_data
        start_value, end_value, hours_value = _aggregate_schedule_blocks(blocks)
        cleaned_data["override_segments"] = _serialize_schedule_segments(blocks)
        cleaned_data["override_start"] = start_value
        cleaned_data["override_end"] = end_value
        cleaned_data["override_hours"] = hours_value
        cleaned_data["schedule_blocks"] = [
            {"start_time": start_block, "end_time": end_block}
            for start_block, end_block in blocks
        ]
        cleaned_data["schedule_windows"] = {
            "start_time": start_value,
            "end_time": end_value,
            "hours_available": hours_value,
        }
        return cleaned_data


class ScheduleAdjustmentRequestForm(BaseScheduleBlocksForm, forms.ModelForm):
    schedule_day_config = [("request", "Requested schedule", None)]

    requested_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    request_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    request_start = HalfHourTimeField(label="Requested start", widget=forms.HiddenInput())
    request_end = HalfHourTimeField(label="Requested end", widget=forms.HiddenInput())
    request_hours = forms.DecimalField(required=False, min_value=Decimal("0"), max_digits=4, decimal_places=2, widget=forms.HiddenInput())

    class Meta:
        model = ScheduleAdjustmentRequest
        fields = ["requested_date", "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["requested_date"].label = "Date to adjust"
        self.fields["requested_date"].help_text = "Choose the specific date you need to change."
        self.fields["note"].label = "Why this needs to change"
        self.fields["note"].help_text = "Optional. Add context for the supervisor reviewing this request."
        if self.instance.pk and not self.initial.get("request_segments"):
            blocks = [
                (block.start_time, block.end_time)
                for block in self.instance.blocks.order_by("position", "start_time", "end_time", "pk")
            ]
            self.initial["request_segments"] = _serialize_schedule_segments(blocks)
            start_value, end_value, hours_value = _aggregate_schedule_blocks(blocks)
            self.initial["request_start"] = start_value
            self.initial["request_end"] = end_value
            self.initial["request_hours"] = hours_value

    def clean(self):
        cleaned_data = super().clean()
        blocks = self._parse_segments(cleaned_data.get("request_segments"), field_name="request_segments", label="Requested schedule")
        if blocks is None:
            return cleaned_data
        start_value, end_value, hours_value = _aggregate_schedule_blocks(blocks)
        cleaned_data["request_segments"] = _serialize_schedule_segments(blocks)
        cleaned_data["request_start"] = start_value
        cleaned_data["request_end"] = end_value
        cleaned_data["request_hours"] = hours_value
        cleaned_data["schedule_blocks"] = [
            {"start_time": start_block, "end_time": end_block}
            for start_block, end_block in blocks
        ]
        cleaned_data["schedule_windows"] = {
            "start_time": start_value,
            "end_time": end_value,
            "hours_available": hours_value,
        }
        return cleaned_data


class TaskIntakeForm(StyledFormMixin, forms.Form):
    raw_message = forms.CharField(widget=forms.Textarea(attrs={"rows": 12}), label="Paste email or request")
    attachments = MultipleFileField(
        required=False,
        label="Optional screenshots or images",
        help_text="Allowed file types: PDF, images, spreadsheets, text files, and Office documents. Each file must be 10 MB or smaller.",
    )


class TaskBoardFilterForm(StyledFormMixin, forms.Form):
    saved_view = forms.ChoiceField(required=False, choices=TASK_SAVED_VIEW_CHOICES)
    q = forms.CharField(required=False, label="Search")
    priority = forms.ChoiceField(required=False, choices=[("", "Any priority"), *Priority.choices])
    due_scope = forms.ChoiceField(required=False, choices=TASK_DUE_SCOPE_CHOICES)
    assigned_to = forms.ModelChoiceField(required=False, queryset=User.objects.none())

    def __init__(self, *args, user=None, include_assignee=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["saved_view"].label = "Saved view"
        self.fields["saved_view"].help_text = "Start from a common board view, then narrow it further if needed."
        self.fields["q"].label = "Search tasks"
        self.fields["q"].widget.attrs.setdefault("placeholder", "Search by title or task details")
        self.fields["priority"].label = "Priority"
        self.fields["due_scope"].label = "Due date"

        if include_assignee:
            team = None if (user and user.is_admin) else getattr(user, "team", None)
            self.fields["assigned_to"].queryset = _worker_user_queryset(include_assignable_supervisors=True, team=team)
            self.fields["assigned_to"].label = "Teammate"
            self.fields["assigned_to"].label_from_instance = _user_choice_label
        else:
            self.fields.pop("assigned_to")


class TaskForm(StyledFormMixin, forms.ModelForm):
    scheduled_week_of = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    scheduled_date = forms.DateField(required=False, widget=forms.HiddenInput())
    scheduled_start_time = HalfHourTimeField(required=False, widget=forms.HiddenInput())
    scheduled_end_time = HalfHourTimeField(required=False, widget=forms.HiddenInput())
    scheduled_window_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    scheduled_window_start = HalfHourTimeField(required=False, widget=forms.HiddenInput())
    scheduled_window_end = HalfHourTimeField(required=False, widget=forms.HiddenInput())
    scheduled_window_hours = forms.DecimalField(required=False, min_value=Decimal("0"), max_digits=4, decimal_places=2, widget=forms.HiddenInput())
    task_window_day_0_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    task_window_day_1_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    task_window_day_2_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    task_window_day_3_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    task_window_day_4_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    task_window_day_5_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    task_window_day_6_segments = forms.CharField(required=False, widget=forms.HiddenInput())
    recurrence_day_of_week = forms.TypedChoiceField(
        required=False,
        choices=WEEKDAY_SELECT_CHOICES,
        coerce=lambda value: int(value) if value not in ("", None) else None,
        empty_value=None,
    )

    class Meta:
        model = Task
        fields = [
            "team",
            "title",
            "raw_message",
            "description",
            "priority",
            "status",
            "due_date",
            "scheduled_date",
            "scheduled_start_time",
            "scheduled_end_time",
            "raw_due_text",
            "respond_to_text",
            "estimated_minutes",
            "assigned_to",
            "additional_assignees",
            "rotating_additional_assignee_count",
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

    def __init__(self, *args, actor=None, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [choice for choice in self.fields["status"].choices if choice[0] != TaskStatus.ASSIGNED]
        self.fields["team"].queryset = _team_queryset()
        self.fields["team"].label = "Team"
        self.fields["team"].required = True
        selected_team = self._resolve_selected_team()
        if actor and not actor.is_admin:
            self.fields["team"].widget = forms.HiddenInput()
            self.fields["team"].required = False
            self.initial["team"] = actor.team_id
            selected_team = actor.team
        else:
            self.fields["team"].help_text = "Choose which team owns this task."
            if selected_team:
                self.initial.setdefault("team", selected_team.pk)
        self.fields["assigned_to"].queryset = _worker_user_queryset(include_assignable_supervisors=True, team=selected_team)
        self.fields["assigned_to"].label = "Assign to"
        self.fields["assigned_to"].help_text = "Choose the main teammate for this task."
        self.fields["assigned_to"].label_from_instance = _user_choice_label
        _configure_additional_assignees_field(
            self.fields["additional_assignees"],
            help_text="Pick any teammates who should always be added to this task.",
            team=selected_team,
        )
        _configure_rotation_count_field(
            self.fields["rotating_additional_assignee_count"],
            count=self.instance.rotating_additional_assignee_count or (1 if getattr(self.instance, "rotate_additional_assignee", False) else 0),
            help_text="Enter how many extra teammates TaskForge should rotate onto this task.",
        )
        self.fields["respond_to_text"].label = "Notify when done"
        self.fields["respond_to_text"].help_text = "Person or office to notify after the task is complete"
        self.fields["scheduled_week_of"].label = "Show week of"
        self.fields["scheduled_week_of"].help_text = "Pick any days and times this task can be worked. TaskForge will only assign teammates who have enough open time inside these windows."
        self._task_window_week_start = self._resolve_task_window_week_start()
        self.initial["scheduled_week_of"] = self._task_window_week_start
        self._initialize_task_window_fields()
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

    def day_rows(self):
        rows = []
        for prefix, day_offset in TASK_WINDOW_DAY_CONFIG:
            target_date = self._task_window_week_start + timedelta(days=day_offset)
            rows.append(
                {
                    "prefix": prefix,
                    "label": _format_short_date_label(target_date),
                    "segments": self[f"{prefix}_segments"],
                    "weekday": target_date.weekday(),
                    "override_entries": [],
                }
            )
        return rows

    def calendar_slots(self):
        return WEEKLY_CALENDAR_SLOTS

    def scheduled_task_window_map(self):
        return self.cleaned_data.get("task_schedule_blocks_by_date", {}) if hasattr(self, "cleaned_data") else {}

    def _resolve_selected_team(self):
        if self.actor and not self.actor.is_admin:
            return self.actor.team
        if self.is_bound:
            raw_team_id = self.data.get(self.add_prefix("team"))
            if raw_team_id:
                return _team_queryset().filter(pk=raw_team_id).first()
        for candidate in (self.initial.get("team"), getattr(self.instance, "team", None), getattr(self.actor, "team", None)):
            if isinstance(candidate, Team):
                return candidate
            if candidate:
                return _team_queryset().filter(pk=candidate).first()
        return None

    def _resolve_task_window_week_start(self):
        if self.is_bound:
            for field_name in ("scheduled_week_of", "scheduled_date", "due_date"):
                parsed_value = _parse_date_value(self.data.get(self.add_prefix(field_name)))
                if parsed_value:
                    return _start_of_week(parsed_value)
            return _start_of_week(timezone.localdate())

        scheduled_blocks = list(getattr(self.instance, "scheduled_blocks", []).all().order_by("work_date", "position", "start_time", "end_time", "pk")) if getattr(self.instance, "pk", None) else []
        for candidate in (
            scheduled_blocks[0].work_date if scheduled_blocks else None,
            self.initial.get("scheduled_week_of"),
            self.initial.get("scheduled_date"),
            getattr(self.instance, "scheduled_date", None),
            self.initial.get("due_date"),
        ):
            if isinstance(candidate, str):
                candidate = _parse_date_value(candidate)
            if candidate:
                return _start_of_week(candidate)
        return _start_of_week(timezone.localdate())

    def _initialize_task_window_fields(self):
        if self.is_bound:
            return

        scheduled_blocks = list(self.instance.scheduled_blocks.order_by("work_date", "position", "start_time", "end_time", "pk")) if getattr(self.instance, "pk", None) else []
        if scheduled_blocks:
            week_start = _start_of_week(scheduled_blocks[0].work_date)
            if week_start != self._task_window_week_start:
                self._task_window_week_start = week_start
                self.initial["scheduled_week_of"] = week_start
            grouped_blocks = {}
            for block in scheduled_blocks:
                grouped_blocks.setdefault(block.work_date, []).append((block.start_time, block.end_time))
            for prefix, day_offset in TASK_WINDOW_DAY_CONFIG:
                current_date = self._task_window_week_start + timedelta(days=day_offset)
                blocks = grouped_blocks.get(current_date, [])
                if blocks:
                    self.initial[f"{prefix}_segments"] = _serialize_schedule_segments(blocks)
            earliest_date = min(grouped_blocks)
            earliest_blocks = grouped_blocks[earliest_date]
            total_minutes = sum(
                _window_minutes(start_value, end_value)
                for blocks in grouped_blocks.values()
                for start_value, end_value in blocks
            )
            self.initial["scheduled_date"] = earliest_date
            self.initial["scheduled_start_time"] = earliest_blocks[0][0]
            self.initial["scheduled_end_time"] = earliest_blocks[0][1]
            self.initial["scheduled_window_segments"] = _serialize_schedule_segments(earliest_blocks)
            self.initial["scheduled_window_start"] = earliest_blocks[0][0]
            self.initial["scheduled_window_end"] = earliest_blocks[-1][1]
            self.initial["scheduled_window_hours"] = Decimal(total_minutes) / Decimal("60")
            return

        scheduled_date = self.initial.get("scheduled_date") or getattr(self.instance, "scheduled_date", None)
        if isinstance(scheduled_date, str):
            scheduled_date = _parse_date_value(scheduled_date)
        start_value = self.initial.get("scheduled_start_time") or getattr(self.instance, "scheduled_start_time", None)
        end_value = self.initial.get("scheduled_end_time") or getattr(self.instance, "scheduled_end_time", None)
        if not (scheduled_date and start_value and end_value):
            return

        week_start = _start_of_week(scheduled_date)
        if week_start != self._task_window_week_start:
            self._task_window_week_start = week_start
            self.initial["scheduled_week_of"] = week_start

        day_offset = (scheduled_date - self._task_window_week_start).days
        if not 0 <= day_offset < len(TASK_WINDOW_DAY_CONFIG):
            return

        blocks = [(start_value, end_value)]
        prefix = TASK_WINDOW_DAY_CONFIG[day_offset][0]
        self.initial[f"{prefix}_segments"] = _serialize_schedule_segments(blocks)
        self.initial["scheduled_window_segments"] = _serialize_schedule_segments(blocks)
        self.initial["scheduled_window_start"] = start_value
        self.initial["scheduled_window_end"] = end_value
        self.initial["scheduled_window_hours"] = Decimal(_window_minutes(start_value, end_value)) / Decimal("60")

    def _parse_task_window_segments(self, raw_value):
        if not raw_value:
            return []
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError:
            self.add_error("scheduled_window_segments", "The task windows have an invalid schedule payload.")
            return None
        if not isinstance(payload, list):
            self.add_error("scheduled_window_segments", "The task windows have an invalid schedule payload.")
            return None

        parsed_blocks = []
        for item in payload:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                self.add_error("scheduled_window_segments", "The task windows have an invalid schedule block.")
                return None
            start_raw, end_raw = item
            try:
                start_value = datetime.strptime(str(start_raw), "%H:%M").time()
                end_value = datetime.strptime(str(end_raw), "%H:%M").time()
            except ValueError:
                self.add_error("scheduled_window_segments", "The task windows must use 30-minute times.")
                return None
            if end_value <= start_value:
                self.add_error("scheduled_window_segments", "Each task window must end after it starts.")
                return None
            if _window_minutes(start_value, end_value) % 30 != 0:
                self.add_error("scheduled_window_segments", "The task windows must use 30-minute increments.")
                return None
            if not _schedule_block_within_workday(start_value, end_value):
                self.add_error("scheduled_window_segments", "Task windows must stay between 7:00 AM and 6:00 PM.")
                return None
            parsed_blocks.append((start_value, end_value))

        parsed_blocks.sort(key=lambda block: (block[0], block[1]))
        normalized_blocks = []
        for start_value, end_value in parsed_blocks:
            if not normalized_blocks:
                normalized_blocks.append([start_value, end_value])
                continue
            last_start, last_end = normalized_blocks[-1]
            if start_value <= last_end:
                normalized_blocks[-1][1] = max(last_end, end_value)
            else:
                normalized_blocks.append([start_value, end_value])
        return [(start_value, end_value) for start_value, end_value in normalized_blocks]

    def _selected_task_window_days(self, cleaned_data):
        selected_days = []
        week_start = cleaned_data.get("scheduled_week_of") or self._task_window_week_start
        for prefix, day_offset in TASK_WINDOW_DAY_CONFIG:
            raw_value = cleaned_data.get(f"{prefix}_segments")
            blocks = self._parse_task_window_segments(raw_value) if raw_value else []
            if blocks is None:
                return None
            if blocks:
                selected_days.append((week_start + timedelta(days=day_offset), blocks))
        return selected_days

    def _clear_task_schedule_fields(self, cleaned_data):
        cleaned_data["scheduled_date"] = None
        cleaned_data["scheduled_start_time"] = None
        cleaned_data["scheduled_end_time"] = None
        cleaned_data["scheduled_window_segments"] = ""
        cleaned_data["scheduled_window_start"] = None
        cleaned_data["scheduled_window_end"] = None
        cleaned_data["scheduled_window_hours"] = Decimal("0")
        cleaned_data["task_schedule_blocks"] = []
        cleaned_data["task_schedule_blocks_by_date"] = {}
        return cleaned_data

    def _clean_schedule_window(self, cleaned_data):
        due_date = cleaned_data.get("due_date")
        scheduled_date = cleaned_data.get("scheduled_date")
        week_value = cleaned_data.get("scheduled_week_of") or scheduled_date or due_date or timezone.localdate()
        cleaned_data["scheduled_week_of"] = _start_of_week(week_value)

        selected_days = self._selected_task_window_days(cleaned_data)
        if selected_days is None:
            return cleaned_data

        if not selected_days:
            start_value = cleaned_data.get("scheduled_start_time")
            end_value = cleaned_data.get("scheduled_end_time")
            segments_value = cleaned_data.get("scheduled_window_segments")
            blocks = []
            if segments_value:
                blocks = self._parse_task_window_segments(segments_value)
                if blocks is None:
                    return cleaned_data
            elif start_value and end_value:
                if not _schedule_block_within_workday(start_value, end_value):
                    self.add_error("scheduled_window_segments", "Task windows must stay between 7:00 AM and 6:00 PM.")
                    return cleaned_data
                blocks = [(start_value, end_value)]

            scheduled_date = cleaned_data.get("scheduled_date") or cleaned_data.get("due_date")
            if blocks and scheduled_date:
                selected_days = [(scheduled_date, blocks)]
            else:
                return self._clear_task_schedule_fields(cleaned_data)

        task_schedule_blocks = []
        task_schedule_blocks_by_date = {}
        total_minutes = 0
        earliest_date = None
        latest_date = None
        earliest_blocks = []

        for work_date, blocks in selected_days:
            task_schedule_blocks_by_date[work_date] = blocks
            if earliest_date is None or work_date < earliest_date:
                earliest_date = work_date
                earliest_blocks = blocks
            if latest_date is None or work_date > latest_date:
                latest_date = work_date
            for position, (start_value, end_value) in enumerate(blocks, start=1):
                task_schedule_blocks.append(
                    {
                        "work_date": work_date,
                        "start_time": start_value,
                        "end_time": end_value,
                        "position": position,
                    }
                )
                total_minutes += _window_minutes(start_value, end_value)

        if not task_schedule_blocks:
            return self._clear_task_schedule_fields(cleaned_data)

        cleaned_data["task_schedule_blocks"] = task_schedule_blocks
        cleaned_data["task_schedule_blocks_by_date"] = task_schedule_blocks_by_date
        cleaned_data["scheduled_date"] = earliest_date
        cleaned_data["scheduled_start_time"] = earliest_blocks[0][0]
        cleaned_data["scheduled_end_time"] = earliest_blocks[0][1]
        cleaned_data["scheduled_window_segments"] = _serialize_schedule_segments(earliest_blocks)
        cleaned_data["scheduled_window_start"] = earliest_blocks[0][0]
        cleaned_data["scheduled_window_end"] = earliest_blocks[-1][1]
        cleaned_data["scheduled_window_hours"] = Decimal(total_minutes) / Decimal("60")

        if not cleaned_data.get("due_date"):
            cleaned_data["due_date"] = latest_date

        estimated_minutes = cleaned_data.get("estimated_minutes")
        if estimated_minutes and estimated_minutes > total_minutes:
            self.add_error("estimated_minutes", "The time estimate is longer than the total allowed task windows.")

        if cleaned_data.get("recurring_task") and len(task_schedule_blocks) > 1:
            self.add_error("scheduled_window_segments", "Repeating tasks can only use one scheduled work window right now.")

        return cleaned_data

    def _validate_worker_schedule_assignments(self, cleaned_data):
        task_schedule_blocks = cleaned_data.get("task_schedule_blocks_by_date") or {}
        if not task_schedule_blocks:
            return cleaned_data

        exclude_task_id = self.instance.pk if getattr(self.instance, "pk", None) else None
        assigned_to = cleaned_data.get("assigned_to")
        additional_assignees = cleaned_data.get("additional_assignees")
        due_date = cleaned_data.get("due_date")
        estimated_minutes = cleaned_data.get("estimated_minutes")

        if assigned_to and assigned_to.role in UserRole.worker_roles() and not TaskAssignmentService.worker_can_take_task(
            assigned_to,
            due_date=due_date,
            estimated_minutes=estimated_minutes,
            task_window_blocks=task_schedule_blocks,
            exclude_task_id=exclude_task_id,
        ):
            self.add_error("assigned_to", f"{assigned_to.display_label} does not have enough scheduled availability during those task windows.")

        unavailable_additional = []
        for teammate in additional_assignees or []:
            if not TaskAssignmentService.worker_can_take_task(
                teammate,
                due_date=due_date,
                estimated_minutes=estimated_minutes,
                task_window_blocks=task_schedule_blocks,
                exclude_task_id=exclude_task_id,
            ):
                unavailable_additional.append(teammate.display_label)
        if unavailable_additional:
            self.add_error("additional_assignees", "Unavailable for those task windows: " + ", ".join(unavailable_additional))
        return cleaned_data

    def clean(self):
        cleaned_data = super().clean()
        if self.actor and not self.actor.is_admin:
            cleaned_data["team"] = self.actor.team
        team = cleaned_data.get("team")
        if not team:
            self.add_error("team", "Choose which team owns this task.")
        assigned_to = cleaned_data.get("assigned_to")
        additional_assignees = cleaned_data.get("additional_assignees")
        if assigned_to and team and assigned_to.team_id != team.id:
            self.add_error("assigned_to", f"{assigned_to.display_label} is not on that team.")
        if additional_assignees:
            mismatched_teammates = [user.display_label for user in additional_assignees if team and user.team_id != team.id]
            if mismatched_teammates:
                self.add_error("additional_assignees", "Not on that team: " + ", ".join(mismatched_teammates))
        if assigned_to and additional_assignees:
            cleaned_data["additional_assignees"] = additional_assignees.exclude(pk=assigned_to.pk)
        cleaned_data["rotating_additional_assignee_count"] = cleaned_data.get("rotating_additional_assignee_count") or 0

        cleaned_data = self._clean_schedule_window(cleaned_data)
        cleaned_data = self._validate_worker_schedule_assignments(cleaned_data)

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

    def save_task_schedule(self, task):
        task.scheduled_blocks.all().delete()
        for block in self.cleaned_data.get("task_schedule_blocks", []):
            TaskScheduleBlock.objects.create(
                task=task,
                work_date=block["work_date"],
                start_time=block["start_time"],
                end_time=block["end_time"],
                position=block["position"],
            )
        return task


class TaskManualForm(TaskForm):
    class Meta(TaskForm.Meta):
        fields = [
            "team",
            "title",
            "description",
            "priority",
            "status",
            "due_date",
            "scheduled_date",
            "scheduled_start_time",
            "scheduled_end_time",
            "respond_to_text",
            "estimated_minutes",
            "assigned_to",
            "additional_assignees",
            "rotating_additional_assignee_count",
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
        fields = ["username", "first_name", "last_name", "email", "team", "assignable_to_tasks"]

    def __init__(self, *args, actor=None, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        self.fields["email"].required = False
        self.fields["email"].help_text = ""
        if not getattr(self.instance, "pk", None):
            self.fields["email"].help_text = "Optional. Leave blank if you do not want to store an email address for this supervisor."
        self.fields["team"].queryset = _team_queryset()
        self.fields["team"].required = True
        self.fields["team"].label = "Team"
        if actor and not actor.is_admin:
            self.fields["team"].widget = forms.HiddenInput()
            self.fields["team"].required = False
            self.initial["team"] = actor.team_id
        else:
            self.fields["team"].help_text = "Choose which team this supervisor belongs to."
        self.fields["assignable_to_tasks"].label = "Allow fallback tasks to be assigned to this supervisor"
        self.fields["assignable_to_tasks"].help_text = ""

    def clean_team(self):
        if self.actor and not self.actor.is_admin:
            return self.actor.team
        team = self.cleaned_data.get("team")
        if not team:
            raise forms.ValidationError("Choose a team for this supervisor.")
        return team


class TeamForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Team
        fields = ["name", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


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


class TaskAttachmentForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TaskAttachment
        fields = ["file"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["file"].help_text = "Allowed file types: PDF, images, spreadsheets, text files, and Office documents. Each file must be 10 MB or smaller."

    def clean_file(self):
        return _validate_uploaded_file(self.cleaned_data["file"])


class RecurringTaskTemplateForm(StyledFormMixin, forms.ModelForm):
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    next_run_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    day_of_week = forms.TypedChoiceField(
        required=False,
        choices=WEEKDAY_SELECT_CHOICES,
        coerce=lambda value: int(value) if value not in ("", None) else None,
        empty_value=None,
    )
    scheduled_start_time = HalfHourTimeField()
    scheduled_end_time = HalfHourTimeField()

    class Meta:
        model = RecurringTaskTemplate
        fields = [
            "team",
            "title",
            "description",
            "priority",
            "estimated_minutes",
            "scheduled_start_time",
            "scheduled_end_time",
            "assign_to",
            "additional_assignees",
            "rotating_additional_assignee_count",
            "recurrence_pattern",
            "recurrence_interval",
            "day_of_week",
            "day_of_month",
            "start_date",
            "next_run_date",
            "active",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, actor=None, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        selected_team = self._resolve_selected_team()
        self.fields["team"].queryset = _team_queryset()
        self.fields["team"].required = True
        self.fields["team"].label = "Team"
        if actor and not actor.is_admin:
            self.fields["team"].widget = forms.HiddenInput()
            self.fields["team"].required = False
            self.initial["team"] = actor.team_id
            selected_team = actor.team
        else:
            self.fields["team"].help_text = "Choose which team owns this recurring task."
            if selected_team:
                self.initial.setdefault("team", selected_team.pk)
        self.fields["description"].label = "Task details"
        self.fields["description"].help_text = "Describe the work that should happen each time this task repeats."
        self.fields["estimated_minutes"].label = "Time estimate"
        self.fields["scheduled_start_time"].label = "Scheduled start time"
        self.fields["scheduled_start_time"].help_text = "Optional. Generated tasks will use this start time on each run."
        self.fields["scheduled_end_time"].label = "Scheduled end time"
        self.fields["scheduled_end_time"].help_text = "Optional. Generated tasks will use this end time on each run."
        self.fields["assign_to"].label = "Assign to"
        self.fields["assign_to"].help_text = "Choose the main teammate for this recurring task. Leave blank to rotate the main assignee automatically."
        self.fields["assign_to"].queryset = _worker_user_queryset(team=selected_team)
        self.fields["assign_to"].label_from_instance = _user_choice_label
        _configure_additional_assignees_field(
            self.fields["additional_assignees"],
            help_text="Pick any teammates who should always join each run of this recurring task.",
            team=selected_team,
        )
        _configure_rotation_count_field(
            self.fields["rotating_additional_assignee_count"],
            count=self.instance.rotating_additional_assignee_count or (1 if getattr(self.instance, "rotate_additional_assignee", False) else 0),
            help_text="Enter how many extra teammates TaskForge should rotate onto each run of this recurring task.",
        )
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

    def _resolve_selected_team(self):
        if self.actor and not self.actor.is_admin:
            return self.actor.team
        if self.is_bound:
            raw_team_id = self.data.get(self.add_prefix("team"))
            if raw_team_id:
                return _team_queryset().filter(pk=raw_team_id).first()
        for candidate in (self.initial.get("team"), getattr(self.instance, "team", None), getattr(self.actor, "team", None)):
            if isinstance(candidate, Team):
                return candidate
            if candidate:
                return _team_queryset().filter(pk=candidate).first()
        return None

    def clean(self):
        cleaned_data = super().clean()
        if self.actor and not self.actor.is_admin:
            cleaned_data["team"] = self.actor.team
        team = cleaned_data.get("team")
        if not team:
            self.add_error("team", "Choose which team owns this recurring task.")
        assign_to = cleaned_data.get("assign_to")
        additional_assignees = cleaned_data.get("additional_assignees")
        if assign_to and team and assign_to.team_id != team.id:
            self.add_error("assign_to", f"{assign_to.display_label} is not on that team.")
        if additional_assignees:
            mismatched_teammates = [user.display_label for user in additional_assignees if team and user.team_id != team.id]
            if mismatched_teammates:
                self.add_error("additional_assignees", "Not on that team: " + ", ".join(mismatched_teammates))
        if assign_to and additional_assignees:
            cleaned_data["additional_assignees"] = additional_assignees.exclude(pk=assign_to.pk)
        cleaned_data["rotating_additional_assignee_count"] = cleaned_data.get("rotating_additional_assignee_count") or 0

        recurrence_pattern = cleaned_data.get("recurrence_pattern")
        day_of_week = cleaned_data.get("day_of_week")
        day_of_month = cleaned_data.get("day_of_month")
        recurrence_interval = cleaned_data.get("recurrence_interval")
        start_value = cleaned_data.get("scheduled_start_time")
        end_value = cleaned_data.get("scheduled_end_time")
        next_run_date = cleaned_data.get("next_run_date")

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

        if start_value or end_value:
            if not start_value:
                self.add_error("scheduled_start_time", "Choose a recurring start time.")
            if not end_value:
                self.add_error("scheduled_end_time", "Choose a recurring end time.")
            if start_value and end_value and end_value <= start_value:
                self.add_error("scheduled_end_time", "End time must be after the start time.")
            if start_value and end_value:
                if not _schedule_block_within_workday(start_value, end_value):
                    self.add_error("scheduled_end_time", "Recurring work windows must stay between 7:00 AM and 6:00 PM.")
                else:
                    estimated_minutes = cleaned_data.get("estimated_minutes")
                    if estimated_minutes and estimated_minutes > _window_minutes(start_value, end_value):
                        self.add_error("estimated_minutes", "The time estimate is longer than the recurring work window.")
                    unavailable = []
                    for teammate in filter(None, [assign_to, *(additional_assignees or [])]):
                        if not TaskAssignmentService.user_is_available_for_window(
                            teammate,
                            scheduled_date=next_run_date,
                            scheduled_start_time=start_value,
                            scheduled_end_time=end_value,
                        ):
                            unavailable.append(teammate.display_label)
                    if assign_to and assign_to.display_label in unavailable:
                        self.add_error("assign_to", f"{assign_to.display_label} is not scheduled during the next recurring work window.")
                        unavailable = [label for label in unavailable if label != assign_to.display_label]
                    if unavailable:
                        self.add_error("additional_assignees", "Unavailable for the next recurring work window: " + ", ".join(unavailable))

        return cleaned_data









