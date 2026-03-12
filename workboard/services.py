from dataclasses import dataclass

from .models import Priority


@dataclass
class ParsedTaskData:
    raw_message: str
    title: str
    priority: str
    raw_due_text: str
    waiting_person: str
    respond_to_text: str
    estimated_minutes: int | None


class TaskParsingService:
    @staticmethod
    def parse_request(raw_message: str) -> ParsedTaskData:
        first_line = raw_message.strip().splitlines()[0] if raw_message.strip() else "New task request"
        lowered = raw_message.lower()
        priority = Priority.HIGH if "urgent" in lowered else Priority.MEDIUM
        estimated_minutes = 60 if len(raw_message) > 240 else 30
        return ParsedTaskData(
            raw_message=raw_message,
            title=first_line[:255] or "New task request",
            priority=priority,
            raw_due_text="Needs review",
            waiting_person="",
            respond_to_text="",
            estimated_minutes=estimated_minutes,
        )
