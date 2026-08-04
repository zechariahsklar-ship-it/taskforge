from datetime import date
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from ..models import Priority, User, UserRole
from ..services import AvailabilityParsingService, ParsedTaskData, TaskParsingService


class TaskParsingServiceTests(TestCase):
    def build_parsed(self, **overrides):
        data = {
            "raw_message": "Please finish this next Friday.",
            "title": "Test task",
            "description": "Description",
            "priority": Priority.MEDIUM,
            "due_date": None,
            "raw_due_text": "Needs review",
            "waiting_person": "",
            "respond_to_text": "",
            "estimated_minutes": 30,
            "assigned_to_id": None,
            "assignment_summary": "",
            "assignment_rationale": [],
            "checklist_items": ["One"],
            "parser_confidence": "medium",
            "parser_warnings": [],
            "due_date_source": "unconfirmed",
            "due_date_original": None,
            "due_date_inferred": False,
            "due_date_defaulted": False,
            "due_date_weekend_adjusted": False,
            "due_date_confidence": "low",
            "due_date_warning": "",
            "priority_confidence": "medium",
        }
        data.update(overrides)
        return ParsedTaskData(**data)

    def test_due_date_metadata_marks_relative_phrase_as_inferred(self):
        source, inferred, confidence = TaskParsingService._due_date_metadata("next Friday", "2026-03-20")

        self.assertEqual(source, "inferred_from_phrase")
        self.assertTrue(inferred)
        self.assertEqual(confidence, "medium")

    def test_due_date_metadata_marks_absolute_phrase_as_high_confidence(self):
        source, inferred, confidence = TaskParsingService._due_date_metadata("March 20, 2026", "2026-03-20")

        self.assertEqual(source, "parsed")
        self.assertFalse(inferred)
        self.assertEqual(confidence, "high")

    def test_due_date_fallback_rolls_weekend_to_monday(self):
        parsed = self.build_parsed(priority=Priority.HIGH, priority_confidence="high")

        with patch("workboard.services.timezone.localdate", return_value=date(2026, 3, 12)):
            updated = TaskParsingService._apply_due_date_rules(parsed)

        self.assertEqual(updated.due_date, "2026-03-16")
        self.assertEqual(updated.due_date_source, "priority_default")
        self.assertTrue(updated.due_date_defaulted)
        self.assertTrue(updated.due_date_weekend_adjusted)
        self.assertEqual(updated.due_date_confidence, "low")

    def test_due_date_rules_warn_for_relative_phrase_resolution(self):
        parsed = self.build_parsed(
            due_date="2026-03-20",
            raw_due_text="next Friday",
            due_date_source="inferred_from_phrase",
            due_date_inferred=True,
            due_date_confidence="medium",
        )

        updated = TaskParsingService._apply_due_date_rules(parsed)

        self.assertIn('inferred from "next Friday"', updated.due_date_warning)
        self.assertEqual(updated.due_date_original, "2026-03-20")


    def test_notify_contact_is_normalized_and_appended_to_checklist(self):
        parsed = TaskParsingService.parse_request(
            "Lookup and deduplicate records for Billy Bob, Sally May, and Todd Blanch in Slate. Let Billy Bob know when it is done.",
            fallback_supervisor=self.build_supervisor_for_parse(),
        )

        self.assertEqual(parsed.waiting_person, "")
        self.assertEqual(parsed.respond_to_text, "Billy Bob")
        self.assertEqual(parsed.checklist_items[-1], "Notify Billy Bob when task is complete")

    def build_supervisor_for_parse(self):
        return User.objects.create_user(username="parse-supervisor-temp", password="password123", role=UserRole.SUPERVISOR)


class TaskParsingFallbackTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username="supervisor-fallback",
            password="password123",
            role=UserRole.SUPERVISOR,
        )

    def test_parse_request_uses_mock_and_low_confidence_when_openai_errors(self):
        with patch.object(TaskParsingService, "parser_settings", return_value={
            "use_mock_parser": False,
            "openai_api_key": "test-key",
            "model": "gpt-test",
            "endpoint": "https://api.openai.com/v1/chat/completions",
        }), patch.object(TaskParsingService, "_parse_with_openai", side_effect=RuntimeError("boom")):
            parsed = TaskParsingService.parse_request("Please reply tomorrow.", fallback_supervisor=self.supervisor)

        self.assertEqual(parsed.parser_confidence, "low")
        self.assertIn("OpenAI parsing failed, so the app used the mock parser instead.", parsed.parser_warnings)
        self.assertTrue(any("OpenAI parser failed and mock fallback was used: boom" in item for item in parsed.assignment_rationale))
        self.assertEqual(parsed.assigned_to_id, self.supervisor.id)

    def test_parse_request_uses_mock_with_warning_when_api_key_missing(self):
        with patch.object(TaskParsingService, "parser_settings", return_value={
            "use_mock_parser": False,
            "openai_api_key": "",
            "model": "gpt-test",
            "endpoint": "https://api.openai.com/v1/chat/completions",
        }):
            parsed = TaskParsingService.parse_request("Please review this soon.", fallback_supervisor=self.supervisor)

        self.assertIn("OPENAI_API_KEY is missing, so the app used the mock parser.", parsed.parser_warnings)
        self.assertTrue(any("Parser mode: mock" in item for item in parsed.assignment_rationale))
        self.assertEqual(parsed.assigned_to_id, self.supervisor.id)


class AvailabilityParsingServiceTests(TestCase):
    def test_mock_parser_extracts_compact_day_codes_and_inverts_to_free_blocks(self):
        raw_text = "MWF 9:00-9:50am\nTR 2:00pm-3:15pm"

        with patch.object(TaskParsingService, "parser_settings", return_value={
            "use_mock_parser": True,
            "openai_api_key": "",
            "model": "gpt-test",
            "endpoint": "https://api.openai.com/v1/chat/completions",
        }):
            parsed = AvailabilityParsingService.parse_class_schedule(raw_text)

        monday_blocks = parsed.segments_by_weekday[0]
        self.assertEqual(
            [(b[0].strftime("%H:%M"), b[1].strftime("%H:%M")) for b in monday_blocks],
            [("07:00", "09:00"), ("10:00", "18:00")],
        )
        tuesday_blocks = parsed.segments_by_weekday[1]
        self.assertEqual(
            [(b[0].strftime("%H:%M"), b[1].strftime("%H:%M")) for b in tuesday_blocks],
            [("07:00", "14:00"), ("15:30", "18:00")],
        )
        saturday_blocks = parsed.segments_by_weekday[5]
        self.assertEqual(
            [(b[0].strftime("%H:%M"), b[1].strftime("%H:%M")) for b in saturday_blocks],
            [("07:00", "18:00")],
        )

    def test_mock_parser_warns_when_nothing_recognized(self):
        with patch.object(TaskParsingService, "parser_settings", return_value={
            "use_mock_parser": True,
            "openai_api_key": "",
            "model": "gpt-test",
            "endpoint": "https://api.openai.com/v1/chat/completions",
        }):
            parsed = AvailabilityParsingService.parse_class_schedule("not a schedule at all")

        self.assertTrue(parsed.warnings)


class ParseClassScheduleViewTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="parse-schedule-supervisor", password="password123", role=UserRole.SUPERVISOR)
        self.worker = User.objects.create_user(username="parse-schedule-worker", password="password123", role=UserRole.STUDENT_WORKER)

    def test_supervisor_can_parse_class_schedule_into_segments(self):
        self.client.force_login(self.supervisor)

        with patch.object(TaskParsingService, "parser_settings", return_value={
            "use_mock_parser": True,
            "openai_api_key": "",
            "model": "gpt-test",
            "endpoint": "https://api.openai.com/v1/chat/completions",
        }):
            response = self.client.post(reverse("parse-class-schedule"), {"raw_schedule": "MWF 9:00-9:50am"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["segments_by_day"]["monday"], [["07:00", "09:00"], ["10:00", "18:00"]])
        self.assertEqual(payload["segments_by_day"]["tuesday"], [["07:00", "18:00"]])

    def test_worker_cannot_access_parse_endpoint(self):
        self.client.force_login(self.worker)

        response = self.client.post(reverse("parse-class-schedule"), {"raw_schedule": "MWF 9:00-9:50am"})

        self.assertEqual(response.status_code, 403)

    def test_get_request_is_rejected(self):
        self.client.force_login(self.supervisor)

        response = self.client.get(reverse("parse-class-schedule"))

        self.assertEqual(response.status_code, 400)
