from datetime import date, time
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from ..models import Priority, Task, TaskAuditAction, TaskAuditEvent, TaskChecklistItem, TaskEstimateFeedback, TaskStatus, User, UserRole, Weekday


class TaskDetailChecklistTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="detail-sup", password="password123", role=UserRole.SUPERVISOR)
        self.student = User.objects.create_user(username="detail-student", password="password123", role=UserRole.STUDENT_WORKER)
        self.student_supervisor = User.objects.create_user(username="detail-student-sup", password="password123", role=UserRole.STUDENT_SUPERVISOR)
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

    def test_assigned_user_can_reorder_checklist_items(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse("task-detail", args=[self.task.pk]),
            {
                "action": "checklist_reorder",
                "item_ids": [str(self.second.pk), str(self.third.pk), str(self.first.pk)],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.third.refresh_from_db()
        self.assertEqual(self.second.position, 1)
        self.assertEqual(self.third.position, 2)
        self.assertEqual(self.first.position, 3)

    def test_student_supervisor_can_reorder_but_not_edit_checklist_text(self):
        self.client.force_login(self.student_supervisor)
        response = self.client.get(reverse("task-detail", args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-checklist-toggle value="{}"'.format(self.first.pk), html=False)
        self.assertContains(response, 'class="checklist-grip"', html=False)
        self.assertNotContains(response, 'class="form-control checklist-title-input"', html=False)
        self.assertNotContains(response, 'class="button-link checklist-delete"', html=False)
        self.assertNotContains(response, 'placeholder="Add checklist item"', html=False)

        post_response = self.client.post(
            reverse("task-detail", args=[self.task.pk]),
            {
                "action": "checklist_save",
                "checklist_item_ids": [str(self.first.pk), str(self.second.pk), str(self.third.pk)],
                "checklist_item_titles": ["Edited first", "Edited second", "Edited third"],
            },
            follow=True,
        )
        self.assertEqual(post_response.status_code, 200)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.third.refresh_from_db()
        self.assertEqual(self.first.title, "First item")
        self.assertEqual(self.second.title, "Second item")
        self.assertEqual(self.third.title, "Third item")

        reorder_response = self.client.post(
            reverse("task-detail", args=[self.task.pk]),
            {
                "action": "checklist_reorder",
                "item_ids": [str(self.third.pk), str(self.first.pk), str(self.second.pk)],
            },
        )
        self.assertEqual(reorder_response.status_code, 200)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.third.refresh_from_db()
        self.assertEqual(self.third.position, 1)
        self.assertEqual(self.first.position, 2)
        self.assertEqual(self.second.position, 3)

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


    def test_supervisor_can_delete_task(self):
        self.client.force_login(self.supervisor)
        response = self.client.post(reverse("task-delete", args=[self.task.pk]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Task.objects.filter(pk=self.task.pk).exists())


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

    def test_task_edit_can_enable_weekly_recurring_and_show_in_recurring_list(self):
        self.task.due_date = date(2026, 4, 10)
        self.task.scheduled_date = date(2026, 4, 10)
        self.task.scheduled_start_time = time(8, 0)
        self.task.scheduled_end_time = time(17, 0)
        self.task.save(update_fields=["due_date", "scheduled_date", "scheduled_start_time", "scheduled_end_time", "updated_at"])

        response = self.client.post(
            reverse("task-edit", args=[self.task.pk]),
            {
                "title": self.task.title,
                "raw_message": self.task.raw_message,
                "description": self.task.description,
                "priority": self.task.priority,
                "status": self.task.status,
                "due_date": "2026-04-10",
                "scheduled_week_of": "2026-04-06",
                "task_window_day_4_segments": '[["08:00", "17:00"]]',
                "raw_due_text": "",
                "waiting_person": "",
                "respond_to_text": "",
                "estimated_minutes": "60",
                "assigned_to": str(self.student.pk),
                "additional_assignees": [],
                "requested_by": "",
                "recurring_task": "on",
                "recurrence_pattern": "weekly",
                "recurrence_interval": "1",
                "recurrence_day_of_week": str(Weekday.FRIDAY),
                "recurrence_day_of_month": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertTrue(self.task.recurring_task)
        self.assertEqual(self.task.recurrence_pattern, "weekly")
        self.assertEqual(self.task.recurrence_day_of_week, Weekday.FRIDAY)
        self.assertEqual(self.task.assigned_to, self.supervisor)
        self.assertIsNotNone(self.task.recurring_template)
        self.assertEqual(self.task.recurring_template.day_of_week, Weekday.FRIDAY)
        recurring_response = self.client.get(reverse("recurring-list"))
        self.assertEqual(recurring_response.status_code, 200)
        self.assertContains(recurring_response, self.task.title)

    def test_task_edit_can_enable_recurring_without_touching_half_hour_schedule_fields(self):
        self.task.due_date = date(2026, 4, 10)
        self.task.scheduled_date = date(2026, 4, 10)
        self.task.scheduled_start_time = time(8, 30)
        self.task.scheduled_end_time = time(12, 0)
        self.task.save(update_fields=["due_date", "scheduled_date", "scheduled_start_time", "scheduled_end_time", "updated_at"])

        response = self.client.post(
            reverse("task-edit", args=[self.task.pk]),
            {
                "title": self.task.title,
                "raw_message": self.task.raw_message,
                "description": self.task.description,
                "priority": self.task.priority,
                "status": self.task.status,
                "due_date": "2026-04-10",
                "scheduled_week_of": "2026-04-06",
                "scheduled_date": "2026-04-10",
                "scheduled_start_time": "08:30:00",
                "scheduled_end_time": "12:00:00",
                "scheduled_window_segments": '[["08:30", "12:00"]]',
                "scheduled_window_start": "08:30",
                "scheduled_window_end": "12:00",
                "scheduled_window_hours": "3.5",
                "task_window_day_0_segments": "",
                "task_window_day_1_segments": "",
                "task_window_day_2_segments": "",
                "task_window_day_3_segments": "",
                "task_window_day_4_segments": '[["08:30", "12:00"]]',
                "raw_due_text": "",
                "waiting_person": "",
                "respond_to_text": "",
                "estimated_minutes": "60",
                "assigned_to": str(self.student.pk),
                "additional_assignees": [],
                "requested_by": "",
                "recurring_task": "on",
                "recurrence_pattern": "weekly",
                "recurrence_interval": "1",
                "recurrence_day_of_week": str(Weekday.FRIDAY),
                "recurrence_day_of_month": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertTrue(self.task.recurring_task)
        self.assertEqual(self.task.recurrence_pattern, "weekly")
        self.assertEqual(self.task.recurrence_day_of_week, Weekday.FRIDAY)
        self.assertEqual(self.task.scheduled_start_time, time(8, 30))
        self.assertEqual(self.task.scheduled_end_time, time(12, 0))
        self.assertIsNotNone(self.task.recurring_template)
        self.assertEqual(self.task.recurring_template.day_of_week, Weekday.FRIDAY)
        recurring_response = self.client.get(reverse("recurring-list"))
        self.assertEqual(recurring_response.status_code, 200)
        self.assertContains(recurring_response, self.task.title)


class TaskAuditHistoryTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username="audit-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
        )
        self.client.force_login(self.supervisor)
        self.task = Task.objects.create(
            title="Audit task",
            description="Track me",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            created_by=self.supervisor,
        )

    def test_task_create_records_audit_event(self):
        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Created with audit",
                "description": "Fresh task",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "respond_to_text": "",
                "estimated_minutes": "",
                "recurring_task": "",
                "rotating_additional_assignee_count": 0,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        task = Task.objects.get(title="Created with audit")
        self.assertTrue(task.audit_events.filter(action=TaskAuditAction.CREATED).exists())

    def test_status_change_records_audit_event(self):
        response = self.client.post(
            reverse("task-detail", args=[self.task.pk]),
            {"action": "status", "status": TaskStatus.IN_PROGRESS},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        event = self.task.audit_events.first()
        self.assertIsNotNone(event)
        self.assertEqual(event.action, TaskAuditAction.STATUS_CHANGED)
        self.assertIn("Changed status", event.summary)

    def test_ajax_status_change_returns_json_without_redirect(self):
        response = self.client.post(
            reverse("task-detail", args=[self.task.pk]),
            {"action": "status", "status": TaskStatus.IN_PROGRESS},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["status"], TaskStatus.IN_PROGRESS)
        self.assertEqual(payload["label"], "In Progress")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TaskStatus.IN_PROGRESS)
        self.assertTrue(self.task.audit_events.filter(action=TaskAuditAction.STATUS_CHANGED).exists())

    def test_note_add_records_audit_event(self):
        response = self.client.post(
            reverse("task-detail", args=[self.task.pk]),
            {"action": "note", "body": "Audit note"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        event = self.task.audit_events.first()
        self.assertIsNotNone(event)
        self.assertEqual(event.action, TaskAuditAction.NOTE_ADDED)

    def test_deleted_task_keeps_audit_record(self):
        response = self.client.post(reverse("task-delete", args=[self.task.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Task.objects.filter(pk=self.task.pk).exists())
        self.assertTrue(TaskAuditEvent.objects.filter(task_title="Audit task", action=TaskAuditAction.DELETED).exists())
