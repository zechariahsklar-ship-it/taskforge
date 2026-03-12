from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import os

from django.db.models import Max, Q, Sum
from django.utils import timezone

from .models import Priority, StudentAvailability, StudentAvailabilityOverride, StudentWorkerProfile, TaskStatus, User, UserRole


@dataclass
class ParsedTaskData:
    raw_message: str
    title: str
    description: str
    priority: str
    raw_due_text: str
    waiting_person: str
    respond_to_text: str
    estimated_minutes: int | None
    assigned_to_id: int | None
    assignment_summary: str
    assignment_rationale: list[str]
    checklist_items: list[str]

    def to_dict(self):
        return asdict(self)


class TaskAssignmentService:
    @staticmethod
    def suggest_assignee(*, due_date, estimated_minutes, fallback_supervisor=None):
        supervisors = User.objects.filter(role=UserRole.SUPERVISOR).order_by("username")
        students = StudentWorkerProfile.objects.filter(active_status=True, user__role=UserRole.STUDENT_WORKER).select_related("user")

        viable = []
        for profile in students:
            capacity = TaskAssignmentService._remaining_capacity_minutes(profile, due_date)
            open_tasks = profile.user.assigned_tasks.exclude(status=TaskStatus.DONE).count()
            last_assigned_at = (
                profile.user.assigned_tasks.exclude(status=TaskStatus.DONE).aggregate(last_assigned=Max("created_at"))["last_assigned"]
            )
            if estimated_minutes is None or capacity >= estimated_minutes:
                viable.append((profile, capacity, open_tasks, last_assigned_at))

        viable.sort(
            key=lambda item: (
                item[3] is not None,
                item[3] or timezone.make_aware(datetime(2000, 1, 1)),
                item[2],
                -item[1],
            )
        )
        if viable:
            profile, capacity, _, _ = viable[0]
            summary = f"Suggested student: {profile.display_name} based on current availability and assignment rotation. Remaining capacity before due date: {int(capacity)} minutes."
            rationale = [
                f"Recommended assignee: {profile.display_name}",
                f"Estimated open capacity before due date: {int(capacity)} minutes",
                "Selection favors workers with enough capacity and lighter recent assignment rotation.",
            ]
            return profile.user, summary, rationale

        supervisor = fallback_supervisor if fallback_supervisor and fallback_supervisor.role == UserRole.SUPERVISOR else supervisors.first()
        if supervisor:
            return supervisor, "No student has enough available capacity before the due date, so this task should be assigned to a supervisor.", [
                f"Recommended assignee: {supervisor.display_label}",
                "No student currently has enough available hours before the due date window.",
                "Fallback rule assigned the task to a supervisor.",
            ]
        return None, "No eligible assignee found.", ["No eligible assignee found."]

    @staticmethod
    def _remaining_capacity_minutes(profile, due_date):
        start_date = timezone.localdate()
        end_date = due_date or (start_date + timedelta(days=7))
        if end_date < start_date:
            end_date = start_date

        weekly = {item.weekday: float(item.hours_available) for item in StudentAvailability.objects.filter(profile=profile)}
        overrides = {
            item.override_date: float(item.hours_available)
            for item in StudentAvailabilityOverride.objects.filter(profile=profile, override_date__range=(start_date, end_date))
        }

        total_minutes = 0
        cursor = start_date
        while cursor <= end_date:
            hours = overrides.get(cursor, weekly.get(cursor.weekday(), 0.0))
            total_minutes += int(hours * 60)
            cursor += timedelta(days=1)

        reserved_minutes = (
            profile.user.assigned_tasks.exclude(status=TaskStatus.DONE)
            .filter(Q(due_date__isnull=True) | Q(due_date__lte=end_date))
            .aggregate(total=Sum("estimated_minutes"))["total"]
            or 0
        )
        return max(total_minutes - reserved_minutes, 0)


class TaskParsingService:
    @staticmethod
    def parser_settings() -> dict:
        return {
            "use_mock_parser": os.getenv("USE_MOCK_TASK_PARSER", "True").lower() == "true",
            "openai_api_key_configured": bool(os.getenv("OPENAI_API_KEY")),
            "model": os.getenv("OPENAI_TASK_PARSER_MODEL", "gpt-5-mini"),
        }

    @staticmethod
    def parse_request(raw_message: str, attachments=None, fallback_supervisor=None) -> ParsedTaskData:
        parser_settings = TaskParsingService.parser_settings()
        first_line = raw_message.strip().splitlines()[0] if raw_message.strip() else "New task request"
        lowered = raw_message.lower()
        priority = Priority.URGENT if "urgent" in lowered else Priority.HIGH if "asap" in lowered else Priority.MEDIUM
        estimated_minutes = 60 if len(raw_message) > 240 else 30
        raw_due_text = "Needs review"
        if "tomorrow" in lowered:
            raw_due_text = "Tomorrow"
        elif "friday" in lowered:
            raw_due_text = "Friday"

        suggested_user, assignment_summary, assignment_rationale = TaskAssignmentService.suggest_assignee(
            due_date=None,
            estimated_minutes=estimated_minutes,
            fallback_supervisor=fallback_supervisor,
        )

        attachment_count = len(attachments or [])
        if attachment_count:
            assignment_summary = f"{assignment_summary} {attachment_count} attachment(s) included for the future parser."
            assignment_rationale.append(f"{attachment_count} attachment(s) were preserved for future API parsing.")
        if not parser_settings["use_mock_parser"] and not parser_settings["openai_api_key_configured"]:
            assignment_rationale.append("Real parser mode is enabled, but OPENAI_API_KEY is not configured. Falling back to mock parsing behavior.")
        else:
            assignment_rationale.append(f"Parser mode: {'mock' if parser_settings['use_mock_parser'] else 'openai'} using model setting `{parser_settings['model']}`.")

        checklist_items = TaskParsingService._build_checklist_items(raw_message)

        return ParsedTaskData(
            raw_message=raw_message,
            title=first_line[:255] or "New task request",
            description=raw_message.strip()[:2000],
            priority=priority,
            raw_due_text=raw_due_text,
            waiting_person="",
            respond_to_text="",
            estimated_minutes=estimated_minutes,
            assigned_to_id=suggested_user.id if suggested_user else None,
            assignment_summary=assignment_summary,
            assignment_rationale=assignment_rationale,
            checklist_items=checklist_items,
        )

    @staticmethod
    def _build_checklist_items(raw_message: str) -> list[str]:
        lines = [line.strip(" -*\t") for line in raw_message.splitlines() if line.strip()]
        items = []
        if lines:
            items.append("Review original request details")
        if len(lines) > 1:
            items.append(f"Complete core task work: {lines[1][:120]}")
        else:
            items.append("Complete core task work")
        if any(word in raw_message.lower() for word in ["reply", "respond", "email"]):
            items.append("Send response or follow-up communication")
        items.append("Confirm task completion and update status")
        deduped = []
        for item in items:
            if item not in deduped:
                deduped.append(item)
        return deduped
