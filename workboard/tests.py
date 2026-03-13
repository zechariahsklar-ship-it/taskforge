from datetime import date
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import Priority, StudentAvailability, StudentWorkerProfile, Task, TaskChecklistItem, TaskEstimateFeedback, TaskIntakeDraft, TaskStatus, User, UserRole, Weekday
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
            list(task.checklist_items.values_list("title", flat=True)),
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
            status=TaskStatus.NEW,
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


class TaskCreateDueDateFallbackTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username="create-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
        )
        self.client.force_login(self.supervisor)

    def test_direct_task_create_applies_priority_due_date_fallback(self):
        with patch("workboard.views.TaskParsingService._priority_due_date", return_value=(date(2026, 3, 15), date(2026, 3, 16))):
            response = self.client.post(
                reverse("task-create"),
                {
                    "title": "Manual task",
                    "raw_message": "",
                    "description": "Manual task without explicit due date",
                    "priority": Priority.HIGH,
                    "status": TaskStatus.NEW,
                    "due_date": "",
                    "raw_due_text": "",
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
                },
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        task = Task.objects.get(title="Manual task")
        self.assertEqual(task.due_date, date(2026, 3, 16))
        self.assertEqual(task.raw_due_text, "Priority-based default for high")


class TaskVisibilityAndAdditionalAssigneeTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="sup-vis", password="password123", role=UserRole.SUPERVISOR)
        self.primary_student = User.objects.create_user(username="alex-vis", password="password123", role=UserRole.STUDENT_WORKER)
        self.extra_student = User.objects.create_user(username="jordan-vis", password="password123", role=UserRole.STUDENT_WORKER)
        self.other_student = User.objects.create_user(username="other-vis", password="password123", role=UserRole.STUDENT_WORKER)
        self.task = Task.objects.create(
            title="Shared task",
            description="A task with multiple assignees",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.primary_student,
            created_by=self.supervisor,
        )
        self.task.additional_assignees.add(self.extra_student)

    def test_student_my_tasks_includes_additional_assignee_tasks(self):
        self.client.force_login(self.extra_student)
        response = self.client.get(reverse("my-tasks"))
        self.assertContains(response, "Shared task")

    def test_other_student_cannot_view_shared_task_detail(self):
        self.client.force_login(self.other_student)
        response = self.client.get(reverse("task-detail", args=[self.task.pk]))
        self.assertEqual(response.status_code, 403)

    def test_supervisor_can_set_additional_assignees_on_create(self):
        self.client.force_login(self.supervisor)
        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Supervisor created shared task",
                "raw_message": "",
                "description": "Supervisor task",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "",
                "raw_due_text": "",
                "waiting_person": "",
                "respond_to_text": "",
                "estimated_minutes": "30",
                "assigned_to": str(self.primary_student.pk),
                "additional_assignees": [str(self.primary_student.pk), str(self.extra_student.pk)],
                "requested_by": "",
                "recurring_task": "",
                "recurrence_pattern": "",
                "recurrence_interval": "",
                "recurrence_day_of_week": "",
                "recurrence_day_of_month": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        task = Task.objects.get(title="Supervisor created shared task")
        self.assertEqual(task.assigned_to, self.primary_student)
        self.assertEqual(list(task.additional_assignees.values_list("id", flat=True)), [self.extra_student.id])


class MyTasksViewOrderingTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="mytasks-supervisor", password="password123", role=UserRole.SUPERVISOR)
        self.student = User.objects.create_user(username="mytasks-student", password="password123", role=UserRole.STUDENT_WORKER)
        self.first_task = Task.objects.create(
            title="First visible task",
            description="First",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.student,
            board_order=1,
            estimated_minutes=20,
        )
        self.second_task = Task.objects.create(
            title="Second visible task",
            description="Second",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.student,
            board_order=2,
            estimated_minutes=45,
        )
        self.review_task = Task.objects.create(
            title="Review visible task",
            description="Review",
            priority=Priority.HIGH,
            status=TaskStatus.REVIEW,
            assigned_to=self.student,
            board_order=1,
            estimated_minutes=30,
        )

    def test_my_tasks_groups_tasks_by_status_and_preserves_board_order(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("my-tasks"))

        self.assertEqual(response.status_code, 200)
        grouped_tasks = response.context["grouped_tasks"]
        new_column = next(column for column in grouped_tasks if column["value"] == TaskStatus.NEW)
        review_column = next(column for column in grouped_tasks if column["value"] == TaskStatus.REVIEW)

        self.assertEqual([task.title for task in new_column["tasks"]], ["First visible task", "Second visible task"])
        self.assertEqual([task.title for task in review_column["tasks"]], ["Review visible task"])
        self.assertContains(response, "Time: 20 min")
        self.assertContains(response, "Time: 45 min")

    def test_supervisor_my_tasks_includes_waiting_tasks(self):
        waiting_task = Task.objects.create(
            title="Waiting task for supervisors",
            description="Blocked by external input",
            priority=Priority.HIGH,
            status=TaskStatus.WAITING,
            assigned_to=self.student,
            board_order=1,
        )
        self.client.force_login(self.supervisor)
        response = self.client.get(reverse("my-tasks"))

        self.assertEqual(response.status_code, 200)
        grouped_tasks = response.context["grouped_tasks"]
        waiting_column = next(column for column in grouped_tasks if column["value"] == TaskStatus.WAITING)
        self.assertIn(waiting_task, waiting_column["tasks"])


class BoardTaskMoveTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="move-sup", password="password123", role=UserRole.SUPERVISOR)
        self.student = User.objects.create_user(username="move-student", password="password123", role=UserRole.STUDENT_WORKER)
        self.other_student = User.objects.create_user(username="move-other", password="password123", role=UserRole.STUDENT_WORKER)
        self.task = Task.objects.create(
            title="Movable task",
            description="Move me",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.student,
            created_by=self.supervisor,
            board_order=1,
        )
        self.second_task = Task.objects.create(
            title="Second task",
            description="Place me later",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.student,
            created_by=self.supervisor,
            board_order=2,
        )
        self.third_task = Task.objects.create(
            title="Third task",
            description="Reorder me",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.student,
            created_by=self.supervisor,
            board_order=3,
        )
        self.review_task = Task.objects.create(
            title="Review task",
            description="Already in review",
            priority=Priority.MEDIUM,
            status=TaskStatus.REVIEW,
            assigned_to=self.student,
            created_by=self.supervisor,
            board_order=1,
        )

    def test_supervisor_can_move_task_between_columns(self):
        self.client.force_login(self.supervisor)
        response = self.client.post(
            reverse("board-task-move", args=[self.task.pk]),
            {"status": TaskStatus.REVIEW, "before_task_id": str(self.review_task.pk)},
        )
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.review_task.refresh_from_db()
        self.second_task.refresh_from_db()
        self.third_task.refresh_from_db()
        self.assertEqual(self.task.status, TaskStatus.REVIEW)
        self.assertEqual(self.task.board_order, 1)
        self.assertEqual(self.review_task.board_order, 2)
        self.assertEqual(self.second_task.board_order, 1)
        self.assertEqual(self.third_task.board_order, 2)

    def test_assigned_student_can_move_own_task(self):
        self.client.force_login(self.student)
        response = self.client.post(reverse("board-task-move", args=[self.task.pk]), {"status": TaskStatus.IN_PROGRESS})
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(self.task.board_order, 1)

    def test_supervisor_can_reorder_within_same_column(self):
        self.client.force_login(self.supervisor)
        response = self.client.post(
            reverse("board-task-move", args=[self.third_task.pk]),
            {"status": TaskStatus.NEW, "before_task_id": str(self.second_task.pk)},
        )
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.second_task.refresh_from_db()
        self.third_task.refresh_from_db()
        self.assertEqual(self.task.board_order, 1)
        self.assertEqual(self.third_task.board_order, 2)
        self.assertEqual(self.second_task.board_order, 3)

    def test_unassigned_student_cannot_move_task(self):
        self.client.force_login(self.other_student)
        response = self.client.post(reverse("board-task-move", args=[self.task.pk]), {"status": TaskStatus.DONE})
        self.assertEqual(response.status_code, 403)


class TaskDetailChecklistTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="detail-sup", password="password123", role=UserRole.SUPERVISOR)
        self.student = User.objects.create_user(username="detail-student", password="password123", role=UserRole.STUDENT_WORKER)
        self.task = Task.objects.create(
            title="Checklist task",
            description="Task with checklist",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            due_date=date(2026, 3, 20),
            raw_due_text="next Friday",
            assigned_to=self.student,
            created_by=self.supervisor,
        )
        self.first = TaskChecklistItem.objects.create(task=self.task, title="First item", position=1)
        self.second = TaskChecklistItem.objects.create(task=self.task, title="Second item", position=2)
        self.third = TaskChecklistItem.objects.create(task=self.task, title="Third item", position=3)

    def test_supervisor_can_reorder_checklist_items(self):
        self.client.force_login(self.supervisor)
        response = self.client.post(
            reverse("task-detail", args=[self.task.pk]),
            {
                "action": "checklist_reorder",
                "item_ids": [str(self.third.pk), str(self.first.pk), str(self.second.pk)],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.third.refresh_from_db()
        self.assertEqual(self.third.position, 1)
        self.assertEqual(self.first.position, 2)
        self.assertEqual(self.second.position, 3)

    def test_supervisor_can_save_checklist_titles_and_order_from_task_screen(self):
        self.client.force_login(self.supervisor)
        response = self.client.post(
            reverse("task-detail", args=[self.task.pk]),
            {
                "action": "checklist_save",
                "checklist_item_ids": [str(self.second.pk), str(self.first.pk), str(self.third.pk)],
                "checklist_item_titles": ["Updated second", "Updated first", "Updated third"],
                "checklist_item_completed": [str(self.first.pk), str(self.third.pk)],
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.third.refresh_from_db()
        self.assertEqual(self.second.position, 1)
        self.assertEqual(self.first.position, 2)
        self.assertEqual(self.third.title, "Updated third")
        self.assertTrue(self.first.is_completed)
        self.assertTrue(self.third.is_completed)

    def test_blank_checklist_title_deletes_item_instead_of_restoring_old_text(self):
        self.client.force_login(self.supervisor)
        response = self.client.post(
            reverse("task-detail", args=[self.task.pk]),
            {
                "action": "checklist_save",
                "checklist_item_ids": [str(self.first.pk), str(self.second.pk), str(self.third.pk)],
                "checklist_item_titles": ["First item", "", "Third item"],
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TaskChecklistItem.objects.filter(pk=self.second.pk).exists())
        self.first.refresh_from_db()
        self.third.refresh_from_db()
        self.assertEqual(self.first.position, 1)
        self.assertEqual(self.third.position, 2)

    def test_supervisor_can_delete_checklist_item_from_task_screen(self):
        self.client.force_login(self.supervisor)
        response = self.client.post(
            reverse("task-detail", args=[self.task.pk]),
            {
                "action": "checklist_save",
                "checklist_item_ids": [str(self.first.pk), str(self.second.pk), str(self.third.pk)],
                "checklist_item_titles": ["First item", "Second item", "Third item"],
                "delete_item_id": str(self.first.pk),
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TaskChecklistItem.objects.filter(pk=self.first.pk).exists())

    def test_assigned_user_can_toggle_checklist_completion(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse("task-detail", args=[self.task.pk]),
            {"action": "checklist_toggle", "item_id": str(self.first.pk), "is_completed": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.first.refresh_from_db()
        self.assertTrue(self.first.is_completed)

    def test_checklist_add_form_uses_placeholder_instead_of_title_label(self):
        self.client.force_login(self.supervisor)
        response = self.client.get(reverse("task-detail", args=[self.task.pk]))
        self.assertContains(response, 'placeholder="Add checklist item"')
        self.assertNotContains(response, '<label for="id_title">Title:</label>', html=False)

    def test_task_detail_shows_actual_due_date_not_raw_due_text(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("task-detail", args=[self.task.pk]))
        self.assertContains(response, "Due: Mar 20, 2026")
        self.assertNotContains(response, "Due: next Friday")

    def test_any_assigned_user_can_add_attachment_from_task_detail(self):
        self.client.force_login(self.student)
        upload = SimpleUploadedFile("note.txt", b"hello", content_type="text/plain")
        response = self.client.post(
            reverse("task-detail", args=[self.task.pk]),
            {"action": "attachment", "file": upload},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.task.attachments.count(), 1)
        self.assertEqual(self.task.attachments.first().original_name, "note.txt")

    def test_notes_render_oldest_first(self):
        self.client.force_login(self.student)
        self.client.post(reverse("task-detail", args=[self.task.pk]), {"action": "note", "body": "First note"}, follow=True)
        self.client.post(reverse("task-detail", args=[self.task.pk]), {"action": "note", "body": "Second note"}, follow=True)
        response = self.client.get(reverse("task-detail", args=[self.task.pk]))
        content = response.content.decode()
        self.assertLess(content.index("First note"), content.index("Second note"))


class TaskEstimateFeedbackTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="estimate-sup", password="password123", role=UserRole.SUPERVISOR)
        self.student = User.objects.create_user(username="estimate-student", password="password123", role=UserRole.STUDENT_WORKER)
        self.task = Task.objects.create(
            title="Estimate task",
            raw_message="Please organize the donor spreadsheet and send an updated copy.",
            description="Estimate test",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            estimated_minutes=30,
            assigned_to=self.student,
            created_by=self.supervisor,
        )
        self.client.force_login(self.supervisor)

    def test_board_card_shows_estimate_text(self):
        response = self.client.get(reverse("board"))
        self.assertContains(response, "Time: 30 min")

    def test_task_edit_records_estimate_feedback_when_minutes_change(self):
        response = self.client.post(
            reverse("task-edit", args=[self.task.pk]),
            {
                "title": self.task.title,
                "raw_message": self.task.raw_message,
                "description": self.task.description,
                "priority": self.task.priority,
                "status": self.task.status,
                "due_date": "",
                "raw_due_text": "",
                "waiting_person": "",
                "respond_to_text": "",
                "estimated_minutes": "75",
                "assigned_to": str(self.student.pk),
                "additional_assignees": [],
                "requested_by": "",
                "recurring_task": "",
                "recurrence_pattern": "",
                "recurrence_interval": "",
                "recurrence_day_of_week": "",
                "recurrence_day_of_month": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        feedback = TaskEstimateFeedback.objects.get(task=self.task)
        self.assertEqual(feedback.original_estimated_minutes, 30)
        self.assertEqual(feedback.corrected_estimated_minutes, 75)
        self.assertEqual(feedback.source, "task_edit")


class AssignedBucketRemovalTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="assigned-bucket-sup", password="password123", role=UserRole.SUPERVISOR)
        self.student = User.objects.create_user(username="assigned-bucket-student", password="password123", role=UserRole.STUDENT_WORKER)
        self.task = Task.objects.create(
            title="Legacy assigned task",
            description="Legacy assigned task",
            priority=Priority.MEDIUM,
            status=TaskStatus.ASSIGNED,
            assigned_to=self.student,
            board_order=1,
        )

    def test_board_groups_legacy_assigned_status_under_new_requests(self):
        self.client.force_login(self.supervisor)
        response = self.client.get(reverse("board"))
        grouped_tasks = response.context["grouped_tasks"]
        self.assertFalse(any(column["value"] == TaskStatus.ASSIGNED for column in grouped_tasks))
        new_column = next(column for column in grouped_tasks if column["value"] == TaskStatus.NEW)
        self.assertIn(self.task, new_column["tasks"])
