from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import json
import os
from urllib import error, request

from django.db.models import Max, Q, Sum
from django.utils import timezone

from .models import Priority, StudentAvailability, StudentAvailabilityOverride, StudentWorkerProfile, TaskEstimateFeedback, TaskStatus, User, UserRole


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
    due_date_source: str
    due_date_original: str | None
    due_date_inferred: bool
    due_date_defaulted: bool
    due_date_weekend_adjusted: bool
    due_date_confidence: str
    due_date_warning: str
    priority_confidence: str

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


class TaskEstimateFeedbackService:
    @staticmethod
    def record_feedback(*, task, original_estimated_minutes, corrected_estimated_minutes, corrected_by=None, source=""):
        if corrected_estimated_minutes in (None, ""):
            return None
        try:
            corrected_value = int(corrected_estimated_minutes)
        except (TypeError, ValueError):
            return None
        original_value = None
        if original_estimated_minutes not in (None, ""):
            try:
                original_value = int(original_estimated_minutes)
            except (TypeError, ValueError):
                original_value = None
        if original_value == corrected_value:
            return None
        return TaskEstimateFeedback.objects.create(
            task=task,
            raw_message=getattr(task, "raw_message", "") or "",
            task_title=getattr(task, "title", "") or "",
            original_estimated_minutes=original_value,
            corrected_estimated_minutes=corrected_value,
            corrected_by=corrected_by,
            source=source[:64],
        )


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
        confidence_override = None

        if not settings["use_mock_parser"] and settings["openai_api_key"]:
            try:
                parsed = TaskParsingService._parse_with_openai(raw_message, attachment_names, settings)
            except Exception as exc:
                parsed = TaskParsingService._parse_with_mock(raw_message)
                parsed.assignment_rationale.append(f"OpenAI parser failed and mock fallback was used: {exc}")
                parsed.parser_warnings.append("OpenAI parsing failed, so the app used the mock parser instead.")
                confidence_override = "low"
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
        parsed = TaskParsingService._apply_priority_and_due_date_fallbacks(parsed)
        parsed = TaskParsingService._apply_due_date_rules(parsed)
        if not parsed.respond_to_text and any(word in raw_message.lower() for word in ["reply", "respond", "email"]):
            parsed.parser_warnings.append("A response action may be needed, but no explicit respond-to text was extracted.")
        parsed.parser_confidence = TaskParsingService._calculate_parser_confidence(parsed)
        if confidence_override:
            parsed.parser_confidence = confidence_override

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
        priority_confidence = TaskParsingService._priority_confidence(raw_message, priority)
        estimated_minutes = 60 if len(raw_message) > 240 else 30
        raw_due_text = "Needs review"
        due_date = None
        if "tomorrow" in lowered:
            raw_due_text = "Tomorrow"
            due_date = str(timezone.localdate() + timedelta(days=1))
        elif "friday" in lowered:
            raw_due_text = "Friday"
        due_date_source, due_date_inferred, due_date_confidence = TaskParsingService._due_date_metadata(raw_due_text, due_date)
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
            due_date_source=due_date_source,
            due_date_original=due_date,
            due_date_inferred=due_date_inferred,
            due_date_defaulted=False,
            due_date_weekend_adjusted=False,
            due_date_confidence=due_date_confidence,
            due_date_warning="",
            priority_confidence=priority_confidence,
        )

    @staticmethod
    def _build_estimate_feedback_examples(limit: int = 5) -> str:
        feedback_items = list(TaskEstimateFeedback.objects.exclude(raw_message="").order_by("-created_at")[:limit])
        if not feedback_items:
            return ""
        lines = ["Use these recent estimate corrections as examples when judging estimated_minutes:"]
        for item in feedback_items:
            original = item.original_estimated_minutes if item.original_estimated_minutes is not None else "none"
            lines.append(
                f"- Title: {item.task_title or 'Untitled'} | Original estimate: {original} | Corrected estimate: {item.corrected_estimated_minutes} | Request excerpt: {item.raw_message[:180]}"
            )
        return " ".join(lines)

    @staticmethod
    def _parse_with_openai(raw_message: str, attachment_names: list[str], settings: dict) -> ParsedTaskData:
        today = str(timezone.localdate())
        feedback_examples = TaskParsingService._build_estimate_feedback_examples()
        prompt = (
            "Extract a structured internal task from the supervisor message. "
            "Return strict JSON matching the schema. Use ISO date format YYYY-MM-DD when the due date can be inferred; otherwise return null for due_date. "
            "Interpret relative dates carefully from today's date. For example, if today is Thursday and the message says next Friday, use the Friday of the following week, not tomorrow. "
            f"Today is {today}. Attachment names: {', '.join(attachment_names) if attachment_names else 'none'}. "
            f"{feedback_examples}"
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
        priority = parsed.get("priority") if parsed.get("priority") in {Priority.URGENT, Priority.HIGH, Priority.MEDIUM, Priority.LOW} else ""
        due_date = parsed.get("due_date")
        due_date = due_date if TaskParsingService._parse_due_date(due_date) else None
        checklist_items = [item.strip() for item in parsed.get("checklist_items", []) if isinstance(item, str) and item.strip()]
        if not checklist_items:
            checklist_items = TaskParsingService._build_checklist_items(raw_message)
        raw_due_text = (parsed.get("raw_due_text") or "Needs review")[:255]
        due_date_source, due_date_inferred, due_date_confidence = TaskParsingService._due_date_metadata(raw_due_text, due_date)
        priority_confidence = TaskParsingService._priority_confidence(raw_message, priority)

        return ParsedTaskData(
            raw_message=raw_message,
            title=title,
            description=description,
            priority=priority,
            due_date=due_date,
            raw_due_text=raw_due_text,
            waiting_person=(parsed.get("waiting_person") or "")[:255],
            respond_to_text=(parsed.get("respond_to_text") or "")[:255],
            estimated_minutes=TaskParsingService._normalize_estimated_minutes(parsed.get("estimated_minutes")),
            assigned_to_id=None,
            assignment_summary="",
            assignment_rationale=[],
            checklist_items=TaskParsingService._dedupe_checklist_items(checklist_items),
            parser_confidence="high",
            parser_warnings=[],
            due_date_source=due_date_source,
            due_date_original=due_date,
            due_date_inferred=due_date_inferred,
            due_date_defaulted=False,
            due_date_weekend_adjusted=False,
            due_date_confidence=due_date_confidence,
            due_date_warning="",
            priority_confidence=priority_confidence,
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
    def _priority_confidence(raw_message: str, priority: str) -> str:
        if not priority:
            return "low"
        lowered = raw_message.lower()
        explicit_priority_cues = {
            Priority.URGENT: ["urgent", "immediately", "right away"],
            Priority.HIGH: ["asap", "high priority", "soon"],
            Priority.MEDIUM: ["medium priority"],
            Priority.LOW: ["low priority", "whenever", "no rush"],
        }
        if any(cue in lowered for cue in explicit_priority_cues.get(priority, [])):
            return "high"
        return "medium"

    @staticmethod
    def _classify_due_date_phrase(raw_due_text: str) -> str:
        phrase = (raw_due_text or "").strip().lower()
        if not phrase or phrase in {"needs review", "not set"}:
            return "missing"
        month_names = {
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
            "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
        }
        relative_markers = {
            "today", "tomorrow", "tonight", "next", "this", "upcoming", "by end of day", "eod", "eow", "end of week",
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        }
        if any(token in phrase for token in relative_markers):
            return "relative"
        if any(month in phrase for month in month_names):
            return "absolute"
        if len(phrase) == 10 and phrase[4] == "-" and phrase[7] == "-":
            return "absolute"
        if "/" in phrase and any(char.isdigit() for char in phrase):
            return "absolute"
        if any(char.isdigit() for char in phrase) and any(suffix in phrase for suffix in ["st", "nd", "rd", "th"]):
            return "absolute"
        return "unknown"

    @staticmethod
    def _due_date_metadata(raw_due_text: str, due_date: str | None) -> tuple[str, bool, str]:
        parsed_due_date = TaskParsingService._parse_due_date(due_date)
        phrase_type = TaskParsingService._classify_due_date_phrase(raw_due_text)
        if not parsed_due_date:
            return "unconfirmed", False, "low"
        if phrase_type == "absolute":
            return "parsed", False, "high"
        if phrase_type == "relative":
            return "inferred_from_phrase", True, "medium"
        if phrase_type == "missing":
            return "parsed", False, "medium"
        return "parsed", True, "medium"


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
        if parsed.priority_confidence == "high":
            score += 1
        elif parsed.priority:
            score += 0.5
        if parsed.due_date_confidence == "high":
            score += 1
        elif parsed.due_date_confidence == "medium":
            score += 0.5
        if parsed.estimated_minutes:
            score += 1
        if parsed.checklist_items:
            score += 1
        if parsed.due_date_defaulted:
            score -= 1
        if parsed.priority_confidence == "low":
            score -= 0.5
        if len(parsed.parser_warnings) >= 3:
            return "low"
        if score >= 4.5 and len(parsed.parser_warnings) <= 1:
            return "high"
        if score <= 2:
            return "low"
        return "medium"

    @staticmethod
    def _apply_priority_and_due_date_fallbacks(parsed: ParsedTaskData) -> ParsedTaskData:
        priority_confirmed = parsed.priority_confidence == "high"
        due_confirmed = parsed.due_date_confidence == "high"
        if not priority_confirmed and not due_confirmed:
            parsed.priority = Priority.LOW
            parsed.priority_confidence = "low"
            parsed.parser_warnings.append(
                "The parser could not confidently confirm either priority or due date. The task was defaulted to low priority and will be due in one week. Please review before saving."
            )
        elif parsed.priority and parsed.priority_confidence == "medium":
            parsed.parser_warnings.append(
                f"Priority was inferred as {parsed.priority}. Please confirm it before saving."
            )
        return parsed

    @staticmethod
    def _priority_due_date(priority: str, base_date: date | None = None) -> tuple[date, date]:
        days_by_priority = {
            Priority.URGENT: 0,
            Priority.HIGH: 2,
            Priority.MEDIUM: 4,
            Priority.LOW: 7,
        }
        start_date = base_date or timezone.localdate()
        fallback_days = days_by_priority.get(priority, 4)
        base_due_date = start_date + timedelta(days=fallback_days)
        fallback_due_date = TaskParsingService._roll_weekend_to_monday(base_due_date)
        return base_due_date, fallback_due_date

    @staticmethod
    def _apply_due_date_rules(parsed: ParsedTaskData) -> ParsedTaskData:
        parsed_due_date = TaskParsingService._parse_due_date(parsed.due_date)
        if parsed_due_date:
            parsed.due_date_original = str(parsed_due_date)
            adjusted_due_date = TaskParsingService._roll_weekend_to_monday(parsed_due_date)
            if adjusted_due_date != parsed_due_date:
                parsed.due_date_weekend_adjusted = True
                parsed.parser_warnings.append("Extracted due date landed on a weekend, so it was moved to Monday.")
            parsed.due_date = str(adjusted_due_date)
            if parsed.due_date_source == "inferred_from_phrase":
                parsed.due_date_warning = (
                    f'The due date was inferred from "{parsed.raw_due_text}" and resolved to {parsed.due_date}. Please confirm it before saving.'
                )
            elif parsed.due_date_weekend_adjusted:
                parsed.due_date_warning = f"The confirmed due date was adjusted to the next Monday: {parsed.due_date}."
            return parsed

        labels_by_priority = {
            Priority.URGENT: "urgent",
            Priority.HIGH: "high",
            Priority.MEDIUM: "medium",
            Priority.LOW: "low",
        }
        base_due_date, fallback_due_date = TaskParsingService._priority_due_date(parsed.priority)
        parsed.due_date = str(fallback_due_date)
        parsed.due_date_source = "priority_default"
        parsed.due_date_original = str(base_due_date)
        parsed.due_date_defaulted = True
        parsed.due_date_inferred = True
        parsed.due_date_weekend_adjusted = fallback_due_date != base_due_date
        parsed.due_date_confidence = "low"
        parsed.raw_due_text = parsed.raw_due_text or f"Priority-based default for {labels_by_priority.get(parsed.priority, parsed.priority)}"
        parsed.parser_warnings.append(
            f"No due date was provided, so the app set one automatically from priority: {labels_by_priority.get(parsed.priority, parsed.priority)} -> {parsed.due_date}."
        )
        parsed.due_date_warning = (
            f"No due date was confirmed in the message. The app applied the {labels_by_priority.get(parsed.priority, parsed.priority)} priority fallback and set the date to {parsed.due_date}. Please review it before saving."
        )
        return parsed

    @staticmethod
    def _roll_weekend_to_monday(value: date) -> date:
        if value.weekday() == 5:
            return value + timedelta(days=2)
        if value.weekday() == 6:
            return value + timedelta(days=1)
        return value

