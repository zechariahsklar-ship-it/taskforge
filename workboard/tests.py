from datetime import date
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from .models import Priority, StudentAvailability, StudentWorkerProfile, Task, TaskIntakeDraft, TaskStatus, User, UserRole, Weekday
from .services import ParsedTaskData, TaskAssignmentService, TaskParsingService


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


class TaskIntakeReviewViewTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username="supervisor1",
            password="password123",
            role=UserRole.SUPERVISOR,
        )
        self.client.force_login(self.supervisor)
        self.draft = TaskIntakeDraft.objects.create(
            created_by=self.supervisor,
            raw_message="Please finish the update by next Friday.",
            parsed_payload={
                "title": "Finish update",
                "raw_message": "Please finish the update by next Friday.",
                "description": "Please finish the update by next Friday.",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "2026-03-20",
                "raw_due_text": "next Friday",
                "waiting_person": "",
                "respond_to_text": "",
                "estimated_minutes": 30,
                "assigned_to_id": None,
                "assignment_summary": "",
                "assignment_rationale": [],
                "checklist_items": ["Review request", "Send response"],
                "parser_confidence": "medium",
                "parser_warnings": [],
                "due_date_source": "inferred_from_phrase",
                "due_date_original": "2026-03-20",
                "due_date_inferred": True,
                "due_date_defaulted": False,
                "due_date_weekend_adjusted": False,
                "due_date_confidence": "medium",
                "due_date_warning": "",
                "priority_confidence": "medium",
            },
        )

    def test_review_page_renders_checklist_rows(self):
        response = self.client.get(reverse("task-intake-review", args=[self.draft.pk]))

        self.assertContains(response, 'name="checklist_items"', count=3)
        self.assertNotContains(response, "checklist_items_text")

    def test_review_post_saves_non_empty_checklist_rows(self):
        response = self.client.post(
            reverse("task-intake-review", args=[self.draft.pk]),
            {
                "title": "Finish update",
                "raw_message": "Please finish the update by next Friday.",
                "description": "Please finish the update by next Friday.",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "2026-03-20",
                "raw_due_text": "next Friday",
                "waiting_person": "",
                "respond_to_text": "",
                "estimated_minutes": "30",
                "assigned_to": "",
                "requested_by": "",
                "recurring_task": "",
                "recurrence_pattern": "",
                "recurrence_interval": "",
                "recurrence_day_of_week": "",
                "recurrence_day_of_month": "",
                "checklist_items": ["Review request", "", "Send response"],
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        task = Task.objects.get(title="Finish update")
        self.assertEqual(
            list(task.checklist_items.order_by("sort_order").values_list("title", flat=True)),
            ["Review request", "Send response"],
        )


class TaskAssignmentServiceTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username="supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
        )
        self.alex = self._create_worker("alex", "Alex Carter")
        self.jordan = self._create_worker("jordan", "Jordan Lee")

    def _create_worker(self, username, display_name, weekday_hours=4):
        user = User.objects.create_user(
            username=username,
            password="password123",
            role=UserRole.STUDENT_WORKER,
        )
        profile = StudentWorkerProfile.objects.create(
            user=user,
            display_name=display_name,
            email=f"{username}@example.com",
            normal_shift_availability="Weekdays",
            max_hours_per_day=4,
        )
        for weekday in Weekday.values:
            StudentAvailability.objects.create(
                profile=profile,
                weekday=weekday,
                hours_available=weekday_hours if weekday < 5 else 0,
            )
        return user

    def test_suggest_assignee_prefers_student_with_lighter_current_load(self):
        Task.objects.create(
            title="Existing task",
            description="Busy work",
            priority=Priority.MEDIUM,
            status=TaskStatus.ASSIGNED,
            assigned_to=self.alex,
            estimated_minutes=60,
            due_date=date(2026, 3, 17),
        )

        with patch("workboard.services.timezone.localdate", return_value=date(2026, 3, 13)):
            assignee, summary, rationale = TaskAssignmentService.suggest_assignee(
                due_date=date(2026, 3, 17),
                estimated_minutes=30,
                fallback_supervisor=self.supervisor,
            )

        self.assertEqual(assignee, self.jordan)
        self.assertIn("Suggested student", summary)
        self.assertIn("Jordan Lee", rationale[0])

    def test_suggest_assignee_falls_back_to_supervisor_when_students_cannot_fit_work(self):
        for profile in StudentWorkerProfile.objects.all():
            profile.weekly_availability.all().update(hours_available=0)

        with patch("workboard.services.timezone.localdate", return_value=date(2026, 3, 13)):
            assignee, summary, rationale = TaskAssignmentService.suggest_assignee(
                due_date=date(2026, 3, 13),
                estimated_minutes=180,
                fallback_supervisor=self.supervisor,
            )

        self.assertEqual(assignee, self.supervisor)
        self.assertIn("assigned to a supervisor", summary)
        self.assertIn("Fallback rule assigned the task to a supervisor.", rationale)


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
