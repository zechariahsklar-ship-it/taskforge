from datetime import date, datetime, time
import json
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Priority, RecurringTaskTemplate, ScheduleAdjustmentRequest, ScheduleAdjustmentRequestStatus, StudentAvailability, StudentAvailabilityBlock, StudentScheduleOverride, StudentWorkerProfile, Task, TaskAuditAction, TaskAuditEvent, TaskChecklistItem, TaskEstimateFeedback, TaskIntakeDraft, TaskStatus, Team, User, UserRole, Weekday, WorkerTag
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

class RecurringTaskListViewTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username="recurring-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
        )
        self.worker = User.objects.create_user(
            username="recurring-worker",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            first_name="Jamie",
            last_name="Worker",
        )
        self.helper = User.objects.create_user(
            username="recurring-helper",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            first_name="Casey",
            last_name="Helper",
        )
        self.first_template = RecurringTaskTemplate.objects.create(
            title="Weekly mail run",
            description="Pick up and sort campus mail.",
            priority=Priority.MEDIUM,
            estimated_minutes=45,
            assign_to=self.worker,
            requested_by=self.supervisor,
            recurrence_pattern="weekly",
            recurrence_interval=1,
        )
        self.second_template = RecurringTaskTemplate.objects.create(
            title="Daily check-in",
            description="Review and post the daily operations check-in.",
            priority=Priority.LOW,
            estimated_minutes=15,
            assign_to=self.worker,
            requested_by=self.supervisor,
            recurrence_pattern="daily",
            recurrence_interval=1,
        )
        self.client.force_login(self.supervisor)

    def test_recurring_page_lists_existing_templates_as_cards(self):
        response = self.client.get(reverse("recurring-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recurring tasks")
        self.assertContains(response, "Weekly mail run")
        self.assertContains(response, "Pick up and sort campus mail.")
        self.assertContains(response, "Assigned to: Jamie Worker")
        self.assertContains(response, 'data-template-id="%s"' % self.first_template.pk)
        self.assertContains(response, reverse("recurring-detail", args=[self.first_template.pk]))
        self.assertNotContains(response, "Create recurring task template")
        self.assertNotContains(response, "Save template")

    def test_recurring_page_backfills_standalone_recurring_tasks(self):
        task = Task.objects.create(
            title="Standalone recurring cleanup",
            description="Weekly cleanup task",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            due_date=date(2026, 3, 17),
            assigned_to=self.worker,
            requested_by=self.supervisor,
            created_by=self.supervisor,
            recurring_task=True,
            recurrence_pattern="weekly",
            recurrence_interval=1,
            recurrence_day_of_week=Weekday.TUESDAY,
        )

        response = self.client.get(reverse("recurring-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Standalone recurring cleanup")
        task.refresh_from_db()
        self.assertIsNotNone(task.recurring_template)
        self.assertEqual(task.recurring_template.assign_to, self.worker)

    def test_recurring_page_shows_additional_teammates_summary(self):
        self.first_template.additional_assignees.add(self.helper)
        self.first_template.rotating_additional_assignee_count = 1
        self.first_template.rotate_additional_assignee = True
        self.first_template.save(update_fields=["rotating_additional_assignee_count", "rotate_additional_assignee", "updated_at"])

        response = self.client.get(reverse("recurring-list"))

        self.assertContains(response, "Additional teammates: Casey Helper, Rotation x1")

    def test_recurring_detail_page_renders_template_details(self):
        response = self.client.get(reverse("recurring-detail", args=[self.first_template.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Weekly mail run")
        self.assertContains(response, "Recurring details")
        self.assertContains(response, "Next run preview")
        self.assertContains(response, "Upcoming run dates")
        self.assertContains(response, "Run now")
        self.assertContains(response, "Jamie Worker is the fixed assignee for the next run.")

    def test_recurring_detail_links_to_edit_page(self):
        detail_response = self.client.get(reverse("recurring-detail", args=[self.first_template.pk]))

        self.assertContains(detail_response, reverse("recurring-edit", args=[self.first_template.pk]))

        edit_response = self.client.get(reverse("recurring-edit", args=[self.first_template.pk]))
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, "Edit recurring task")
        self.assertContains(edit_response, "Save changes")
        self.assertContains(edit_response, reverse("recurring-delete", args=[self.first_template.pk]))
        self.assertContains(edit_response, "Delete recurring task")


    def test_recurring_delete_view_converts_generated_tasks_to_regular_tasks(self):
        generated_task = Task.objects.create(
            title="Generated recurring run",
            description="Already created from the template",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.worker,
            recurring_task=True,
            recurring_template=self.first_template,
            recurrence_pattern="weekly",
            recurrence_interval=1,
            recurrence_day_of_week=Weekday.MONDAY,
        )

        response = self.client.post(reverse("recurring-delete", args=[self.first_template.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, reverse("recurring-list"))
        self.assertFalse(RecurringTaskTemplate.objects.filter(pk=self.first_template.pk).exists())
        generated_task.refresh_from_db()
        self.assertIsNone(generated_task.recurring_template)
        self.assertFalse(generated_task.recurring_task)
        self.assertEqual(generated_task.recurrence_pattern, "")
        self.assertIsNone(generated_task.recurrence_interval)
        self.assertIsNone(generated_task.recurrence_day_of_week)
        self.assertIsNone(generated_task.recurrence_day_of_month)
        self.assertNotContains(response, "Generated recurring run")
        self.assertContains(response, "Recurring task removed.")

    def test_recurring_run_now_creates_task_using_template_next_run_date(self):
        self.first_template.next_run_date = date(2026, 3, 27)
        self.first_template.save(update_fields=["next_run_date", "updated_at"])

        with patch("workboard.recurring_service.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 12, 0))):
            response = self.client.post(reverse("recurring-run-now", args=[self.first_template.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        generated = Task.objects.filter(recurring_template=self.first_template).latest("pk")
        self.first_template.refresh_from_db()
        self.assertEqual(generated.due_date, date(2026, 3, 27))
        self.assertEqual(generated.assigned_to, self.worker)
        self.assertEqual(self.first_template.next_run_date, date(2026, 4, 3))
        self.assertContains(response, "Recurring task queued for 2026-03-27.")

    def test_recurring_run_now_warns_when_current_run_is_still_open(self):
        Task.objects.create(
            title="Open recurring run",
            description="Still in progress",
            priority=Priority.MEDIUM,
            status=TaskStatus.IN_PROGRESS,
            assigned_to=self.worker,
            recurring_task=True,
            recurring_template=self.first_template,
        )

        with patch("workboard.recurring_service.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 12, 0))):
            response = self.client.post(reverse("recurring-run-now", args=[self.first_template.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already has an open run on the board")
        self.assertEqual(Task.objects.filter(recurring_template=self.first_template).count(), 1)

    def test_recurring_edit_page_uses_clear_schedule_labels(self):
        response = self.client.get(reverse("recurring-edit", args=[self.first_template.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Task details")
        self.assertContains(response, "Time estimate")
        self.assertContains(response, "Repeat cadence")
        self.assertContains(response, "Repeat every")
        self.assertContains(response, "Weekday to repeat on")
        self.assertContains(response, "Select a weekday")
        self.assertContains(response, "Monday")
        self.assertContains(response, "Sunday")
        self.assertNotContains(response, 'type="number" name="day_of_week"', html=False)
        self.assertContains(response, 'value="07:00"')
        self.assertContains(response, 'value="18:00"')
        self.assertNotContains(response, 'value="06:30"')
        self.assertNotContains(response, 'value="18:30"')
        self.assertContains(response, "Day of month to repeat on")
        self.assertContains(response, "Fixed additional assignees")
        self.assertContains(response, "Add rotating team members")
        self.assertContains(response, "Recurring task is active")
    def test_recurring_move_view_reorders_templates(self):
        response = self.client.post(
            reverse("recurring-move", args=[self.second_template.pk]),
            {"before_template_id": str(self.first_template.pk)},
        )

        self.assertEqual(response.status_code, 200)
        self.first_template.refresh_from_db()
        self.second_template.refresh_from_db()
        self.assertEqual(self.second_template.display_order, 1)
        self.assertEqual(self.first_template.display_order, 2)

class TaskAssignmentServiceTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username="supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
        )
        self.alex = self._create_worker("alex", "Alex Carter")
        self.jordan = self._create_worker("jordan", "Jordan Lee")

    def _create_worker(self, username, display_name, weekday_hours=4, role=UserRole.STUDENT_WORKER):
        user = User.objects.create_user(
            username=username,
            password="password123",
            role=role,
        )
        profile = StudentWorkerProfile.objects.create(
            user=user,
            display_name=display_name,
            email=f"{username}@example.com",
            normal_shift_availability="Weekdays",
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
        self.assertIn("Suggested worker", summary)
        self.assertIn("Jordan Lee", rationale[0])

    def test_suggest_assignee_only_considers_workers_with_required_tags(self):
        specialist_tag = WorkerTag.objects.create(name="Front Desk", team=self.alex.team)
        self.jordan.worker_profile.tags.add(specialist_tag)

        with patch("workboard.services.timezone.localdate", return_value=date(2026, 3, 13)):
            assignee, summary, rationale = TaskAssignmentService.suggest_assignee(
                due_date=date(2026, 3, 17),
                estimated_minutes=30,
                fallback_supervisor=self.supervisor,
                required_tag_ids=[specialist_tag.pk],
            )

        self.assertEqual(assignee, self.jordan)
        self.assertIn("required worker tags", summary)
        self.assertIn("Worker tag filters were applied", rationale[-1])

    def test_suggest_assignee_falls_back_to_requesting_supervisor_when_students_cannot_fit_work(self):
        for profile in StudentWorkerProfile.objects.all():
            profile.weekly_availability.all().update(hours_available=0)

        with patch("workboard.services.timezone.localdate", return_value=date(2026, 3, 13)):
            assignee, summary, rationale = TaskAssignmentService.suggest_assignee(
                due_date=date(2026, 3, 13),
                estimated_minutes=180,
                fallback_supervisor=self.supervisor,
            )

        self.assertEqual(assignee, self.supervisor)
        self.assertIn("stay with the supervising user", summary)
        self.assertIn("Fallback rule assigned the task to the supervising user instead of rotating among supervisors.", rationale)

    def test_student_supervisor_stays_in_worker_rotation(self):
        lead = self._create_worker("lead-student-supervisor", "Morgan Lead", role=UserRole.STUDENT_SUPERVISOR)
        StudentWorkerProfile.objects.exclude(user=lead).update(active_status=False)

        with patch("workboard.services.timezone.localdate", return_value=date(2026, 3, 13)):
            assignee, summary, rationale = TaskAssignmentService.suggest_assignee(
                due_date=date(2026, 3, 17),
                estimated_minutes=30,
                fallback_supervisor=self.supervisor,
            )

        self.assertEqual(assignee, lead)
        self.assertIn("Suggested worker", summary)
        self.assertIn("Morgan Lead", rationale[0])

    def test_same_day_assignment_skips_worker_without_enough_time_left_and_uses_next_worker(self):
        Task.objects.create(
            title="Already booked",
            description="Consumes Alex's remaining time today",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.alex,
            estimated_minutes=30,
            due_date=date(2026, 3, 13),
        )

        with (
            patch("workboard.services.timezone.localdate", return_value=date(2026, 3, 13)),
            patch(
                "workboard.services.timezone.localtime",
                return_value=timezone.make_aware(datetime(2026, 3, 13, 16, 0)),
            ),
        ):
            assignee, _, _ = TaskAssignmentService.suggest_assignee(
                due_date=date(2026, 3, 13),
                estimated_minutes=45,
                fallback_supervisor=self.supervisor,
            )

        self.assertEqual(assignee, self.jordan)

    def test_same_day_assignment_after_5pm_falls_back_to_supervisor(self):
        with (
            patch("workboard.services.timezone.localdate", return_value=date(2026, 3, 13)),
            patch(
                "workboard.services.timezone.localtime",
                return_value=timezone.make_aware(datetime(2026, 3, 13, 17, 5)),
            ),
        ):
            assignee, summary, rationale = TaskAssignmentService.suggest_assignee(
                due_date=date(2026, 3, 13),
                estimated_minutes=30,
                fallback_supervisor=self.supervisor,
            )

        self.assertEqual(assignee, self.supervisor)
        self.assertIn("stay with the supervising user", summary)
        self.assertIn("Fallback rule assigned the task to the supervising user instead of rotating among supervisors.", rationale)


    def test_suggest_assignee_does_not_rotate_to_other_supervisors(self):
        User.objects.create_user(
            username="backup-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
            assignable_to_tasks=True,
        )
        for profile in StudentWorkerProfile.objects.all():
            profile.weekly_availability.all().update(hours_available=0)

        with patch("workboard.services.timezone.localdate", return_value=date(2026, 3, 13)):
            assignee, summary, rationale = TaskAssignmentService.suggest_assignee(
                due_date=date(2026, 3, 13),
                estimated_minutes=180,
                fallback_supervisor=self.supervisor,
            )

        self.assertEqual(assignee, self.supervisor)
        self.assertIn("stay with the supervising user", summary)
        self.assertIn("Fallback rule assigned the task to the supervising user instead of rotating among supervisors.", rationale)


class RecurringTaskGenerationRotationTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="recurring-gen-sup", password="password123", role=UserRole.SUPERVISOR)
        self.alex = User.objects.create_user(username="recurring-gen-alex", password="password123", role=UserRole.STUDENT_WORKER)
        self.jordan = User.objects.create_user(username="recurring-gen-jordan", password="password123", role=UserRole.STUDENT_WORKER)
        self.sam = User.objects.create_user(username="recurring-gen-sam", password="password123", role=UserRole.STUDENT_WORKER)
        self.alex_profile = StudentWorkerProfile.objects.create(
            user=self.alex,
            display_name="Alex Carter",
            email="alex-rotation@example.com",
            normal_shift_availability="Weekdays",
        )
        self.jordan_profile = StudentWorkerProfile.objects.create(
            user=self.jordan,
            display_name="Jordan Lee",
            email="jordan-rotation@example.com",
            normal_shift_availability="Weekdays",
        )
        self.sam_profile = StudentWorkerProfile.objects.create(
            user=self.sam,
            display_name="Sam Patel",
            email="sam-rotation@example.com",
            normal_shift_availability="Weekdays",
        )
        for profile in (self.alex_profile, self.jordan_profile, self.sam_profile):
            for weekday in Weekday.values:
                StudentAvailability.objects.create(
                    profile=profile,
                    weekday=weekday,
                    hours_available=4 if weekday < 5 else 0,
                )
        self.template = RecurringTaskTemplate.objects.create(
            title="Rotating recurring task",
            description="Rotate me",
            priority=Priority.MEDIUM,
            estimated_minutes=30,
            assign_to=None,
            requested_by=self.supervisor,
            recurrence_pattern="weekly",
            recurrence_interval=1,
            next_run_date=date(2026, 3, 20),
        )
        self.previous_task = Task.objects.create(
            title="Rotating recurring task",
            description="Previous run",
            raw_message="Take out the trash and notify Facilities.",
            priority=Priority.MEDIUM,
            status=TaskStatus.DONE,
            due_date=date(2026, 3, 13),
            estimated_minutes=30,
            assigned_to=self.alex,
            requested_by=self.supervisor,
            created_by=self.supervisor,
            recurring_task=True,
            recurring_template=self.template,
            recurrence_pattern="weekly",
            recurrence_interval=1,
            recurrence_day_of_week=Weekday.FRIDAY,
            respond_to_text="Facilities",
            completed_at=timezone.make_aware(datetime(2026, 3, 13, 17, 30)),
        )
        TaskChecklistItem.objects.create(task=self.previous_task, title="Recurring step", is_completed=True, position=1)
        self.client.force_login(self.supervisor)

    def _run_generator_at(self, when):
        with patch("workboard.management.commands.generate_recurring_tasks.timezone.now", return_value=when):
            call_command("generate_recurring_tasks")

    def _load_board_at(self, when):
        with patch("workboard.recurring_service.timezone.now", return_value=when):
            return self.client.get(reverse("board"))

    def test_generate_recurring_tasks_creates_new_task_after_evening_cutoff(self):
        self.sam_profile.active_status = False
        self.sam_profile.save(update_fields=["active_status"])

        self._run_generator_at(timezone.make_aware(datetime(2026, 3, 13, 18, 5)))

        self.previous_task.refresh_from_db()
        self.template.refresh_from_db()
        tasks = list(Task.objects.filter(recurring_template=self.template).order_by("due_date", "pk"))
        self.assertEqual(len(tasks), 2)
        next_task = tasks[-1]
        self.assertEqual(self.previous_task.status, TaskStatus.DONE)
        self.assertEqual(self.previous_task.due_date, date(2026, 3, 13))
        self.assertEqual(next_task.status, TaskStatus.NEW)
        self.assertEqual(next_task.due_date, date(2026, 3, 20))
        self.assertEqual(next_task.assigned_to, self.jordan)
        self.assertEqual(next_task.created_by, self.supervisor)
        self.assertEqual(next_task.raw_message, self.previous_task.raw_message)
        self.assertEqual(next_task.respond_to_text, "Facilities")
        self.assertEqual(list(next_task.checklist_items.values_list("title", flat=True)), ["Recurring step"])
        self.assertFalse(next_task.checklist_items.get().is_completed)
        self.assertEqual(self.template.next_run_date, date(2026, 3, 27))

    def test_generate_recurring_tasks_keeps_open_run_visible_and_creates_next_cycle(self):
        self.sam_profile.active_status = False
        self.sam_profile.save(update_fields=["active_status"])
        self.previous_task.status = TaskStatus.IN_PROGRESS
        self.previous_task.completed_at = None
        self.previous_task.save(update_fields=["status", "completed_at", "updated_at"])

        self._run_generator_at(timezone.make_aware(datetime(2026, 3, 13, 18, 5)))

        self.previous_task.refresh_from_db()
        self.template.refresh_from_db()
        tasks = list(Task.objects.filter(recurring_template=self.template).order_by("due_date", "pk"))
        self.assertEqual(len(tasks), 2)
        next_task = tasks[-1]
        self.assertEqual(self.previous_task.status, TaskStatus.IN_PROGRESS)
        self.assertIsNone(self.previous_task.completed_at)
        self.assertEqual(next_task.status, TaskStatus.NEW)
        self.assertEqual(next_task.due_date, date(2026, 3, 20))
        self.assertEqual(next_task.assigned_to, self.jordan)
        self.assertEqual(self.template.next_run_date, date(2026, 3, 27))

    def test_request_driven_rollover_waits_until_evening_cutoff(self):
        response = self._load_board_at(timezone.make_aware(datetime(2026, 3, 13, 17, 0)))

        self.assertEqual(response.status_code, 200)
        self.previous_task.refresh_from_db()
        self.template.refresh_from_db()
        self.assertEqual(Task.objects.filter(recurring_template=self.template).count(), 1)
        self.assertEqual(self.previous_task.status, TaskStatus.DONE)
        self.assertEqual(self.template.next_run_date, date(2026, 3, 20))

    def test_request_driven_rollover_creates_next_cycle_after_evening_cutoff(self):
        self.sam_profile.active_status = False
        self.sam_profile.save(update_fields=["active_status"])

        response = self._load_board_at(timezone.make_aware(datetime(2026, 3, 13, 18, 5)))

        self.assertEqual(response.status_code, 200)
        self.previous_task.refresh_from_db()
        self.template.refresh_from_db()
        tasks = list(Task.objects.filter(recurring_template=self.template).order_by("due_date", "pk"))
        self.assertEqual(len(tasks), 2)
        self.assertEqual(self.previous_task.status, TaskStatus.DONE)
        self.assertEqual(tasks[-1].due_date, date(2026, 3, 20))
        self.assertEqual(tasks[-1].assigned_to, self.jordan)
        self.assertEqual(self.template.next_run_date, date(2026, 3, 27))

    def test_generate_recurring_tasks_sets_fixed_and_rotating_additional_assignees(self):
        extra_template = RecurringTaskTemplate.objects.create(
            title="Recurring team task",
            description="Needs backup help",
            priority=Priority.MEDIUM,
            estimated_minutes=30,
            assign_to=self.alex,
            requested_by=self.supervisor,
            recurrence_pattern="weekly",
            recurrence_interval=1,
            next_run_date=date(2026, 3, 20),
            rotating_additional_assignee_count=1,
            rotate_additional_assignee=True,
        )
        extra_template.additional_assignees.add(self.jordan)

        self._run_generator_at(timezone.make_aware(datetime(2026, 3, 13, 18, 5)))

        generated = Task.objects.filter(recurring_template=extra_template).latest("pk")
        self.assertEqual(generated.assigned_to, self.alex)
        self.assertEqual(generated.due_date, date(2026, 3, 20))
        self.assertEqual(list(generated.additional_assignees.values_list("id", flat=True)), [self.jordan.id])
        self.assertEqual(generated.rotating_additional_assignee_count, 1)
        self.assertEqual(list(generated.rotating_additional_assignees.values_list("id", flat=True)), [self.sam.id])

    def test_generate_recurring_tasks_backfills_every_release_window_that_has_passed(self):
        self.sam_profile.active_status = False
        self.sam_profile.save(update_fields=["active_status"])

        self._run_generator_at(timezone.make_aware(datetime(2026, 3, 27, 18, 5)))

        self.template.refresh_from_db()
        due_dates = list(Task.objects.filter(recurring_template=self.template).order_by("due_date", "pk").values_list("due_date", flat=True))
        self.assertEqual(
            due_dates,
            [date(2026, 3, 13), date(2026, 3, 20), date(2026, 3, 27), date(2026, 4, 3)],
        )
        self.assertEqual(self.template.next_run_date, date(2026, 4, 10))

    def test_generate_recurring_tasks_backfills_legacy_recurring_tasks(self):
        legacy_task = Task.objects.create(
            title="Legacy recurring cleanup",
            description="Older recurring task without a template",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            due_date=date(2026, 3, 17),
            assigned_to=self.alex,
            requested_by=self.supervisor,
            created_by=self.supervisor,
            recurring_task=True,
            recurrence_pattern="weekly",
            recurrence_interval=1,
            recurrence_day_of_week=Weekday.TUESDAY,
        )

        self._run_generator_at(timezone.make_aware(datetime(2026, 3, 13, 8, 0)))

        legacy_task.refresh_from_db()
        self.assertIsNotNone(legacy_task.recurring_template)
        self.assertEqual(legacy_task.recurring_template.assign_to, self.alex)


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
        self.worker = User.objects.create_user(
            username="create-worker",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            first_name="Taylor",
            last_name="Worker",
        )
        StudentWorkerProfile.objects.create(
            user=self.worker,
            display_name="Taylor Worker",
            email="taylor.worker@example.com",
            normal_shift_availability="",
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

    def test_direct_task_create_builds_recurring_template_when_enabled(self):
        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Weekly clean up",
                "raw_message": "",
                "description": "Recurring weekly clean up",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "2026-03-16",
                "raw_due_text": "",
                "waiting_person": "",
                "respond_to_text": "",
                "estimated_minutes": "45",
                "assigned_to": str(self.worker.pk),
                "requested_by": str(self.supervisor.pk),
                "recurring_task": "on",
                "recurrence_pattern": "weekly",
                "recurrence_interval": "1",
                "recurrence_day_of_week": str(Weekday.MONDAY),
                "recurrence_day_of_month": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        task = Task.objects.get(title="Weekly clean up", due_date=date(2026, 3, 16))
        self.assertIsNotNone(task.recurring_template)
        self.assertEqual(task.recurring_template.assign_to, self.worker)
        self.assertGreaterEqual(task.recurring_template.next_run_date, date(2026, 3, 23))

    def test_direct_task_create_carries_required_worker_tags_to_recurring_template(self):
        specialist_tag = WorkerTag.objects.create(name="Front Desk", team=self.worker.team)
        self.worker.worker_profile.tags.add(specialist_tag)

        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Tagged weekly clean up",
                "raw_message": "",
                "description": "Recurring weekly clean up with tags",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "2026-03-16",
                "raw_due_text": "",
                "waiting_person": "",
                "respond_to_text": "",
                "estimated_minutes": "45",
                "required_worker_tags": [str(specialist_tag.pk)],
                "assigned_to": str(self.worker.pk),
                "requested_by": str(self.supervisor.pk),
                "recurring_task": "on",
                "recurrence_pattern": "weekly",
                "recurrence_interval": "1",
                "recurrence_day_of_week": str(Weekday.MONDAY),
                "recurrence_day_of_month": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        task = Task.objects.get(title="Tagged weekly clean up", due_date=date(2026, 3, 16))
        self.assertEqual(list(task.required_worker_tags.values_list("pk", flat=True)), [specialist_tag.pk])
        self.assertEqual(list(task.recurring_template.required_worker_tags.values_list("pk", flat=True)), [specialist_tag.pk])


class TaskScheduledWindowTests(TestCase):
    def setUp(self):
        fixed_now = timezone.make_aware(datetime(2026, 3, 16, 8, 0))
        self.localdate_patcher = patch("django.utils.timezone.localdate", return_value=date(2026, 3, 16))
        self.localtime_patcher = patch("django.utils.timezone.localtime", return_value=fixed_now)
        self.localdate_patcher.start()
        self.localtime_patcher.start()
        self.addCleanup(self.localtime_patcher.stop)
        self.addCleanup(self.localdate_patcher.stop)

        self.supervisor = User.objects.create_user(
            username="scheduled-window-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
        )
        self.morning_worker = User.objects.create_user(
            username="morning-worker",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            first_name="Morning",
            last_name="Worker",
        )
        self.afternoon_worker = User.objects.create_user(
            username="afternoon-worker",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            first_name="Afternoon",
            last_name="Worker",
        )
        self._create_schedule(self.morning_worker, time(9, 0), time(12, 0))
        self._create_schedule(self.afternoon_worker, time(13, 0), time(17, 0))
        self.client.force_login(self.supervisor)

    def _create_schedule(self, user, start_value, end_value):
        profile = StudentWorkerProfile.objects.create(
            user=user,
            display_name=user.get_full_name(),
            email=f"{user.username}@example.com",
            normal_shift_availability="",
        )
        for weekday in Weekday.values:
            availability = StudentAvailability.objects.create(
                profile=profile,
                weekday=weekday,
                start_time=start_value if weekday < 5 else None,
                end_time=end_value if weekday < 5 else None,
                hours_available=3 if weekday < 5 else 0,
            )
            if weekday < 5 and start_value and end_value:
                StudentAvailabilityBlock.objects.create(
                    availability=availability,
                    start_time=start_value,
                    end_time=end_value,
                    position=1,
                )

    def _replace_blocks(self, user, weekday, blocks):
        availability = user.worker_profile.weekly_availability.get(weekday=weekday)
        availability.blocks.all().delete()
        availability.start_time = blocks[0][0] if blocks else None
        availability.end_time = blocks[-1][1] if blocks else None
        availability.hours_available = sum((datetime.combine(date.today(), end_value) - datetime.combine(date.today(), start_value)).total_seconds() // 60 for start_value, end_value in blocks) / 60 if blocks else 0
        availability.save(update_fields=["start_time", "end_time", "hours_available"])
        for position, (start_value, end_value) in enumerate(blocks, start=1):
            StudentAvailabilityBlock.objects.create(
                availability=availability,
                start_time=start_value,
                end_time=end_value,
                position=position,
            )

    def test_task_create_page_uses_task_window_picker(self):
        response = self.client.get(reverse("task-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Required worker tags")
        self.assertContains(response, "Scheduled work window")
        self.assertContains(response, 'data-task-window-toggle', html=False)
        self.assertContains(response, 'data-task-window-fields style="display: none;"', html=False)
        self.assertNotContains(response, "Show week of")
        self.assertContains(response, 'data-schedule-summary-card="task_window_day_0"')
        self.assertContains(response, 'data-schedule-summary-card="task_window_day_4"')
        self.assertNotContains(response, 'data-schedule-summary-card="task_window_day_5"')
        self.assertNotContains(response, 'data-schedule-summary-card="task_window_day_6"')
        self.assertContains(response, 'data-slot-value="07:00"')
        self.assertContains(response, 'data-slot-end="18:00"')
        self.assertNotContains(response, 'data-slot-value="06:30"')
        self.assertNotContains(response, 'data-slot-end="18:30"')
        self.assertContains(response, 'Select a weekday')
        self.assertContains(response, 'Monday')
        self.assertContains(response, 'Friday')
        self.assertNotContains(response, 'type="number" name="recurrence_day_of_week"', html=False)
        self.assertNotContains(response, '<label for="id_scheduled_start_time">Start time</label>', html=True)
        self.assertNotContains(response, '<label for="id_scheduled_end_time">End time</label>', html=True)

    def test_task_create_rejects_task_window_outside_student_workday(self):
        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Late shift coverage",
                "description": "Should not be schedulable after hours.",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "",
                "scheduled_week_of": "2026-03-16",
                "task_window_day_2_segments": "[[\"18:00\", \"19:00\"]]",
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
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Task windows must stay between 7:00 AM and 6:00 PM.")
        self.assertFalse(Task.objects.filter(title="Late shift coverage").exists())

    def test_task_create_accepts_task_window_segments_payload(self):
        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Segment scheduled coverage",
                "description": "Cover the front desk in the morning.",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "",
                "scheduled_week_of": "2026-03-16",
                "task_window_day_2_segments": '[["09:30", "10:30"]]',
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
        task = Task.objects.get(title="Segment scheduled coverage")
        self.assertEqual(task.assigned_to, self.morning_worker)
        self.assertEqual(task.scheduled_date, date(2026, 3, 18))
        self.assertEqual(task.scheduled_start_time, time(9, 30))
        self.assertEqual(task.scheduled_end_time, time(10, 30))
        self.assertEqual(task.due_date, date(2026, 3, 18))

    def test_task_create_auto_assigns_worker_available_in_scheduled_window(self):
        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Scheduled front desk coverage",
                "description": "Cover the front desk in the morning.",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "",
                "scheduled_week_of": "2026-03-16",
                "task_window_day_0_segments": '[["09:30", "10:30"]]',
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
        task = Task.objects.get(title="Scheduled front desk coverage")
        self.assertEqual(task.assigned_to, self.morning_worker)
        self.assertEqual(task.scheduled_date, date(2026, 3, 16))
        self.assertEqual(task.scheduled_start_time, time(9, 30))
        self.assertEqual(task.scheduled_end_time, time(10, 30))
        self.assertEqual(task.due_date, date(2026, 3, 16))

    def test_task_create_auto_assigns_only_workers_with_required_tags(self):
        specialist_tag = WorkerTag.objects.create(name="Front Desk", team=self.morning_worker.team)
        self.morning_worker.worker_profile.tags.add(specialist_tag)

        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Tagged front desk coverage",
                "description": "Cover the front desk with a tagged teammate.",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "2026-03-17",
                "respond_to_text": "",
                "estimated_minutes": "30",
                "required_worker_tags": [str(specialist_tag.pk)],
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
        task = Task.objects.get(title="Tagged front desk coverage")
        self.assertEqual(task.assigned_to, self.morning_worker)
        self.assertEqual(list(task.required_worker_tags.values_list("pk", flat=True)), [specialist_tag.pk])

    def test_task_create_rejects_manual_assignee_missing_required_tags(self):
        specialist_tag = WorkerTag.objects.create(name="Front Desk", team=self.morning_worker.team)
        self.morning_worker.worker_profile.tags.add(specialist_tag)

        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Tagged phone shift",
                "description": "Tagged task with the wrong teammate selected.",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "2026-03-17",
                "respond_to_text": "",
                "estimated_minutes": "30",
                "required_worker_tags": [str(specialist_tag.pk)],
                "assigned_to": str(self.afternoon_worker.pk),
                "requested_by": "",
                "recurring_task": "",
                "recurrence_pattern": "",
                "recurrence_interval": "",
                "recurrence_day_of_week": "",
                "recurrence_day_of_month": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "missing these required worker tags")
        self.assertFalse(Task.objects.filter(title="Tagged phone shift").exists())

    def test_task_create_rejects_manual_assignee_outside_scheduled_window(self):
        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Scheduled phone shift",
                "description": "Morning phone shift.",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "2026-03-16",
                "scheduled_week_of": "2026-03-16",
                "task_window_day_0_segments": '[["09:30", "10:30"]]',
                "respond_to_text": "",
                "estimated_minutes": "30",
                "assigned_to": str(self.afternoon_worker.pk),
                "requested_by": "",
                "recurring_task": "",
                "recurrence_pattern": "",
                "recurrence_interval": "",
                "recurrence_day_of_week": "",
                "recurrence_day_of_month": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "does not have enough scheduled availability during those task windows")
        self.assertFalse(Task.objects.filter(title="Scheduled phone shift").exists())

    def test_task_create_accepts_multiple_task_windows_and_uses_last_window_as_due_date(self):
        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Multi-window lab coverage",
                "description": "Can be completed during two morning windows.",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "",
                "scheduled_week_of": "2026-03-16",
                "task_window_day_0_segments": '[["09:00", "10:00"]]',
                "task_window_day_2_segments": '[["09:00", "10:30"]]',
                "respond_to_text": "",
                "estimated_minutes": "120",
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
        task = Task.objects.get(title="Multi-window lab coverage")
        self.assertEqual(task.assigned_to, self.morning_worker)
        self.assertEqual(task.scheduled_date, date(2026, 3, 16))
        self.assertEqual(task.due_date, date(2026, 3, 18))
        self.assertEqual(task.scheduled_blocks.count(), 2)
        self.assertEqual(
            list(task.scheduled_blocks.values_list("work_date", "start_time", "end_time")),
            [
                (date(2026, 3, 16), time(9, 0), time(10, 0)),
                (date(2026, 3, 18), time(9, 0), time(10, 30)),
            ],
        )

    def test_task_create_checks_total_available_minutes_inside_task_windows(self):
        Task.objects.create(
            title="Existing morning commitment",
            description="Already using the Monday task window.",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.morning_worker,
            due_date=date(2026, 3, 16),
            estimated_minutes=60,
            scheduled_date=date(2026, 3, 16),
            scheduled_start_time=time(9, 0),
            scheduled_end_time=time(10, 0),
        )

        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Needs more window time",
                "description": "Two windows exist, but one is already consumed.",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "",
                "scheduled_week_of": "2026-03-16",
                "task_window_day_0_segments": '[["09:00", "10:00"]]',
                "task_window_day_2_segments": '[["09:00", "10:00"]]',
                "respond_to_text": "",
                "estimated_minutes": "90",
                "assigned_to": str(self.morning_worker.pk),
                "requested_by": "",
                "recurring_task": "",
                "recurrence_pattern": "",
                "recurrence_interval": "",
                "recurrence_day_of_week": "",
                "recurrence_day_of_month": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "does not have enough scheduled availability during those task windows")
        self.assertFalse(Task.objects.filter(title="Needs more window time").exists())

    def test_task_create_syncs_weekly_recurring_day_from_task_window(self):
        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Friday closing checklist",
                "description": "Wrap up the lab each Friday.",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "",
                "scheduled_week_of": "2026-03-16",
                "task_window_day_4_segments": '[["08:00", "17:00"]]',
                "respond_to_text": "",
                "estimated_minutes": "60",
                "assigned_to": "",
                "requested_by": "",
                "recurring_task": "on",
                "recurrence_pattern": "weekly",
                "recurrence_interval": "1",
                "recurrence_day_of_week": "",
                "recurrence_day_of_month": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        task = Task.objects.get(title="Friday closing checklist")
        self.assertTrue(task.recurring_task)
        self.assertEqual(task.scheduled_date, date(2026, 3, 20))
        self.assertEqual(task.recurrence_day_of_week, Weekday.FRIDAY)
        self.assertIsNotNone(task.recurring_template)
        self.assertEqual(task.recurring_template.day_of_week, Weekday.FRIDAY)
        self.assertEqual(task.recurring_template.next_run_date, date(2026, 3, 27))

    def test_split_shift_worker_is_available_inside_second_block_but_not_gap(self):
        self._replace_blocks(
            self.morning_worker,
            Weekday.MONDAY,
            [(time(9, 0), time(11, 0)), (time(13, 0), time(15, 0))],
        )

        self.assertFalse(
            TaskAssignmentService.user_is_available_for_window(
                self.morning_worker,
                scheduled_date=date(2026, 3, 16),
                scheduled_start_time=time(11, 30),
                scheduled_end_time=time(12, 30),
            )
        )
        self.assertTrue(
            TaskAssignmentService.user_is_available_for_window(
                self.morning_worker,
                scheduled_date=date(2026, 3, 16),
                scheduled_start_time=time(13, 30),
                scheduled_end_time=time(14, 30),
            )
        )


    def test_worker_create_accepts_start_and_end_schedule_fields(self):
        response = self.client.post(
            reverse("worker-create"),
            {
                "username": "schedule-student",
                "password": "password123",
                "first_name": "Taylor",
                "last_name": "Schedule",
                "email": "",
                "active_status": "on",
                "skill_notes": "",
                "monday_start": "09:00",
                "monday_end": "12:00",
                "tuesday_start": "10:00",
                "tuesday_end": "14:00",
                "wednesday_start": "",
                "wednesday_end": "",
                "thursday_start": "",
                "thursday_end": "",
                "friday_start": "",
                "friday_end": "",
                "saturday_start": "",
                "saturday_end": "",
                "sunday_start": "",
                "sunday_end": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        profile = User.objects.get(username="schedule-student").worker_profile
        monday = profile.weekly_availability.get(weekday=Weekday.MONDAY)
        tuesday = profile.weekly_availability.get(weekday=Weekday.TUESDAY)
        self.assertEqual(monday.start_time, time(9, 0))
        self.assertEqual(monday.end_time, time(12, 0))
        self.assertEqual(float(monday.hours_available), 3.0)
        self.assertEqual(tuesday.start_time, time(10, 0))
        self.assertEqual(tuesday.end_time, time(14, 0))
        self.assertEqual(float(tuesday.hours_available), 4.0)


class TaskCreateLabelTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username="create-label-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
            first_name="Avery",
            last_name="Stone",
        )
        self.student = User.objects.create_user(
            username="alex-worker",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            first_name="Alex",
            last_name="Johnson",
        )
        StudentWorkerProfile.objects.create(
            user=self.student,
            display_name="Alex Johnson",
            email="alex@example.com",
            normal_shift_availability="",
        )
        self.client.force_login(self.supervisor)

    def test_task_create_uses_full_names_in_user_dropdowns(self):
        response = self.client.get(reverse("task-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alex Johnson")
        self.assertContains(response, "Avery Stone")
        self.assertContains(response, "Assign to")
        self.assertContains(response, "Fixed additional assignees")
        self.assertContains(response, "Add rotating team members")
        self.assertContains(response, "Scheduled work window")
        self.assertNotContains(response, "Show week of")
        self.assertContains(response, 'data-schedule-summary-card="task_window_day_0"')
        self.assertContains(response, 'data-schedule-summary-card="task_window_day_4"')
        self.assertNotContains(response, 'data-schedule-summary-card="task_window_day_5"')
        self.assertNotContains(response, "Scheduled date")
        self.assertNotContains(response, "Start time")
        self.assertNotContains(response, "End time")
        self.assertContains(response, 'name="additional_assignees"', count=1)
        self.assertContains(response, 'name="rotating_additional_assignee_count"')
        self.assertContains(response, 'type="checkbox"', html=False)
        self.assertNotContains(response, 'data-recurring-toggle', html=False)
        self.assertContains(response, 'data-recurring-fields style="display: none;"', html=False)
        self.assertNotContains(response, '<select name="additional_assignees"', html=False)
        self.assertNotContains(response, "Requested by")
        self.assertNotContains(response, ">alex-worker<", html=False)
        self.assertNotContains(response, ">create-label-supervisor<", html=False)

    def test_task_create_hides_extra_helper_copy(self):
        response = self.client.get(reverse("task-create"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Choose which team owns this task.")
        self.assertNotContains(response, "Describe the work that needs to be done.")
        self.assertNotContains(response, "Leave blank if this can be scheduled from priority.")
        self.assertNotContains(response, "Person or office to notify after the task is complete")
        self.assertNotContains(response, "Choose the main teammate for this task.")
        self.assertNotContains(response, "Pick any teammates who should always be added to this task.")
        self.assertNotContains(response, "Choose any Monday through Friday times this task can be worked. TaskForge uses these windows to find teammates who have availability and to place the task inside those hours.")
        self.assertNotContains(response, "Turn this on only if this task should repeat automatically.")

    def test_task_edit_hides_extra_helper_copy(self):
        task = Task.objects.create(
            title="Edit helper text task",
            description="Testing edit helper text cleanup",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.student,
            created_by=self.supervisor,
        )

        response = self.client.get(reverse("task-edit", args=[task.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Choose which team owns this task.")
        self.assertNotContains(response, "Describe the work that needs to be done.")
        self.assertNotContains(response, "Leave blank if this can be scheduled from priority.")
        self.assertNotContains(response, "Person or office to notify after the task is complete")
        self.assertNotContains(response, "Choose the main teammate for this task.")
        self.assertNotContains(response, "Pick any teammates who should always be added to this task.")
        self.assertNotContains(response, "Choose any Monday through Friday times this task can be worked. TaskForge uses these windows to find teammates who have availability and to place the task inside those hours.")
        self.assertNotContains(response, "Turn this on only if this task should repeat automatically.")

    def test_task_edit_with_existing_recurring_task_keeps_recurring_panel_available(self):
        task = Task.objects.create(
            title="Existing recurring task",
            description="Testing recurring task panel",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.student,
            created_by=self.supervisor,
            recurring_task=True,
            recurrence_pattern="weekly",
            recurrence_interval=1,
            recurrence_day_of_week=Weekday.FRIDAY,
        )

        response = self.client.get(reverse("task-edit", args=[task.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'data-recurring-toggle', html=False)
        self.assertNotContains(response, 'data-recurring-fields style="display: none;"', html=False)


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

    def test_rotating_additional_assignee_can_view_shared_task(self):
        self.task.rotating_additional_assignee_count = 1
        self.task.rotate_additional_assignee = True
        self.task.rotating_additional_assignee = self.other_student
        self.task.save(update_fields=["rotating_additional_assignee_count", "rotate_additional_assignee", "rotating_additional_assignee", "updated_at"])
        self.task.rotating_additional_assignees.add(self.other_student)

        self.client.force_login(self.other_student)
        response = self.client.get(reverse("my-tasks"))
        self.assertContains(response, "Shared task")
        detail_response = self.client.get(reverse("task-detail", args=[self.task.pk]))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "other-vis (rotation)")

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


class TaskCreateAdditionalAssigneeRotationTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="rotation-supervisor", password="password123", role=UserRole.SUPERVISOR)
        self.primary_student = self._create_worker("primary-helper", "Primary Helper")
        self.fixed_student = self._create_worker("fixed-helper", "Fixed Helper")
        self.rotating_student = self._create_worker("rotating-helper", "Rotating Helper")
        self.second_rotating_student = self._create_worker("second-rotating-helper", "Taylor Helper")
        self.client.force_login(self.supervisor)

    def _create_worker(self, username, display_name):
        first_name, last_name = display_name.split(" ", 1)
        user = User.objects.create_user(
            username=username,
            password="password123",
            role=UserRole.STUDENT_WORKER,
            first_name=first_name,
            last_name=last_name,
        )
        profile = StudentWorkerProfile.objects.create(
            user=user,
            display_name=display_name,
            email=f"{username}@example.com",
            normal_shift_availability="Weekdays",
        )
        for weekday in Weekday.values:
            StudentAvailability.objects.create(
                profile=profile,
                weekday=weekday,
                hours_available=4 if weekday < 5 else 0,
            )
        return user

    def test_create_task_can_add_fixed_and_rotating_additional_assignees(self):
        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Collaborative task",
                "description": "Needs a main worker, one fixed helper, and rotating helpers.",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "2026-03-20",
                "respond_to_text": "",
                "estimated_minutes": "45",
                "assigned_to": str(self.primary_student.pk),
                "additional_assignees": [str(self.fixed_student.pk)],
                "rotating_additional_assignee_count": "2",
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
        task = Task.objects.get(title="Collaborative task")
        self.assertEqual(list(task.additional_assignees.values_list("id", flat=True)), [self.fixed_student.id])
        self.assertEqual(task.rotating_additional_assignee_count, 2)
        self.assertSetEqual(
            set(task.rotating_additional_assignees.values_list("id", flat=True)),
            {self.rotating_student.id, self.second_rotating_student.id},
        )
        self.assertContains(response, "Fixed Helper")
        self.assertContains(response, "Rotating Helper (rotation)")
        self.assertContains(response, "Taylor Helper (rotation)")


class BoardFilterAndAlertTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="board-filter-supervisor", password="password123", role=UserRole.SUPERVISOR)
        self.worker_one = User.objects.create_user(username="board-filter-worker-1", password="password123", role=UserRole.STUDENT_WORKER)
        self.worker_two = User.objects.create_user(username="board-filter-worker-2", password="password123", role=UserRole.STUDENT_WORKER)
        StudentWorkerProfile.objects.create(user=self.worker_one, display_name="Alex Worker", email="alex@example.com")
        StudentWorkerProfile.objects.create(user=self.worker_two, display_name="Jamie Worker", email="jamie@example.com")
        self.overdue_task = Task.objects.create(
            title="Overdue archive cleanup",
            description="Needs attention",
            priority=Priority.HIGH,
            status=TaskStatus.NEW,
            assigned_to=self.worker_one,
            due_date=date(2026, 3, 19),
            board_order=1,
        )
        self.waiting_task = Task.objects.create(
            title="Waiting on vendor reply",
            description="Blocked",
            priority=Priority.MEDIUM,
            status=TaskStatus.WAITING,
            assigned_to=self.worker_two,
            due_date=date(2026, 3, 24),
            board_order=1,
        )
        self.scheduled_task = Task.objects.create(
            title="Front desk shift prep",
            description="Prep for the day",
            priority=Priority.MEDIUM,
            status=TaskStatus.IN_PROGRESS,
            assigned_to=self.worker_one,
            due_date=date(2026, 3, 20),
            scheduled_date=date(2026, 3, 20),
            scheduled_start_time=time(9, 0),
            scheduled_end_time=time(10, 0),
            board_order=1,
        )
        self.recurring_task = Task.objects.create(
            title="Weekly recurring mail sweep",
            description="Recurring work",
            priority=Priority.LOW,
            status=TaskStatus.NEW,
            assigned_to=self.worker_two,
            due_date=date(2026, 3, 25),
            recurring_task=True,
            recurrence_pattern="weekly",
            recurrence_interval=1,
            board_order=2,
        )
        RecurringTaskTemplate.objects.create(
            title="Due soon recurring template",
            description="Heads up",
            priority=Priority.MEDIUM,
            estimated_minutes=30,
            recurrence_pattern="weekly",
            recurrence_interval=1,
            next_run_date=date(2027, 3, 21),
        )
        self.client.force_login(self.supervisor)

    def test_board_shows_compact_due_today_warning(self):
        with patch("workboard.task_views.timezone.localdate", return_value=date(2026, 3, 20)):
            response = self.client.get(reverse("board"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Warning:")
        self.assertContains(response, "1 task due or scheduled today")
        self.assertContains(response, reverse("board") + "?saved_view=today")
        self.assertNotContains(response, "1 overdue task")
        self.assertNotContains(response, "1 recurring task due soon")
        self.assertContains(response, "Saved view")
        self.assertContains(response, "Search tasks")

    def test_board_saved_view_filters_waiting_tasks(self):
        response = self.client.get(reverse("board"), {"saved_view": "waiting"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Waiting on vendor reply")
        self.assertNotContains(response, "Overdue archive cleanup")
        self.assertContains(response, "View: Waiting / blocked")

    def test_board_assignee_filter_matches_selected_teammate(self):
        response = self.client.get(reverse("board"), {"assigned_to": str(self.worker_one.pk)})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Overdue archive cleanup")
        self.assertContains(response, "Front desk shift prep")
        self.assertNotContains(response, "Waiting on vendor reply")

    def test_board_groups_overdue_tasks_into_overdue_column_between_new_and_in_progress(self):
        with patch("workboard.task_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 12, 0))):
            response = self.client.get(reverse("board"))

        self.assertEqual(response.status_code, 200)
        grouped_tasks = response.context["grouped_tasks"]
        self.assertEqual(
            [column["value"] for column in grouped_tasks],
            [TaskStatus.NEW, "overdue", TaskStatus.IN_PROGRESS, TaskStatus.WAITING, TaskStatus.DONE],
        )
        overdue_column = next(column for column in grouped_tasks if column["value"] == "overdue")
        new_column = next(column for column in grouped_tasks if column["value"] == TaskStatus.NEW)
        self.assertEqual([task.title for task in overdue_column["tasks"]], ["Overdue archive cleanup"])
        self.assertIn("Weekly recurring mail sweep", [task.title for task in new_column["tasks"]])
        self.assertNotIn("Overdue archive cleanup", [task.title for task in new_column["tasks"]])
        self.assertContains(response, "Stage: New Requests")

    def test_board_overdue_view_includes_today_tasks_after_evening_cutoff(self):
        with patch("workboard.task_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 18, 5))):
            response = self.client.get(reverse("board"), {"saved_view": "overdue"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Overdue archive cleanup")
        self.assertContains(response, "Front desk shift prep")
        self.assertNotContains(response, "Waiting on vendor reply")

    def test_board_filter_bar_hides_schedule_and_status_controls(self):
        response = self.client.get(reverse("board"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="schedule_scope"', html=False)
        self.assertNotContains(response, 'name="completion_scope"', html=False)
        self.assertNotContains(response, '>Schedule</label>', html=False)
        self.assertNotContains(response, '>Status</label>', html=False)

    def test_board_hides_done_tasks_older_than_seven_days(self):
        Task.objects.create(
            title="Old completed board task",
            description="Should move to completed tasks",
            priority=Priority.MEDIUM,
            status=TaskStatus.DONE,
            assigned_to=self.worker_one,
            completed_at=timezone.make_aware(datetime(2026, 3, 10, 9, 0)),
            board_order=1,
        )
        Task.objects.create(
            title="Recent completed board task",
            description="Should stay in Done for now",
            priority=Priority.MEDIUM,
            status=TaskStatus.DONE,
            assigned_to=self.worker_one,
            completed_at=timezone.make_aware(datetime(2026, 3, 18, 9, 0)),
            board_order=2,
        )

        with patch("workboard.task_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 12, 0))):
            response = self.client.get(reverse("board"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recent completed board task")
        self.assertNotContains(response, "Old completed board task")


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
        waiting_column = next(column for column in grouped_tasks if column["value"] == TaskStatus.WAITING)

        self.assertEqual([task.title for task in new_column["tasks"]], ["First visible task", "Second visible task"])
        self.assertEqual([task.title for task in waiting_column["tasks"]], ["Review visible task"])
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

    def test_my_tasks_shows_compact_due_today_warning(self):
        self.first_task.due_date = date(2026, 3, 20)
        self.first_task.save(update_fields=["due_date"])
        self.client.force_login(self.student)

        with patch("workboard.task_views.timezone.localdate", return_value=date(2026, 3, 20)):
            response = self.client.get(reverse("my-tasks"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Warning:")
        self.assertContains(response, "1 task due or scheduled today")
        self.assertContains(response, reverse("my-tasks") + "?saved_view=today")
        self.assertNotContains(response, "1 overdue task")

    def test_my_tasks_filter_bar_hides_schedule_and_status_controls(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("my-tasks"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="schedule_scope"', html=False)
        self.assertNotContains(response, 'name="completion_scope"', html=False)
        self.assertNotContains(response, '>Schedule</label>', html=False)
        self.assertNotContains(response, '>Status</label>', html=False)

    def test_my_tasks_hides_done_tasks_older_than_seven_days(self):
        Task.objects.create(
            title="Old completed my task",
            description="Should move off My Tasks",
            priority=Priority.MEDIUM,
            status=TaskStatus.DONE,
            assigned_to=self.student,
            completed_at=timezone.make_aware(datetime(2026, 3, 10, 8, 0)),
            board_order=1,
        )
        Task.objects.create(
            title="Recent completed my task",
            description="Should stay visible for now",
            priority=Priority.MEDIUM,
            status=TaskStatus.DONE,
            assigned_to=self.student,
            completed_at=timezone.make_aware(datetime(2026, 3, 18, 8, 0)),
            board_order=2,
        )
        self.client.force_login(self.student)

        with patch("workboard.task_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 12, 0))):
            response = self.client.get(reverse("my-tasks"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recent completed my task")
        self.assertNotContains(response, "Old completed my task")


class MyTasksOverdueSectionTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="My Tasks Overdue", description="My Tasks overdue scope")
        self.supervisor = User.objects.create_user(
            username="mytasks-overdue-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
            team=self.team,
        )
        self.student_supervisor = User.objects.create_user(
            username="mytasks-overdue-lead",
            password="password123",
            role=UserRole.STUDENT_SUPERVISOR,
            team=self.team,
        )
        self.student = User.objects.create_user(
            username="mytasks-overdue-worker",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            team=self.team,
        )
        self.other_student = User.objects.create_user(
            username="mytasks-overdue-helper",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            team=self.team,
        )
        StudentWorkerProfile.objects.create(user=self.student_supervisor, display_name="Morgan Lead", email="lead@example.com")
        StudentWorkerProfile.objects.create(user=self.student, display_name="Taylor Worker", email="worker@example.com")
        StudentWorkerProfile.objects.create(user=self.other_student, display_name="Casey Helper", email="helper@example.com")
        self.overdue_task = Task.objects.create(
            team=self.team,
            title="Team overdue task",
            description="Needs follow up",
            priority=Priority.HIGH,
            status=TaskStatus.IN_PROGRESS,
            due_date=date(2026, 3, 19),
            assigned_to=self.student,
            created_by=self.supervisor,
            board_order=1,
        )
        self.waiting_task = Task.objects.create(
            team=self.team,
            title="Waiting task for leads",
            description="Waiting work",
            priority=Priority.MEDIUM,
            status=TaskStatus.WAITING,
            due_date=date(2026, 3, 22),
            assigned_to=self.other_student,
            created_by=self.supervisor,
            board_order=1,
        )
        self.lead_task = Task.objects.create(
            team=self.team,
            title="Lead personal task",
            description="Lead-owned task",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            due_date=date(2026, 3, 22),
            assigned_to=self.student_supervisor,
            created_by=self.supervisor,
            board_order=2,
        )

    def test_supervisor_my_tasks_shows_team_overdue_tasks_in_overdue_column(self):
        self.client.force_login(self.supervisor)

        with patch("workboard.task_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 12, 0))):
            response = self.client.get(reverse("my-tasks"))

        self.assertEqual(response.status_code, 200)
        grouped_tasks = response.context["grouped_tasks"]
        self.assertEqual(
            [column["value"] for column in grouped_tasks],
            [TaskStatus.NEW, "overdue", TaskStatus.IN_PROGRESS, TaskStatus.WAITING, TaskStatus.DONE],
        )
        overdue_column = next(column for column in grouped_tasks if column["value"] == "overdue")
        waiting_column = next(column for column in grouped_tasks if column["value"] == TaskStatus.WAITING)
        self.assertEqual([task.title for task in overdue_column["tasks"]], ["Team overdue task"])
        self.assertEqual([task.title for task in waiting_column["tasks"]], ["Waiting task for leads"])
        self.assertContains(response, "Stage: In Progress")

    def test_student_supervisor_my_tasks_shows_team_overdue_tasks_in_overdue_column(self):
        self.client.force_login(self.student_supervisor)

        with patch("workboard.task_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 12, 0))):
            response = self.client.get(reverse("my-tasks"))

        self.assertEqual(response.status_code, 200)
        grouped_tasks = response.context["grouped_tasks"]
        overdue_column = next(column for column in grouped_tasks if column["value"] == "overdue")
        new_column = next(column for column in grouped_tasks if column["value"] == TaskStatus.NEW)
        self.assertEqual([task.title for task in overdue_column["tasks"]], ["Team overdue task"])
        self.assertEqual([task.title for task in new_column["tasks"]], ["Lead personal task"])
        self.assertContains(response, "Assigned to: Taylor Worker")

    def test_student_worker_sees_personal_overdue_tasks_in_overdue_column(self):
        self.client.force_login(self.student)

        with patch("workboard.task_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 12, 0))):
            response = self.client.get(reverse("my-tasks"))

        self.assertEqual(response.status_code, 200)
        grouped_tasks = response.context["grouped_tasks"]
        overdue_column = next(column for column in grouped_tasks if column["value"] == "overdue")
        grouped_titles = [task.title for column in grouped_tasks for task in column["tasks"]]
        self.assertEqual([task.title for task in overdue_column["tasks"]], ["Team overdue task"])
        self.assertNotIn("Waiting task for leads", grouped_titles)
        self.assertNotIn("Lead personal task", grouped_titles)

    def test_supervisor_overdue_column_treats_due_today_tasks_as_overdue_after_cutoff(self):
        self.overdue_task.due_date = date(2026, 3, 20)
        self.overdue_task.save(update_fields=["due_date", "updated_at"])
        self.client.force_login(self.supervisor)

        with patch("workboard.task_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 18, 5))):
            response = self.client.get(reverse("my-tasks"))

        self.assertEqual(response.status_code, 200)
        overdue_column = next(column for column in response.context["grouped_tasks"] if column["value"] == "overdue")
        self.assertEqual([task.title for task in overdue_column["tasks"]], ["Team overdue task"])


class CompletedTasksViewTests(TestCase):
    def setUp(self):
        self.team_alpha = Team.objects.create(name="Completed Alpha", description="Alpha completed tasks")
        self.team_beta = Team.objects.create(name="Completed Beta", description="Beta completed tasks")
        self.supervisor = User.objects.create_user(
            username="completed-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
            first_name="Alice",
            last_name="Supervisor",
            team=self.team_alpha,
        )
        self.supervisor_beta = User.objects.create_user(
            username="completed-beta-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
            first_name="Ben",
            last_name="Beta",
            team=self.team_beta,
        )
        self.student_supervisor = User.objects.create_user(
            username="completed-student-lead",
            password="password123",
            role=UserRole.STUDENT_SUPERVISOR,
            team=self.team_alpha,
        )
        self.worker_one = User.objects.create_user(
            username="completed-worker-one",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            team=self.team_alpha,
        )
        self.worker_two = User.objects.create_user(
            username="completed-worker-two",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            team=self.team_alpha,
        )
        self.worker_beta = User.objects.create_user(
            username="completed-worker-beta",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            team=self.team_beta,
        )
        StudentWorkerProfile.objects.create(
            user=self.student_supervisor,
            display_name="Jordan Lead",
            email="lead@example.com",
            normal_shift_availability="",
        )
        StudentWorkerProfile.objects.create(
            user=self.worker_one,
            display_name="Alex Archive",
            email="alex@example.com",
            normal_shift_availability="",
        )
        StudentWorkerProfile.objects.create(
            user=self.worker_two,
            display_name="Jamie Closeout",
            email="jamie@example.com",
            normal_shift_availability="",
        )
        StudentWorkerProfile.objects.create(
            user=self.worker_beta,
            display_name="Beta Worker",
            email="beta@example.com",
            normal_shift_availability="",
        )
        self.old_task = Task.objects.create(
            team=self.team_alpha,
            title="Archive inbox cleanup",
            description="Completed earlier",
            priority=Priority.MEDIUM,
            status=TaskStatus.DONE,
            assigned_to=self.worker_one,
            created_by=self.supervisor,
            estimated_minutes=30,
            completed_at=timezone.make_aware(datetime(2026, 3, 10, 9, 0)),
        )
        self.recent_task = Task.objects.create(
            team=self.team_alpha,
            title="Desk closeout",
            description="Completed yesterday",
            priority=Priority.HIGH,
            status=TaskStatus.DONE,
            assigned_to=self.worker_two,
            created_by=self.supervisor,
            estimated_minutes=45,
            completed_at=timezone.make_aware(datetime(2026, 3, 19, 15, 0)),
        )
        self.collab_task = Task.objects.create(
            team=self.team_alpha,
            title="Mail run follow-up",
            description="Shared finish",
            priority=Priority.LOW,
            status=TaskStatus.DONE,
            assigned_to=self.worker_two,
            created_by=self.supervisor,
            estimated_minutes=60,
            completed_at=timezone.make_aware(datetime(2026, 3, 20, 9, 30)),
        )
        self.collab_task.additional_assignees.add(self.worker_one)
        self.beta_task = Task.objects.create(
            team=self.team_beta,
            title="Beta closed task",
            description="Should stay scoped to beta",
            priority=Priority.MEDIUM,
            status=TaskStatus.DONE,
            assigned_to=self.worker_beta,
            created_by=self.supervisor_beta,
            estimated_minutes=25,
            completed_at=timezone.make_aware(datetime(2026, 3, 20, 10, 0)),
        )

    def test_completed_tasks_tab_appears_after_my_tasks(self):
        self.client.force_login(self.supervisor)

        response = self.client.get(reverse("board"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertLess(content.index(reverse("my-tasks")), content.index(reverse("completed-tasks")))

    def test_supervisor_completed_tasks_page_is_team_scoped_and_newest_first(self):
        self.client.force_login(self.supervisor)

        response = self.client.get(reverse("completed-tasks"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([task.title for task in response.context["tasks"]], ["Mail run follow-up", "Desk closeout", "Archive inbox cleanup"])
        self.assertNotContains(response, "Beta closed task")
        self.assertContains(response, "Completed tasks")
        self.assertContains(response, "Completed in last 7 days")
        self.assertIn("student", response.context["filter_form"].fields)

    def test_student_supervisor_can_filter_team_completed_tasks_by_student(self):
        self.client.force_login(self.student_supervisor)

        response = self.client.get(reverse("completed-tasks"), {"student": str(self.worker_one.pk)})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([task.title for task in response.context["tasks"]], ["Mail run follow-up", "Archive inbox cleanup"])
        self.assertNotContains(response, "Desk closeout")
        self.assertIn("student", response.context["filter_form"].fields)
        self.assertContains(response, "Student: Alex Archive")

    def test_student_worker_only_sees_their_completed_tasks(self):
        self.client.force_login(self.worker_one)

        response = self.client.get(reverse("completed-tasks"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([task.title for task in response.context["tasks"]], ["Mail run follow-up", "Archive inbox cleanup"])
        self.assertNotContains(response, "Desk closeout")
        self.assertNotIn("student", response.context["filter_form"].fields)
        self.assertEqual(response.context["summary_cards"][0]["value"], 2)

    def test_completed_tasks_search_filters_results(self):
        self.client.force_login(self.supervisor)

        response = self.client.get(reverse("completed-tasks"), {"q": "Desk"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([task.title for task in response.context["tasks"]], ["Desk closeout"])
        self.assertIn('Search: "Desk"', response.context["active_filters"])
        self.assertEqual(response.context["task_count"], 1)


class StudentSupervisorPermissionsTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="lead-sup", password="password123", role=UserRole.SUPERVISOR)
        self.student_supervisor = User.objects.create_user(username="student-lead", password="password123", role=UserRole.STUDENT_SUPERVISOR)
        self.worker = User.objects.create_user(username="board-worker", password="password123", role=UserRole.STUDENT_WORKER)
        StudentWorkerProfile.objects.create(
            user=self.student_supervisor,
            display_name="Student Lead",
            email="lead@example.com",
            normal_shift_availability="",
        )
        StudentWorkerProfile.objects.create(
            user=self.worker,
            display_name="Board Worker",
            email="worker@example.com",
            normal_shift_availability="",
        )
        self.task = Task.objects.create(
            title="Shared board task",
            description="Visible to the student supervisor",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.worker,
            created_by=self.supervisor,
            board_order=1,
        )

    def test_student_supervisor_sees_full_board_and_can_open_task_edit(self):
        self.client.force_login(self.student_supervisor)

        board_response = self.client.get(reverse("board"))
        self.assertEqual(board_response.status_code, 200)
        self.assertContains(board_response, "Shared board task")

        detail_response = self.client.get(reverse("task-detail", args=[self.task.pk]))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, reverse("task-edit", args=[self.task.pk]))

        edit_response = self.client.get(reverse("task-edit", args=[self.task.pk]))
        self.assertEqual(edit_response.status_code, 200)

    def test_student_supervisor_cannot_create_tasks(self):
        self.client.force_login(self.student_supervisor)
        response = self.client.get(reverse("task-create"))
        self.assertEqual(response.status_code, 403)

    def test_student_supervisor_can_move_other_workers_task_on_board(self):
        self.client.force_login(self.student_supervisor)
        response = self.client.post(reverse("board-task-move", args=[self.task.pk]), {"status": TaskStatus.IN_PROGRESS})
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TaskStatus.IN_PROGRESS)


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
            {"status": TaskStatus.WAITING, "before_task_id": str(self.review_task.pk)},
        )
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.review_task.refresh_from_db()
        self.second_task.refresh_from_db()
        self.third_task.refresh_from_db()
        self.assertEqual(self.task.status, TaskStatus.WAITING)
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


class ScheduleAdjustmentRequestTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="schedule-request-supervisor", password="password123", role=UserRole.SUPERVISOR)
        self.student = User.objects.create_user(username="schedule-request-student", password="password123", role=UserRole.STUDENT_WORKER)
        self.student_supervisor = User.objects.create_user(username="schedule-request-lead", password="password123", role=UserRole.STUDENT_SUPERVISOR)
        self.profile = StudentWorkerProfile.objects.create(
            user=self.student,
            display_name="Schedule Student",
            email="schedule-student@example.com",
            normal_shift_availability="",
        )
        self.student_supervisor_profile = StudentWorkerProfile.objects.create(
            user=self.student_supervisor,
            display_name="Schedule Lead",
            email="schedule-lead@example.com",
            normal_shift_availability="",
        )

    def test_student_can_submit_schedule_adjustment_request(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse("schedule-adjustment-request"),
            {
                "requested_date": "2026-03-24",
                "note": "Need to swap tutoring hours.",
                "request_segments": json.dumps([["13:00", "15:00"], ["16:00", "17:00"]]),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        adjustment_request = ScheduleAdjustmentRequest.objects.get(profile=self.profile)
        self.assertEqual(adjustment_request.requested_by, self.student)
        self.assertEqual(adjustment_request.status, ScheduleAdjustmentRequestStatus.PENDING)
        self.assertEqual(adjustment_request.blocks.count(), 2)
        self.assertContains(response, "Schedule adjustment request submitted for 2026-03-24")
        self.assertContains(response, "Pending")

    def test_student_supervisor_can_open_schedule_adjustment_page(self):
        self.client.force_login(self.student_supervisor)
        response = self.client.get(reverse("schedule-adjustment-request"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Request a schedule adjustment")

    def test_supervisor_can_apply_schedule_request_to_temporary_override(self):
        adjustment_request = ScheduleAdjustmentRequest.objects.create(
            profile=self.profile,
            requested_by=self.student,
            requested_date=date(2026, 3, 24),
            note="Need to work the afternoon instead.",
        )
        ScheduleAdjustmentRequest.objects.filter(pk=adjustment_request.pk)
        adjustment_request.blocks.create(start_time=time(13, 0), end_time=time(15, 0), position=1)
        adjustment_request.blocks.create(start_time=time(16, 0), end_time=time(17, 0), position=2)

        self.client.force_login(self.supervisor)
        response = self.client.post(
            reverse("schedule-adjustment-requests"),
            {"action": "apply_request", "schedule_request_id": str(adjustment_request.pk)},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        adjustment_request.refresh_from_db()
        self.assertEqual(adjustment_request.status, ScheduleAdjustmentRequestStatus.APPLIED)
        self.assertEqual(adjustment_request.reviewed_by, self.supervisor)
        self.assertIsNotNone(adjustment_request.applied_override)
        schedule_override = StudentScheduleOverride.objects.get(profile=self.profile, override_date=date(2026, 3, 24))
        self.assertEqual(schedule_override.blocks.count(), 2)
        self.assertIn("Applied from request by Schedule Student", schedule_override.note)
        self.assertTrue(
            TaskAssignmentService.user_is_available_for_window(
                self.student,
                scheduled_date=date(2026, 3, 24),
                scheduled_start_time=time(13, 30),
                scheduled_end_time=time(14, 30),
            )
        )

    def test_non_supervisor_cannot_open_schedule_request_review_page(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("schedule-adjustment-requests"))
        self.assertEqual(response.status_code, 403)


class PeopleManagementTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="people-sup", password="password123", role=UserRole.SUPERVISOR)
        self.other_supervisor = User.objects.create_user(username="other-sup", password="password123", role=UserRole.SUPERVISOR)
        self.student = User.objects.create_user(username="remove-student", password="password123", role=UserRole.STUDENT_WORKER)
        self.profile = StudentWorkerProfile.objects.create(
            user=self.student,
            display_name="Remove Student",
            email="remove-student@example.com",
            normal_shift_availability="Weekdays",
        )
        self.student_supervisor = User.objects.create_user(username="student-lead", password="password123", role=UserRole.STUDENT_SUPERVISOR)
        self.student_supervisor_profile = StudentWorkerProfile.objects.create(
            user=self.student_supervisor,
            display_name="Student Lead",
            email="student-lead@example.com",
            normal_shift_availability="Weekdays",
        )
        self.task = Task.objects.create(
            title="Assigned to removed student",
            description="Cleanup",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.student,
            created_by=self.supervisor,
        )
        self.recurring_template = RecurringTaskTemplate.objects.create(
            title="Student recurring",
            description="Recurring cleanup",
            priority=Priority.MEDIUM,
            estimated_minutes=30,
            assign_to=self.student,
            requested_by=self.supervisor,
            recurrence_pattern="weekly",
            recurrence_interval=1,
        )
        self.worker_tag = WorkerTag.objects.create(name="Front Desk", team=self.profile.user.team)
        self.client.force_login(self.supervisor)

    def test_people_page_shows_cleaner_actions_for_workers_and_supervisors(self):
        self.profile.tags.add(self.worker_tag)
        response = self.client.get(reverse("worker-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("worker-create"))
        self.assertContains(response, reverse("student-supervisor-create"))
        self.assertContains(response, reverse("supervisor-create"))
        self.assertContains(response, reverse("worker-tag-create"))
        self.assertContains(response, "Worker tags")
        self.assertContains(response, self.worker_tag.name)
        self.assertContains(response, "Front Desk")
        self.assertContains(response, "Student supervisors")
        self.assertContains(response, reverse("worker-edit", args=[self.profile.pk]))
        self.assertContains(response, reverse("worker-schedule", args=[self.profile.pk]))
        self.assertContains(response, reverse("worker-edit", args=[self.student_supervisor_profile.pk]))
        self.assertContains(response, reverse("worker-schedule", args=[self.student_supervisor_profile.pk]))
        self.assertContains(response, reverse("supervisor-edit", args=[self.other_supervisor.pk]))
        self.assertContains(response, "Edit worker")
        self.assertContains(response, "Edit student supervisor")
        self.assertContains(response, "Edit schedule")
        self.assertNotContains(response, "Remove student")
        self.assertNotContains(response, "Remove supervisor")
        self.assertNotContains(response, "Manage student workers, supervisors, and assignment availability.")
        self.assertNotContains(response, "<th>Availability</th>", html=False)
        self.assertNotContains(response, "Max Hours/Day")

    def test_edit_worker_updates_student_details(self):
        response = self.client.post(
            reverse("worker-edit", args=[self.profile.pk]),
            {
                "username": "updated-student",
                "first_name": "Jordan",
                "last_name": "Parker",
                "email": "jordan@example.com",
                "active_status": "",
                "tags": [str(self.worker_tag.pk)],
                "skill_notes": "Prefers morning tasks",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.student.refresh_from_db()
        self.assertEqual(self.student.username, "updated-student")
        self.assertEqual(self.student.first_name, "Jordan")
        self.assertEqual(self.student.last_name, "Parker")
        self.assertEqual(self.student.email, "jordan@example.com")
        self.assertEqual(self.profile.display_name, "Jordan Parker")
        self.assertFalse(self.profile.active_status)
        self.assertEqual(self.profile.skill_notes, "Prefers morning tasks")
        self.assertEqual(list(self.profile.tags.values_list("pk", flat=True)), [self.worker_tag.pk])

    def test_edit_pages_hide_optional_email_help_text(self):
        worker_response = self.client.get(reverse("worker-edit", args=[self.profile.pk]))
        student_supervisor_response = self.client.get(reverse("worker-edit", args=[self.student_supervisor_profile.pk]))
        supervisor_response = self.client.get(reverse("supervisor-edit", args=[self.other_supervisor.pk]))

        self.assertEqual(worker_response.status_code, 200)
        self.assertEqual(student_supervisor_response.status_code, 200)
        self.assertEqual(supervisor_response.status_code, 200)
        self.assertNotContains(worker_response, "Optional. Leave blank if you do not want to store an email address for this person.")
        self.assertNotContains(student_supervisor_response, "Optional. Leave blank if you do not want to store an email address for this person.")
        self.assertNotContains(supervisor_response, "Optional. Leave blank if you do not want to store an email address for this supervisor.")

    def test_supervisor_can_create_worker_tag_for_their_team(self):
        response = self.client.post(
            reverse("worker-tag-create"),
            {"name": "Phones"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(WorkerTag.objects.filter(name="Phones", team=self.supervisor.team).exists())

    def test_edit_schedule_updates_student_weekly_schedule(self):
        response = self.client.post(
            reverse("worker-schedule", args=[self.profile.pk]),
            {
                "action": "weekly",
                "monday_segments": json.dumps([["09:00", "14:00"]]),
                "friday_segments": json.dumps([["10:00", "11:00"]]),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.weekly_availability.get(weekday=Weekday.MONDAY).hours_available, 5)
        self.assertEqual(self.profile.weekly_availability.get(weekday=Weekday.FRIDAY).hours_available, 1)

    def test_temporary_schedule_override_replaces_normal_blocks_for_specific_date(self):
        monday, _ = StudentAvailability.objects.update_or_create(
            profile=self.profile,
            weekday=Weekday.MONDAY,
            defaults={"start_time": time(9, 0), "end_time": time(12, 0), "hours_available": 3},
        )
        monday.blocks.all().delete()
        StudentAvailabilityBlock.objects.create(availability=monday, start_time=time(9, 0), end_time=time(12, 0), position=1)

        response = self.client.post(
            reverse("worker-schedule", args=[self.profile.pk]),
            {
                "action": "schedule_override",
                "override_date": "2026-03-16",
                "note": "Split lab schedule",
                "override_segments": json.dumps([["14:00", "16:00"], ["16:30", "17:30"]]),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        schedule_override = self.profile.schedule_overrides.get(override_date=date(2026, 3, 16))
        self.assertEqual(schedule_override.blocks.count(), 2)
        self.assertEqual(schedule_override.block_summary, "2:00 PM - 4:00 PM, 4:30 PM - 5:30 PM")
        self.assertContains(response, 'class="weekly-schedule-summary-card is-temporary-override" data-schedule-summary-card="monday"', html=False)
        self.assertContains(response, "Mar 16, 2026")
        self.assertContains(response, "2:00 PM - 4:00 PM, 4:30 PM - 5:30 PM (3 hrs)")
        self.assertNotContains(response, 'data-schedule-summary-text="monday"')
        self.assertFalse(
            TaskAssignmentService.user_is_available_for_window(
                self.student,
                scheduled_date=date(2026, 3, 16),
                scheduled_start_time=time(9, 30),
                scheduled_end_time=time(10, 30),
            )
        )
        self.assertTrue(
            TaskAssignmentService.user_is_available_for_window(
                self.student,
                scheduled_date=date(2026, 3, 16),
                scheduled_start_time=time(14, 30),
                scheduled_end_time=time(15, 30),
            )
        )
        self.assertTrue(
            TaskAssignmentService.user_is_available_for_window(
                self.student,
                scheduled_date=date(2026, 3, 23),
                scheduled_start_time=time(9, 30),
                scheduled_end_time=time(10, 30),
            )
        )
        self.assertFalse(
            TaskAssignmentService.user_is_available_for_window(
                self.student,
                scheduled_date=date(2026, 3, 23),
                scheduled_start_time=time(14, 30),
                scheduled_end_time=time(15, 30),
            )
        )


    def test_empty_temporary_schedule_override_marks_day_unavailable_only_for_that_date(self):
        monday, _ = StudentAvailability.objects.update_or_create(
            profile=self.profile,
            weekday=Weekday.MONDAY,
            defaults={"start_time": time(9, 0), "end_time": time(12, 0), "hours_available": 3},
        )
        monday.blocks.all().delete()
        StudentAvailabilityBlock.objects.create(availability=monday, start_time=time(9, 0), end_time=time(12, 0), position=1)

        response = self.client.post(
            reverse("worker-schedule", args=[self.profile.pk]),
            {
                "action": "schedule_override",
                "override_date": "2026-03-16",
                "note": "Out for the day",
                "override_segments": "[]",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        schedule_override = self.profile.schedule_overrides.get(override_date=date(2026, 3, 16))
        self.assertEqual(schedule_override.blocks.count(), 0)
        self.assertContains(response, "Mar 16, 2026")
        self.assertContains(response, "Off (0 hrs)")
        self.assertNotContains(response, 'data-schedule-summary-text="monday"')
        self.assertFalse(
            TaskAssignmentService.user_is_available_for_window(
                self.student,
                scheduled_date=date(2026, 3, 16),
                scheduled_start_time=time(9, 30),
                scheduled_end_time=time(10, 30),
            )
        )
        self.assertTrue(
            TaskAssignmentService.user_is_available_for_window(
                self.student,
                scheduled_date=date(2026, 3, 23),
                scheduled_start_time=time(9, 30),
                scheduled_end_time=time(10, 30),
            )
        )

    def test_worker_schedule_rejects_after_hours_override(self):
        response = self.client.post(
            reverse("worker-schedule", args=[self.profile.pk]),
            {
                "action": "schedule_override",
                "override_date": "2026-03-16",
                "note": "Too late",
                "override_segments": json.dumps([["18:00", "19:00"]]),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Temporary schedule must stay between 7:00 AM and 6:00 PM.")
        self.assertFalse(self.profile.schedule_overrides.filter(override_date=date(2026, 3, 16)).exists())

    def test_add_student_form_uses_weekly_schedule_fields_and_hides_old_profile_fields(self):
        response = self.client.get(reverse("worker-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Weekly schedule")
        self.assertContains(response, 'name="monday_start"')
        self.assertContains(response, 'name="monday_end"')
        self.assertContains(response, 'name="sunday_start"')
        self.assertContains(response, 'name="sunday_end"')
        self.assertNotContains(response, "Typical schedule")
        self.assertNotContains(response, 'name="normal_shift_availability"')
        self.assertNotContains(response, 'name="max_hours_per_day"')
        self.assertNotContains(response, "Display name")
        self.assertNotContains(response, 'name="display_name"')
        self.assertContains(response, 'name="email"', count=1)

    def test_worker_forms_render_calendar_style_schedule_picker(self):
        create_response = self.client.get(reverse("worker-create"))
        details_response = self.client.get(reverse("worker-edit", args=[self.profile.pk]))
        schedule_response = self.client.get(reverse("worker-schedule", args=[self.profile.pk]))

        self.assertContains(create_response, 'data-weekly-schedule-picker')
        self.assertContains(create_response, 'data-clear-week')
        self.assertContains(create_response, 'data-schedule-summary-card="monday"')
        self.assertContains(create_response, 'class="weekly-schedule-hidden-fields"')
        self.assertContains(create_response, 'class="weekly-calendar-cell"', count=154)
        self.assertContains(create_response, 'data-slot-value="07:00"')
        self.assertContains(create_response, 'data-slot-end="18:00"')
        self.assertNotContains(create_response, 'data-slot-value="06:30"')
        self.assertNotContains(create_response, 'data-slot-end="18:30"')
        self.assertContains(create_response, 'data-copy-day="monday"')
        self.assertContains(create_response, 'data-clear-day="monday"')
        self.assertNotContains(details_response, 'data-weekly-schedule-picker')
        self.assertContains(details_response, 'Remove student')
        self.assertContains(schedule_response, 'data-weekly-schedule-picker')
        self.assertContains(schedule_response, 'Weekly schedule', count=1)
        self.assertContains(schedule_response, 'Temporary schedule change')
        self.assertContains(schedule_response, 'data-load-normal-schedule')
        self.assertNotContains(schedule_response, 'data-copy-day="monday"')
        self.assertNotContains(schedule_response, 'data-clear-day="monday"')
        self.assertNotContains(schedule_response, 'Click or drag across the calendar')
        self.assertNotContains(schedule_response, 'Temporary hour adjustment')
        self.assertNotContains(schedule_response, 'Existing hour adjustments')
        self.assertContains(schedule_response, 'class="weekly-schedule-hidden-fields"', count=2)
        self.assertContains(schedule_response, 'name="monday_segments"')
        self.assertContains(schedule_response, 'name="override_segments"')

    def test_schedule_page_prefills_selected_date_from_weekly_schedule(self):
        monday, _ = StudentAvailability.objects.update_or_create(
            profile=self.profile,
            weekday=Weekday.MONDAY,
            defaults={"start_time": time(9, 0), "end_time": time(12, 0), "hours_available": 3},
        )
        monday.blocks.all().delete()
        StudentAvailabilityBlock.objects.create(availability=monday, start_time=time(9, 0), end_time=time(12, 0), position=1)

        response = self.client.get(reverse("worker-schedule", args=[self.profile.pk]), {"override_date": "2026-03-23"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_override_date"], date(2026, 3, 23))
        self.assertEqual(response.context["schedule_override_form"].initial["override_segments"], json.dumps([["09:00", "12:00"]]))
        self.assertContains(response, "Loaded March 23, 2026 into the editor")

    def test_schedule_page_loads_existing_override_into_editor(self):
        override = self.profile.schedule_overrides.create(override_date=date(2026, 3, 16), note="Existing override")
        override.blocks.create(start_time=time(14, 0), end_time=time(16, 0), position=1)

        response = self.client.get(reverse("worker-schedule", args=[self.profile.pk]), {"override_date": "2026-03-16"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_override_date"], date(2026, 3, 16))
        self.assertEqual(response.context["schedule_override_form"].instance.pk, override.pk)
        self.assertContains(response, '?override_date=2026-03-16')

    def test_edit_pages_show_remove_actions_for_student_supervisors_and_supervisors(self):
        student_supervisor_response = self.client.get(reverse("worker-edit", args=[self.student_supervisor_profile.pk]))
        supervisor_response = self.client.get(reverse("supervisor-edit", args=[self.other_supervisor.pk]))

        self.assertContains(student_supervisor_response, "Remove student supervisor")
        self.assertContains(supervisor_response, "Remove supervisor")

    def test_creating_student_uses_first_and_last_name_for_display_name_and_saves_weekly_hours(self):
        response = self.client.post(
            reverse("worker-create"),
            {
                "username": "new-student",
                "password": "password123",
                "first_name": "Taylor",
                "last_name": "Brooks",
                "email": "taylor@example.com",
                "active_status": "on",
                "skill_notes": "Strong with spreadsheets",
                "monday_hours": "4",
                "tuesday_hours": "3",
                "wednesday_hours": "2",
                "thursday_hours": "4",
                "friday_hours": "1",
                "saturday_hours": "0",
                "sunday_hours": "0",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username="new-student")
        profile = user.worker_profile
        self.assertEqual(profile.display_name, "Taylor Brooks")
        self.assertEqual(profile.weekly_availability.get(weekday=Weekday.MONDAY).hours_available, 4)
        self.assertEqual(profile.weekly_availability.get(weekday=Weekday.TUESDAY).hours_available, 3)
        self.assertEqual(profile.weekly_availability.get(weekday=Weekday.WEDNESDAY).hours_available, 2)
        self.assertEqual(profile.weekly_availability.get(weekday=Weekday.THURSDAY).hours_available, 4)
        self.assertEqual(profile.weekly_availability.get(weekday=Weekday.FRIDAY).hours_available, 1)

    def test_creating_student_allows_blank_email(self):
        response = self.client.post(
            reverse("worker-create"),
            {
                "username": "blank-email-student",
                "password": "password123",
                "first_name": "Taylor",
                "last_name": "Blank",
                "email": "",
                "active_status": "on",
                "skill_notes": "",
                "monday_hours": "4",
                "tuesday_hours": "3",
                "wednesday_hours": "2",
                "thursday_hours": "4",
                "friday_hours": "1",
                "saturday_hours": "0",
                "sunday_hours": "0",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username="blank-email-student")
        self.assertEqual(user.email, "")
        self.assertEqual(user.worker_profile.email, "")

    def test_creating_student_supervisor_allows_blank_email(self):
        response = self.client.post(
            reverse("student-supervisor-create"),
            {
                "username": "blank-email-student-supervisor",
                "password": "password123",
                "first_name": "Morgan",
                "last_name": "Blank",
                "email": "",
                "active_status": "on",
                "skill_notes": "",
                "monday_hours": "4",
                "tuesday_hours": "3",
                "wednesday_hours": "2",
                "thursday_hours": "4",
                "friday_hours": "1",
                "saturday_hours": "0",
                "sunday_hours": "0",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username="blank-email-student-supervisor")
        self.assertEqual(user.email, "")
        self.assertEqual(user.worker_profile.email, "")

    def test_creating_student_supervisor_uses_worker_profile_and_saves_weekly_hours(self):
        response = self.client.post(
            reverse("student-supervisor-create"),
            {
                "username": "lead-student",
                "password": "password123",
                "first_name": "Morgan",
                "last_name": "Lee",
                "email": "morgan@example.com",
                "active_status": "on",
                "skill_notes": "Can help triage and QA tasks",
                "monday_hours": "4",
                "tuesday_hours": "3",
                "wednesday_hours": "2",
                "thursday_hours": "4",
                "friday_hours": "1",
                "saturday_hours": "0",
                "sunday_hours": "0",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username="lead-student")
        self.assertEqual(user.role, UserRole.STUDENT_SUPERVISOR)
        profile = user.worker_profile
        self.assertEqual(profile.display_name, "Morgan Lee")
        self.assertEqual(profile.weekly_availability.get(weekday=Weekday.MONDAY).hours_available, 4)
        self.assertEqual(profile.weekly_availability.get(weekday=Weekday.THURSDAY).hours_available, 4)

    def test_removing_student_reassigns_tasks_and_recurring_templates_to_current_supervisor(self):
        response = self.client.post(reverse("worker-delete", args=[self.student.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(pk=self.student.pk).exists())
        self.task.refresh_from_db()
        self.recurring_template.refresh_from_db()
        self.assertEqual(self.task.assigned_to, self.supervisor)
        self.assertEqual(self.recurring_template.assign_to, self.supervisor)

    def test_removing_supervisor_reassigns_tasks_and_recurring_templates_to_current_supervisor(self):
        supervisor_task = Task.objects.create(
            title="Assigned to removed supervisor",
            description="Supervisor cleanup",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            assigned_to=self.other_supervisor,
            created_by=self.supervisor,
        )
        supervisor_template = RecurringTaskTemplate.objects.create(
            title="Supervisor recurring",
            description="Supervisor recurring cleanup",
            priority=Priority.MEDIUM,
            estimated_minutes=20,
            assign_to=self.other_supervisor,
            requested_by=self.supervisor,
            recurrence_pattern="weekly",
            recurrence_interval=1,
        )

        response = self.client.post(reverse("supervisor-delete", args=[self.other_supervisor.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(pk=self.other_supervisor.pk).exists())
        supervisor_task.refresh_from_db()
        supervisor_template.refresh_from_db()
        self.assertEqual(supervisor_task.assigned_to, self.supervisor)
        self.assertEqual(supervisor_template.assign_to, self.supervisor)

    def test_creating_supervisor_allows_blank_email(self):
        response = self.client.post(
            reverse("supervisor-create"),
            {
                "username": "blank-email-supervisor",
                "password": "password123",
                "first_name": "Avery",
                "last_name": "Blank",
                "email": "",
                "assignable_to_tasks": "on",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username="blank-email-supervisor")
        self.assertEqual(user.role, UserRole.SUPERVISOR)
        self.assertEqual(user.email, "")

    def test_supervisor_edit_page_hides_old_fallback_explanation(self):
        response = self.client.get(reverse("supervisor-edit", args=[self.other_supervisor.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Turn this off if tasks should never fall back to this supervisor when no worker has enough time available.")


    def test_supervisor_edit_updates_assignment_eligibility(self):
        response = self.client.post(
            reverse("supervisor-edit", args=[self.other_supervisor.pk]),
            {
                "username": self.other_supervisor.username,
                "first_name": "Avery",
                "last_name": "Supervisor",
                "email": "avery@example.com",
                "assignable_to_tasks": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Remove supervisor")
        self.other_supervisor.refresh_from_db()
        self.assertEqual(self.other_supervisor.first_name, "Avery")
        self.assertFalse(self.other_supervisor.assignable_to_tasks)


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

class SupervisorRoutePermissionTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="perm-supervisor", password="password123", role=UserRole.SUPERVISOR)
        self.student_worker = User.objects.create_user(username="perm-worker", password="password123", role=UserRole.STUDENT_WORKER)
        self.student_supervisor = User.objects.create_user(username="perm-student-supervisor", password="password123", role=UserRole.STUDENT_SUPERVISOR)
        StudentWorkerProfile.objects.create(user=self.student_worker, display_name="Perm Worker", email="perm-worker@example.com")
        StudentWorkerProfile.objects.create(user=self.student_supervisor, display_name="Perm Lead", email="perm-lead@example.com")
        self.template = RecurringTaskTemplate.objects.create(
            title="Permission recurring",
            description="Permission check",
            priority=Priority.MEDIUM,
            recurrence_pattern="weekly",
            recurrence_interval=1,
            next_run_date=date(2026, 3, 20),
            requested_by=self.supervisor,
        )

    def test_reports_requires_full_supervisor_role(self):
        self.client.force_login(self.student_worker)
        worker_response = self.client.get(reverse("reports"))
        self.assertEqual(worker_response.status_code, 403)

        self.client.force_login(self.student_supervisor)
        lead_response = self.client.get(reverse("reports"))
        self.assertEqual(lead_response.status_code, 403)

    def test_admin_guide_requires_full_supervisor_role(self):
        self.client.force_login(self.student_worker)
        worker_response = self.client.get(reverse("admin-guide"))
        self.assertEqual(worker_response.status_code, 403)

        self.client.force_login(self.student_supervisor)
        lead_response = self.client.get(reverse("admin-guide"))
        self.assertEqual(lead_response.status_code, 403)

    def test_recurring_run_now_requires_full_supervisor_role(self):
        self.client.force_login(self.student_supervisor)
        response = self.client.post(reverse("recurring-run-now", args=[self.template.pk]))
        self.assertEqual(response.status_code, 403)



@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class ReportsViewTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Reports Team")
        self.supervisor = User.objects.create_user(username="reports-supervisor", password="password123", role=UserRole.SUPERVISOR, team=self.team)
        self.worker = User.objects.create_user(username="reports-worker", password="password123", role=UserRole.STUDENT_WORKER, team=self.team)
        self.profile = StudentWorkerProfile.objects.create(user=self.worker, display_name="Taylor Reports", email="reports@example.com")
        for weekday in Weekday.values:
            StudentAvailability.objects.create(profile=self.profile, weekday=weekday, hours_available=4 if weekday < 5 else 0)

        self.completed_task = Task.objects.create(
            title="Completed task",
            description="Done this week",
            priority=Priority.MEDIUM,
            status=TaskStatus.DONE,
            assigned_to=self.worker,
            created_by=self.supervisor,
            completed_at=timezone.make_aware(datetime(2026, 3, 18, 10, 0)),
        )
        self._set_created_at(self.completed_task, timezone.make_aware(datetime(2026, 3, 17, 9, 0)))

        self.overdue_task = Task.objects.create(
            title="Overdue task",
            description="Overdue",
            priority=Priority.HIGH,
            status=TaskStatus.NEW,
            assigned_to=self.worker,
            created_by=self.supervisor,
            due_date=date(2026, 3, 19),
            estimated_minutes=120,
        )
        self._set_created_at(self.overdue_task, timezone.make_aware(datetime(2026, 3, 17, 8, 0)))

        self.waiting_task = Task.objects.create(
            title="Waiting task",
            description="Waiting",
            priority=Priority.MEDIUM,
            status=TaskStatus.WAITING,
            assigned_to=self.worker,
            created_by=self.supervisor,
            due_date=date(2026, 3, 21),
            estimated_minutes=60,
        )
        self._set_created_at(self.waiting_task, timezone.make_aware(datetime(2026, 3, 18, 8, 30)))

        self.generated_recurring_task = Task.objects.create(
            title="Generated recurring task",
            description="Created by recurring workflow",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            recurring_task=True,
            assigned_to=self.worker,
            created_by=self.supervisor,
            estimated_minutes=30,
        )
        self._set_created_at(self.generated_recurring_task, timezone.make_aware(datetime(2026, 3, 19, 9, 0)))

        RecurringTaskTemplate.objects.create(
            title="Report recurring",
            description="Recurring report",
            priority=Priority.MEDIUM,
            recurrence_pattern="weekly",
            recurrence_interval=1,
            next_run_date=date(2027, 3, 21),
            requested_by=self.supervisor,
            active=True,
        )
        self.recurring_event = TaskAuditEvent.objects.create(
            task=self.generated_recurring_task,
            task_title="Recurring report",
            action=TaskAuditAction.RECURRING_RUN,
            summary="Generated recurring task",
        )
        self._set_created_at(self.recurring_event, timezone.make_aware(datetime(2026, 3, 19, 9, 0)))

        self.client.force_login(self.supervisor)

    def _set_created_at(self, instance, value):
        instance.__class__.objects.filter(pk=instance.pk).update(created_at=value)
        instance.refresh_from_db()

    def test_reports_view_renders_weekly_metrics_history_and_worker_table(self):
        with patch("workboard.report_views.timezone.localdate", return_value=date(2026, 3, 20)):
            response = self.client.get(reverse("reports"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["period"], "week")
        self.assertEqual(response.context["period_start"], date(2026, 3, 16))
        self.assertEqual(response.context["period_end"], date(2026, 3, 22))
        summary = {card["label"]: card["value"] for card in response.context["summary_cards"]}
        self.assertEqual(summary["Completed this week"], 1)
        self.assertEqual(summary["Created this week"], 4)
        self.assertEqual(summary["Due this week"], 2)
        self.assertEqual(summary["Open due by week end"], 2)
        self.assertEqual(summary["Recurring runs this week"], 1)
        self.assertContains(response, "Weekly report")
        self.assertContains(response, "Past reports")
        self.assertContains(response, "Export CSV")
        self.assertContains(response, "Taylor Reports")
        self.assertEqual(response.context["export_url"], f"{reverse('reports')}?period=week&anchor=2026-03-16&export=csv")
        self.assertEqual(response.context["history_entries"][0]["start"], date(2026, 3, 16))

    def test_reports_view_supports_monthly_report_selection(self):
        with patch("workboard.report_views.timezone.localdate", return_value=date(2026, 3, 20)):
            response = self.client.get(reverse("reports"), {"period": "month", "anchor": "2026-03-20"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["period"], "month")
        self.assertEqual(response.context["period_start"], date(2026, 3, 1))
        self.assertEqual(response.context["period_end"], date(2026, 3, 31))
        summary = {card["label"]: card["value"] for card in response.context["summary_cards"]}
        self.assertEqual(summary["Completed this month"], 1)
        self.assertEqual(summary["Created this month"], 4)
        self.assertEqual(summary["Due this month"], 2)
        self.assertEqual(summary["Open due by month end"], 2)
        self.assertEqual(summary["Recurring runs this month"], 1)
        self.assertContains(response, "Monthly report for March 2026.")
        self.assertContains(response, "Current month")
        self.assertContains(response, "Previous month")

    def test_reports_view_can_show_past_month_reports(self):
        past_completed = Task.objects.create(
            title="February completed task",
            description="Closed in February",
            priority=Priority.MEDIUM,
            status=TaskStatus.DONE,
            assigned_to=self.worker,
            created_by=self.supervisor,
            due_date=date(2026, 2, 11),
            completed_at=timezone.make_aware(datetime(2026, 2, 12, 11, 0)),
        )
        self._set_created_at(past_completed, timezone.make_aware(datetime(2026, 2, 10, 9, 0)))
        past_waiting = Task.objects.create(
            title="February waiting task",
            description="Still open",
            priority=Priority.MEDIUM,
            status=TaskStatus.WAITING,
            assigned_to=self.worker,
            created_by=self.supervisor,
            due_date=date(2026, 2, 14),
            estimated_minutes=45,
        )
        self._set_created_at(past_waiting, timezone.make_aware(datetime(2026, 2, 13, 8, 0)))
        past_event = TaskAuditEvent.objects.create(
            task=past_waiting,
            task_title="February recurring report",
            action=TaskAuditAction.RECURRING_RUN,
            summary="Generated recurring task",
        )
        self._set_created_at(past_event, timezone.make_aware(datetime(2026, 2, 13, 9, 0)))

        with patch("workboard.report_views.timezone.localdate", return_value=date(2026, 3, 20)):
            response = self.client.get(reverse("reports"), {"period": "month", "anchor": "2026-02-10"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["period_start"], date(2026, 2, 1))
        self.assertEqual(response.context["period_end"], date(2026, 2, 28))
        summary = {card["label"]: card["value"] for card in response.context["summary_cards"]}
        self.assertEqual(summary["Completed this month"], 1)
        self.assertEqual(summary["Created this month"], 2)
        self.assertEqual(summary["Due this month"], 2)
        self.assertEqual(summary["Open due by month end"], 1)
        self.assertEqual(summary["Recurring runs this month"], 1)
        self.assertTrue(any(entry["is_selected"] and entry["start"] == date(2026, 2, 1) for entry in response.context["history_entries"]))
        self.assertContains(response, "Showing a past month")

    def test_reports_view_can_export_selected_report_as_csv(self):
        with patch("workboard.report_views.timezone.localdate", return_value=date(2026, 3, 20)):
            response = self.client.get(reverse("reports"), {"period": "month", "anchor": "2026-03-20", "export": "csv"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("taskforge-month-report-2026-03-01-to-2026-03-31.csv", response["Content-Disposition"])
        content = response.content.decode()
        self.assertIn("TaskForge report", content)
        self.assertIn("Report type,Monthly", content)
        self.assertIn("Completed this month,1", content)
        self.assertIn("Taylor Reports", content)

class AdminGuideViewTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(username="guide-supervisor", password="password123", role=UserRole.SUPERVISOR)
        self.client.force_login(self.supervisor)

    def test_admin_guide_renders_key_sections(self):
        response = self.client.get(reverse("admin-guide"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Supervisor guide")
        self.assertContains(response, "Daily workflow")
        self.assertContains(response, "Recurring tasks")
        self.assertContains(response, "Schedules and temporary changes")
        self.assertContains(response, "Testing and checks")
        self.assertContains(response, reverse("reports"))



class SecurityHardeningTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username="sec-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
        )
        self.client.force_login(self.supervisor)

    def test_worker_create_generates_non_default_password_when_blank(self):
        response = self.client.post(
            reverse("worker-create"),
            {
                "username": "newworker",
                "password": "",
                "first_name": "New",
                "last_name": "Worker",
                "email": "",
                "active_status": "on",
                "skill_notes": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        worker = User.objects.get(username="newworker")
        self.assertTrue(worker.must_change_password)
        self.assertFalse(worker.check_password("changeme123"))

    def test_task_intake_form_rejects_unsupported_attachment_type(self):
        attachment = SimpleUploadedFile("dangerous.exe", b"boom", content_type="application/octet-stream")
        response = self.client.post(
            reverse("task-intake"),
            {"raw_message": "Please review this file.", "attachments": attachment},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unsupported file type")


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



class TeamHierarchyTests(TestCase):
    def setUp(self):
        self.team_alpha = Team.objects.create(name="Alpha", description="Alpha team")
        self.team_beta = Team.objects.create(name="Beta", description="Beta team")
        self.admin = User.objects.create_superuser(username="global-admin", password="password123")
        self.supervisor_alpha = User.objects.create_user(
            username="supervisor-alpha",
            password="password123",
            role=UserRole.SUPERVISOR,
            first_name="Alice",
            last_name="Alpha",
            team=self.team_alpha,
        )
        self.supervisor_beta = User.objects.create_user(
            username="supervisor-beta",
            password="password123",
            role=UserRole.SUPERVISOR,
            first_name="Ben",
            last_name="Beta",
            team=self.team_beta,
        )
        self.worker_alpha = User.objects.create_user(
            username="worker-alpha",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            first_name="Willa",
            last_name="Alpha",
            team=self.team_alpha,
        )
        self.worker_beta = User.objects.create_user(
            username="worker-beta",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            first_name="Wes",
            last_name="Beta",
            team=self.team_beta,
        )
        self.alpha_task = Task.objects.create(
            team=self.team_alpha,
            title="Alpha task",
            priority=Priority.MEDIUM,
            status=TaskStatus.NEW,
            due_date=date(2026, 3, 31),
            assigned_to=self.worker_alpha,
            created_by=self.supervisor_alpha,
        )
        self.beta_task = Task.objects.create(
            team=self.team_beta,
            title="Beta task",
            priority=Priority.HIGH,
            status=TaskStatus.NEW,
            due_date=date(2026, 3, 31),
            assigned_to=self.worker_beta,
            created_by=self.supervisor_beta,
        )
        self.alpha_template = RecurringTaskTemplate.objects.create(
            team=self.team_alpha,
            title="Alpha recurring",
            description="Alpha recurring work",
            priority=Priority.MEDIUM,
            assign_to=self.worker_alpha,
            requested_by=self.supervisor_alpha,
            recurrence_pattern="weekly",
            recurrence_interval=1,
        )
        self.beta_template = RecurringTaskTemplate.objects.create(
            team=self.team_beta,
            title="Beta recurring",
            description="Beta recurring work",
            priority=Priority.MEDIUM,
            assign_to=self.worker_beta,
            requested_by=self.supervisor_beta,
            recurrence_pattern="weekly",
            recurrence_interval=1,
        )

    def test_supervisor_board_is_limited_to_their_team(self):
        self.client.force_login(self.supervisor_alpha)

        response = self.client.get(reverse("board"))

        self.assertContains(response, "Alpha task")
        self.assertNotContains(response, "Beta task")

    def test_supervisor_cannot_open_other_team_task_detail(self):
        self.client.force_login(self.supervisor_alpha)

        response = self.client.get(reverse("task-detail", args=[self.beta_task.pk]))

        self.assertEqual(response.status_code, 404)

    def test_recurring_list_is_team_scoped_for_supervisors(self):
        self.client.force_login(self.supervisor_alpha)

        response = self.client.get(reverse("recurring-list"))

        self.assertContains(response, "Alpha recurring")
        self.assertNotContains(response, "Beta recurring")

    def test_admin_people_page_shows_team_management(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("worker-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Teams")
        self.assertContains(response, reverse("team-create"))
        self.assertContains(response, self.team_alpha.name)
        self.assertContains(response, self.team_beta.name)

    def test_team_delete_moves_to_team_edit_page(self):
        self.client.force_login(self.admin)

        worker_list_response = self.client.get(reverse("worker-list"))
        self.assertEqual(worker_list_response.status_code, 200)
        self.assertNotContains(worker_list_response, "Delete team")

        edit_response = self.client.get(reverse("team-edit", args=[self.team_alpha.pk]))
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, "Delete team")


    def test_admin_can_create_team(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("team-create"),
            {"name": "Gamma", "description": "Gamma team"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Team.objects.filter(name="Gamma").exists())

    def test_supervisor_cannot_open_team_management_routes(self):
        self.client.force_login(self.supervisor_alpha)

        response = self.client.get(reverse("team-create"))

        self.assertEqual(response.status_code, 403)


