from datetime import date, datetime
from unittest.mock import patch
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from ..models import Priority, RecurringTaskTemplate, StudentAvailability, StudentWorkerProfile, Task, TaskChecklistItem, TaskStatus, User, UserRole, Weekday


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


class RecurringTaskPageLoadSweepTests(TestCase):
    # Backfilling a legacy recurring task onto a template used to happen only
    # from the generate_recurring_tasks management command or a visit to the
    # Recurring page - never from ordinary page traffic. If the scheduled
    # job wasn't running and nobody happened to open the Recurring page, that
    # task's daily/weekly/monthly cycle would never advance no matter how
    # much of the rest of the site got used. Regular page loads now run the
    # same backfill as part of the request-time recurring sweep.
    def setUp(self):
        self.supervisor = User.objects.create_user(username="sweep-sup", password="password123", role=UserRole.SUPERVISOR)
        self.worker = User.objects.create_user(username="sweep-worker", password="password123", role=UserRole.STUDENT_WORKER)
        self.legacy_task = Task.objects.create(
            title="Legacy daily watering",
            description="Water the plants",
            priority=Priority.MEDIUM,
            status=TaskStatus.DONE,
            due_date=date(2026, 3, 12),
            assigned_to=self.worker,
            requested_by=self.supervisor,
            created_by=self.supervisor,
            recurring_task=True,
            recurrence_pattern="daily",
            recurrence_interval=1,
            completed_at=timezone.make_aware(datetime(2026, 3, 12, 17, 0)),
        )

    def test_ordinary_page_load_backfills_legacy_task_and_generates_next_cycle(self):
        self.assertIsNone(self.legacy_task.recurring_template)

        self.client.force_login(self.worker)
        with patch("workboard.recurring_service.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 13, 9, 0))):
            response = self.client.get(reverse("my-tasks"))

        self.assertEqual(response.status_code, 200)
        self.legacy_task.refresh_from_db()
        self.assertIsNotNone(self.legacy_task.recurring_template)
        generated = Task.objects.filter(recurring_template=self.legacy_task.recurring_template).exclude(pk=self.legacy_task.pk)
        self.assertTrue(generated.exists())


class DailyWeekdayRecurrenceTests(TestCase):
    # "Daily" means weekday-daily (no Saturday/Sunday cycles), and a daily
    # template that's fallen multiple days behind should only generate its
    # most recently due cycle - not flood the board with one task per day
    # that was missed while nobody checked the site.
    def setUp(self):
        self.supervisor = User.objects.create_user(username="weekday-sup", password="password123", role=UserRole.SUPERVISOR)
        self.worker = User.objects.create_user(username="weekday-worker", password="password123", role=UserRole.STUDENT_WORKER)
        self.profile = StudentWorkerProfile.objects.create(user=self.worker, display_name="Weekday Worker", email="weekday@example.com")
        for weekday in Weekday.values:
            StudentAvailability.objects.create(profile=self.profile, weekday=weekday, hours_available=4 if weekday < 5 else 0)
        self.template = RecurringTaskTemplate.objects.create(
            title="Daily plant watering",
            description="Water the office plants",
            priority=Priority.LOW,
            estimated_minutes=15,
            assign_to=None,
            requested_by=self.supervisor,
            recurrence_pattern="daily",
            recurrence_interval=1,
            # Thursday, 2026-08-20
            next_run_date=date(2026, 8, 20),
        )
        self.previous_task = Task.objects.create(
            title="Daily plant watering",
            status=TaskStatus.DONE,
            due_date=date(2026, 8, 19),
            assigned_to=self.worker,
            requested_by=self.supervisor,
            created_by=self.supervisor,
            recurring_task=True,
            recurring_template=self.template,
            recurrence_pattern="daily",
            recurrence_interval=1,
            estimated_minutes=15,
            completed_at=timezone.make_aware(datetime(2026, 8, 19, 17, 0)),
        )

    def _run_generator_at(self, when):
        with patch("workboard.recurring_service.timezone.now", return_value=when):
            from ..recurring_service import RecurringTaskService

            return RecurringTaskService.run_templates_ready_today(now=when)

    def test_next_cycle_after_friday_skips_the_weekend(self):
        # next_run_date starts Thu 2026-08-20. A same-day check releases
        # Thursday's own task and advances to Friday (no weekend involved
        # yet); a second check on Friday releases Friday's task and must
        # advance past the weekend straight to Monday, not Saturday.
        self._run_generator_at(timezone.make_aware(datetime(2026, 8, 20, 9, 0)))
        self.template.refresh_from_db()
        self.assertEqual(self.template.next_run_date, date(2026, 8, 21))  # Friday

        self._run_generator_at(timezone.make_aware(datetime(2026, 8, 21, 9, 0)))
        self.template.refresh_from_db()
        self.assertEqual(self.template.next_run_date, date(2026, 8, 24))  # Monday, not Saturday

        tasks = list(Task.objects.filter(recurring_template=self.template).exclude(pk=self.previous_task.pk).order_by("due_date"))
        self.assertEqual([t.due_date for t in tasks], [date(2026, 8, 20), date(2026, 8, 21)])

    def test_multi_day_backlog_only_creates_the_most_recent_cycle(self):
        # Simulate the site going unchecked from Thu 8/20 (next_run_date,
        # the first ungenerated cycle) all the way to Thu 8/27 - five
        # weekdays are overdue (8/20, 8/21, 8/24, 8/25, 8/26, 8/27, skipping
        # the 8/22-8/23 weekend). Only 8/27's task should be created.
        created, _ = self._run_generator_at(timezone.make_aware(datetime(2026, 8, 27, 9, 0)))

        self.assertEqual(created, 1)
        self.template.refresh_from_db()
        tasks = list(Task.objects.filter(recurring_template=self.template).exclude(pk=self.previous_task.pk))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].due_date, date(2026, 8, 27))
        self.assertEqual(tasks[0].assigned_to, self.worker)
        # Next cycle should be the following weekday, not a pile-up of
        # every skipped day.
        self.assertEqual(self.template.next_run_date, date(2026, 8, 28))

    def test_legacy_weekend_next_run_date_is_normalized_forward(self):
        # A template that somehow ended up with next_run_date on a Saturday
        # (legacy data, or a hand-edited date) should get nudged to the
        # following Monday instead of never becoming ready.
        self.template.next_run_date = date(2026, 8, 22)  # Saturday
        self.template.save(update_fields=["next_run_date", "updated_at"])

        self._run_generator_at(timezone.make_aware(datetime(2026, 8, 24, 18, 5)))

        self.template.refresh_from_db()
        tasks = list(Task.objects.filter(recurring_template=self.template).exclude(pk=self.previous_task.pk))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].due_date, date(2026, 8, 24))  # Monday
        self.assertEqual(self.template.next_run_date, date(2026, 8, 25))
