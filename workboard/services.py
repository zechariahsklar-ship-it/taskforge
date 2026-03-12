from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import json
import os
from urllib import error, request

from django.db.models import Max, Q, Sum
from django.utils import timezone

from .models import Priority, StudentAvailability, StudentAvailabilityOverride, StudentWorkerProfile, TaskStatus, User, UserRole


@dataclass
class ParsedTaskData:
    raw_message: str
    title: str
    description: str
    priority: str
    due_date: str | None
    raw_due_text: str
    waiting_person: str
    respond_to_text: str
    estimated_minutes: int | None
    assigned_to_id: int | None
    assignment_summary: str
    assignment_rationale: list[str]
    checklist_items: list[str]
    parser_confidence: str
    parser_warnings: list[str]

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
            "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
            "model": os.getenv("OPENAI_TASK_PARSER_MODEL", "gpt-5-mini"),
            "endpoint": "https://api.openai.com/v1/chat/completions",
        }

    @staticmethod
    def parse_request(raw_message: str, attachments=None, fallback_supervisor=None) -> ParsedTaskData:
        settings = TaskParsingService.parser_settings()
        attachment_names = [getattr(item, "original_name", getattr(item, "name", "attachment")) for item in (attachments or [])]

        if not settings["use_mock_parser"] and settings["openai_api_key"]:
            try:
                parsed = TaskParsingService._parse_with_openai(raw_message, attachment_names, settings)
            except Exception as exc:
                parsed = TaskParsingService._parse_with_mock(raw_message)
                parsed.assignment_rationale.append(f"OpenAI parser failed and mock fallback was used: {exc}")
                parsed.parser_confidence = "low"
                parsed.parser_warnings.append("OpenAI parsing failed, so the app used the mock parser instead.")
            else:
                parsed.assignment_rationale.append(f"Parser mode: openai using model setting `{settings['model']}`.")
        else:
            parsed = TaskParsingService._parse_with_mock(raw_message)
            if not settings["use_mock_parser"] and not settings["openai_api_key"]:
                parsed.assignment_rationale.append("Real parser mode is enabled, but OPENAI_API_KEY is not configured. Falling back to mock parsing behavior.")
                parsed.parser_warnings.append("OPENAI_API_KEY is missing, so the app used the mock parser.")
            parsed.assignment_rationale.append(f"Parser mode: mock using model setting `{settings['model']}`.")

        if attachment_names:
            parsed.assignment_rationale.append(
                f"{len(attachment_names)} attachment(s) were preserved for the workflow. The current live parser uses the message text and attachment names; binary image analysis can be added next."
            )
            parsed.parser_warnings.append("Attachments were stored, but the parser currently uses message text and attachment names only.")
        if not parsed.due_date:
            parsed.parser_warnings.append("No exact due date was extracted. Review the due date field before saving.")
        if not parsed.respond_to_text and any(word in raw_message.lower() for word in ["reply", "respond", "email"]):
            parsed.parser_warnings.append("A response action may be needed, but no explicit respond-to text was extracted.")
        parsed.parser_confidence = TaskParsingService._calculate_parser_confidence(parsed)

        due_date_value = TaskParsingService._parse_due_date(parsed.due_date)
        suggested_user, assignment_summary, assignment_rationale = TaskAssignmentService.suggest_assignee(
            due_date=due_date_value,
            estimated_minutes=parsed.estimated_minutes,
            fallback_supervisor=fallback_supervisor,
        )
        parsed.assigned_to_id = suggested_user.id if suggested_user else None
        parsed.assignment_summary = assignment_summary
        parsed.assignment_rationale = assignment_rationale + parsed.assignment_rationale
        return parsed

    @staticmethod
    def _parse_with_mock(raw_message: str) -> ParsedTaskData:
        first_line = raw_message.strip().splitlines()[0] if raw_message.strip() else "New task request"
        lowered = raw_message.lower()
        priority = Priority.URGENT if "urgent" in lowered else Priority.HIGH if "asap" in lowered else Priority.MEDIUM
        estimated_minutes = 60 if len(raw_message) > 240 else 30
        raw_due_text = "Needs review"
        due_date = None
        if "tomorrow" in lowered:
            raw_due_text = "Tomorrow"
            due_date = str(timezone.localdate() + timedelta(days=1))
        elif "friday" in lowered:
            raw_due_text = "Friday"
        return ParsedTaskData(
            raw_message=raw_message,
            title=first_line[:255] or "New task request",
            description=raw_message.strip()[:2000],
            priority=priority,
            due_date=due_date,
            raw_due_text=raw_due_text,
            waiting_person="",
            respond_to_text="",
            estimated_minutes=estimated_minutes,
            assigned_to_id=None,
            assignment_summary="",
            assignment_rationale=[],
            checklist_items=TaskParsingService._build_checklist_items(raw_message),
            parser_confidence="medium",
            parser_warnings=[],
        )

    @staticmethod
    def _parse_with_openai(raw_message: str, attachment_names: list[str], settings: dict) -> ParsedTaskData:
        today = str(timezone.localdate())
        prompt = (
            "Extract a structured internal task from the supervisor message. "
            "Return strict JSON matching the schema. Use ISO date format YYYY-MM-DD when the due date can be inferred; otherwise return null for due_date. "
            "Interpret relative dates carefully from today's date. For example, if today is Thursday and the message says next Friday, use the Friday of the following week, not tomorrow. "
            f"Today is {today}. Attachment names: {', '.join(attachment_names) if attachment_names else 'none'}."
        )
        payload = {
            "model": settings["model"],
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": raw_message},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "task_extraction",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "priority": {"type": "string", "enum": ["urgent", "high", "medium", "low"]},
                            "due_date": {"type": ["string", "null"]},
                            "raw_due_text": {"type": "string"},
                            "waiting_person": {"type": "string"},
                            "respond_to_text": {"type": "string"},
                            "estimated_minutes": {"type": ["integer", "null"]},
                            "checklist_items": {
                                "type": "array",
                                "items": {"type": "string"}
                            }
                        },
                        "required": [
                            "title",
                            "description",
                            "priority",
                            "due_date",
                            "raw_due_text",
                            "waiting_person",
                            "respond_to_text",
                            "estimated_minutes",
                            "checklist_items"
                        ],
                        "additionalProperties": False
                    }
                }
            },
        }
        req = request.Request(
            settings["endpoint"],
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings['openai_api_key']}",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=90) as response:
                body = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Network error: {exc.reason}") from exc

        choice = body["choices"][0]
        message = choice["message"]
        if message.get("refusal"):
            raise RuntimeError(f"Model refusal: {message['refusal']}")
        content = message.get("content", "")
        if not isinstance(content, str):
            raise RuntimeError("Unexpected OpenAI response format.")
        parsed = json.loads(content)

        title = (parsed.get("title") or "New task request")[:255]
        description = (parsed.get("description") or raw_message).strip()[:2000]
        priority = parsed.get("priority") if parsed.get("priority") in {Priority.URGENT, Priority.HIGH, Priority.MEDIUM, Priority.LOW} else Priority.MEDIUM
        due_date = parsed.get("due_date")
        due_date = due_date if TaskParsingService._parse_due_date(due_date) else None
        checklist_items = [item.strip() for item in parsed.get("checklist_items", []) if isinstance(item, str) and item.strip()]
        if not checklist_items:
            checklist_items = TaskParsingService._build_checklist_items(raw_message)

        return ParsedTaskData(
            raw_message=raw_message,
            title=title,
            description=description,
            priority=priority,
            due_date=due_date,
            raw_due_text=(parsed.get("raw_due_text") or "Needs review")[:255],
            waiting_person=(parsed.get("waiting_person") or "")[:255],
            respond_to_text=(parsed.get("respond_to_text") or "")[:255],
            estimated_minutes=TaskParsingService._normalize_estimated_minutes(parsed.get("estimated_minutes")),
            assigned_to_id=None,
            assignment_summary="",
            assignment_rationale=[],
            checklist_items=TaskParsingService._dedupe_checklist_items(checklist_items),
            parser_confidence="high",
            parser_warnings=[],
        )

    @staticmethod
    def _normalize_estimated_minutes(value):
        if value in (None, ""):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return max(number, 0)

    @staticmethod
    def _parse_due_date(value):
        if not value:
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None

    @staticmethod
    def _dedupe_checklist_items(items: list[str]) -> list[str]:
        deduped = []
        for item in items:
            if item not in deduped:
                deduped.append(item)
        return deduped

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
        return TaskParsingService._dedupe_checklist_items(items)

    @staticmethod
    def _calculate_parser_confidence(parsed: ParsedTaskData) -> str:
        score = 0
        if parsed.title:
            score += 1
        if parsed.description:
            score += 1
        if parsed.priority:
            score += 1
        if parsed.due_date:
            score += 1
        if parsed.estimated_minutes:
            score += 1
        if parsed.checklist_items:
            score += 1
        if len(parsed.parser_warnings) >= 3:
            return "low"
        if score >= 5 and len(parsed.parser_warnings) <= 1:
            return "high"
        return "medium"
