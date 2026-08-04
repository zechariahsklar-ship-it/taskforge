from django.test import TestCase
from django.urls import reverse
from ..models import Priority, Task, TaskIntakeDraft, TaskStatus, User, UserRole


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
                "respond_to_text": "Billy Bob",
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
        self.assertNotContains(response, "Recurring task")
        self.assertNotContains(response, "Recurrence pattern")
        self.assertNotContains(response, 'name="recurring_task"')
        self.assertNotContains(response, 'name="recurrence_pattern"')
        self.assertNotContains(response, 'name="recurrence_interval"')

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
                "respond_to_text": "Billy Bob",
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
            list(task.checklist_items.values_list("title", flat=True)),
            ["Review request", "Send response", "Notify Billy Bob when task is complete"],
        )


class TaskIntakeViewTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username="intake-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
        )
        self.client.force_login(self.supervisor)

    def test_intake_page_renders_loading_state_hooks(self):
        response = self.client.get(reverse("task-intake"))

        self.assertContains(response, 'data-intake-form')
        self.assertContains(response, 'data-intake-submit')
        self.assertContains(response, 'data-intake-loading')
        self.assertContains(response, "Parsing request and preparing review")
