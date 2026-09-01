"""Parses raw task-request text and worker availability messages into structured data, optionally via an LLM."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import json
import os
import re
from urllib import error, request

from django.utils import timezone

from .assignment_service import TaskAssignmentService
from .models import Priority, TaskEstimateFeedback


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


class TaskParsingService:
    @staticmethod
    def _attachment_names(attachments=None) -> list[str]:
        return [getattr(item, "original_name", getattr(item, "name", "attachment")) for item in (attachments or [])]

    @staticmethod
    def _append_attachment_notes(parsed: ParsedTaskData, attachment_names: list[str]) -> ParsedTaskData:
        if not attachment_names:
            return parsed
        parsed.assignment_rationale.append(
            f"{len(attachment_names)} attachment(s) were preserved for the workflow. The current live parser uses the message text and attachment names; binary image analysis can be added next."
        )
        parsed.parser_warnings.append(
            "Attachments were stored, but the parser currently uses message text and attachment names only."
        )
        return parsed

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
        attachment_names = TaskParsingService._attachment_names(attachments)
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

        parsed = TaskParsingService._append_attachment_notes(parsed, attachment_names)
        parsed = TaskParsingService._apply_priority_and_due_date_fallbacks(parsed)
        parsed = TaskParsingService._apply_due_date_rules(parsed)
        parsed.respond_to_text = TaskParsingService._normalize_notify_contact(parsed.respond_to_text)
        parsed.checklist_items = TaskParsingService._append_notify_checklist_item(parsed.checklist_items, parsed.respond_to_text)
        if not parsed.respond_to_text and any(word in raw_message.lower() for word in ["reply", "respond", "email", "let", "tell", "notify"]):
            parsed.parser_warnings.append("A follow-up contact may be needed, but no clear notify person was extracted.")
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
            respond_to_text=TaskParsingService._infer_notify_contact(raw_message),
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
        # Feed the live parser a few recent human corrections so estimate guesses
        # stay grounded in how this team actually sizes work.
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
                            "respond_to_text": {"type": "string", "description": "Short name of the person or office to notify when the task is complete. Return a contact name only, not a full sentence."},
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
            waiting_person="",
            respond_to_text=TaskParsingService._normalize_notify_contact((parsed.get("respond_to_text") or "")[:255]),
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
    def _normalize_notify_contact(value: str) -> str:
        contact = (value or "").strip()
        if not contact:
            return ""
        lowered = contact.lower()
        prefixes = [
            "respond to ",
            "reply to ",
            "email ",
            "let ",
            "notify ",
            "tell ",
            "follow up with ",
            "send to ",
            "contact ",
        ]
        for prefix in prefixes:
            if lowered.startswith(prefix):
                contact = contact[len(prefix):].strip()
                lowered = contact.lower()
                break
        for splitter in [" know when ", " when ", " after ", " once ", " about ", " regarding ", ":"]:
            idx = lowered.find(splitter)
            if idx > 0:
                contact = contact[:idx].strip(' .,!;:-')
                lowered = contact.lower()
        if lowered.endswith(" know"):
            contact = contact[:-5].strip(' .,!;:-')
            lowered = contact.lower()
        generic_values = {"student worker", "worker", "supervisor", "someone", "them", "him", "her", "team", "staff"}
        if lowered in generic_values or len(contact) > 120:
            return ""
        return contact.strip(' .,!;:-')[:255]

    @staticmethod
    def _infer_notify_contact(raw_message: str) -> str:
        lowered = raw_message.lower()
        patterns = ["let ", "tell ", "notify ", "email ", "reply to ", "respond to ", "send to "]
        for pattern in patterns:
            index = lowered.find(pattern)
            if index >= 0:
                snippet = raw_message[index + len(pattern):]
                for splitter in [" when ", " after ", " once ", " about ", " regarding ", ".", "\n", ","]:
                    lowered_snippet = snippet.lower()
                    split_index = lowered_snippet.find(splitter)
                    if split_index > 0:
                        snippet = snippet[:split_index]
                        break
                return TaskParsingService._normalize_notify_contact(snippet)
        return ""

    @staticmethod
    def _append_notify_checklist_item(items: list[str], notify_contact: str) -> list[str]:
        deduped = TaskParsingService._dedupe_checklist_items(items)
        if not notify_contact:
            return deduped
        follow_up_item = f"Notify {notify_contact} when task is complete"
        return [item for item in deduped if item != follow_up_item] + [follow_up_item]

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
        # First respect any parsed date, then fall back to the priority-based rule
        # when the message never confirmed one.
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


AVAILABILITY_WORK_DAY_START = time(7, 0)
AVAILABILITY_WORK_DAY_END = time(18, 0)

_FULL_DAY_WORDS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thurs": 3, "thur": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_DAY_CODE_LETTER_TO_WEEKDAY = {"m": 0, "t": 1, "w": 2, "r": 3, "f": 4, "s": 5, "u": 6}

_TIME_RANGE_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*(?:-|–|to)\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?",
    re.IGNORECASE,
)


@dataclass
class ParsedAvailabilityData:
    segments_by_weekday: dict
    warnings: list

    def to_dict(self):
        return asdict(self)


def _expand_day_code(code: str):
    weekdays = []
    lowered = code.strip().lower()
    index = 0
    while index < len(lowered):
        if lowered[index:index + 2] == "th":
            weekdays.append(3)
            index += 2
            continue
        mapped = _DAY_CODE_LETTER_TO_WEEKDAY.get(lowered[index])
        if mapped is None:
            return None
        weekdays.append(mapped)
        index += 1
    return weekdays or None


def _weekdays_for_line_prefix(prefix: str):
    lowered = prefix.strip().lower().rstrip(".")
    if lowered in _FULL_DAY_WORDS:
        return [_FULL_DAY_WORDS[lowered]]
    return _expand_day_code(prefix)


def _parse_time_token(hour_str, minute_str, ampm, *, fallback_ampm=None):
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    if hour > 23 or minute > 59:
        return None
    period = (ampm or fallback_ampm or "").lower().replace(".", "")
    if period == "pm" and hour != 12:
        hour = (hour + 12) if hour < 12 else hour
    elif period == "am" and hour == 12:
        hour = 0
    if hour > 23:
        return None
    return time(hour, minute)


def _parse_iso_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%H:%M").time()
    except ValueError:
        return None


def _floor_to_half_hour(value: time) -> time:
    return time(value.hour, 0 if value.minute < 30 else 30)


def _ceil_to_half_hour(value: time) -> time:
    if value.minute == 0:
        return value
    if value.minute <= 30:
        return time(value.hour, 30)
    if value.hour >= 23:
        return time(23, 30)
    return time(value.hour + 1, 0)


class AvailabilityParsingService:
    @staticmethod
    def parser_settings() -> dict:
        return TaskParsingService.parser_settings()

    @staticmethod
    def parse_class_schedule(raw_text: str) -> ParsedAvailabilityData:
        settings = AvailabilityParsingService.parser_settings()
        if not settings["use_mock_parser"] and settings["openai_api_key"]:
            try:
                busy_by_weekday, warnings = AvailabilityParsingService._parse_busy_blocks_with_openai(raw_text, settings)
            except Exception as exc:
                busy_by_weekday, warnings = AvailabilityParsingService._parse_busy_blocks_with_mock(raw_text)
                warnings = warnings + [f"OpenAI parser failed and mock fallback was used: {exc}"]
        else:
            busy_by_weekday, warnings = AvailabilityParsingService._parse_busy_blocks_with_mock(raw_text)
        segments_by_weekday = AvailabilityParsingService._invert_busy_to_free(busy_by_weekday)
        return ParsedAvailabilityData(segments_by_weekday=segments_by_weekday, warnings=warnings)

    @staticmethod
    def _parse_busy_blocks_with_mock(raw_text: str):
        # Best-effort parser for common registrar-style formats, one entry per
        # line, e.g. "MWF 9:00-9:50am" or "Tuesday 2:00pm-3:15pm". Anything
        # messier (comma-separated day lists, pasted screenshots, etc.) should
        # go through the live OpenAI parser instead.
        busy_by_weekday: dict = {}
        unmatched_lines = 0
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        for line in lines:
            prefix_match = re.match(r"^([A-Za-z]+)\s+(.*)$", line)
            if not prefix_match:
                unmatched_lines += 1
                continue
            day_prefix, remainder = prefix_match.groups()
            weekdays = _weekdays_for_line_prefix(day_prefix)
            time_match = _TIME_RANGE_RE.search(remainder)
            if not weekdays or not time_match:
                unmatched_lines += 1
                continue
            start_h, start_m, start_ampm, end_h, end_m, end_ampm = time_match.groups()
            end_time = _parse_time_token(end_h, end_m, end_ampm)
            start_time = _parse_time_token(start_h, start_m, start_ampm, fallback_ampm=end_ampm)
            if not start_time or not end_time or end_time <= start_time:
                unmatched_lines += 1
                continue
            for weekday in weekdays:
                busy_by_weekday.setdefault(weekday, []).append((start_time, end_time))
        warnings = []
        if unmatched_lines:
            warnings.append(f"{unmatched_lines} line(s) could not be read as a class time and were skipped.")
        if not busy_by_weekday:
            warnings.append('No class times were recognized. Try one entry per line, like "MWF 9:00-9:50am".')
        return busy_by_weekday, warnings

    @staticmethod
    def _parse_busy_blocks_with_openai(raw_text: str, settings: dict):
        prompt = (
            "Extract this student's class schedule as a list of weekly busy time blocks. "
            "Expand compact day codes like MWF (Monday/Wednesday/Friday) or TR/TTh (Tuesday/Thursday) "
            "into separate entries, one per weekday. Use 24-hour HH:MM time format. Only include "
            "Monday through Friday classes."
        )
        payload = {
            "model": settings["model"],
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": raw_text},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "class_schedule_extraction",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "classes": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "weekday": {
                                            "type": "string",
                                            "enum": ["monday", "tuesday", "wednesday", "thursday", "friday"],
                                        },
                                        "start_time": {"type": "string"},
                                        "end_time": {"type": "string"},
                                    },
                                    "required": ["weekday", "start_time", "end_time"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["classes"],
                        "additionalProperties": False,
                    },
                },
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

        busy_by_weekday: dict = {}
        for entry in parsed.get("classes", []):
            weekday = _FULL_DAY_WORDS.get(str(entry.get("weekday", "")).lower())
            start_time = _parse_iso_time(entry.get("start_time"))
            end_time = _parse_iso_time(entry.get("end_time"))
            if weekday is None or not start_time or not end_time or end_time <= start_time:
                continue
            busy_by_weekday.setdefault(weekday, []).append((start_time, end_time))
        return busy_by_weekday, []

    @staticmethod
    def _invert_busy_to_free(busy_by_weekday: dict) -> dict:
        free_by_weekday = {}
        for weekday in range(7):
            busy_blocks = sorted(busy_by_weekday.get(weekday, []))
            free_blocks = []
            cursor = AVAILABILITY_WORK_DAY_START
            for start, end in busy_blocks:
                block_start = max(_floor_to_half_hour(start), AVAILABILITY_WORK_DAY_START)
                block_end = min(_ceil_to_half_hour(end), AVAILABILITY_WORK_DAY_END)
                if block_start > cursor:
                    free_blocks.append((cursor, block_start))
                if block_end > cursor:
                    cursor = block_end
            if cursor < AVAILABILITY_WORK_DAY_END:
                free_blocks.append((cursor, AVAILABILITY_WORK_DAY_END))
            free_by_weekday[weekday] = free_blocks
        return free_by_weekday
