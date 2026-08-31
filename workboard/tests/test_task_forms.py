from datetime import date, datetime, time
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from ..models import Priority, StudentAvailability, StudentAvailabilityBlock, StudentWorkerProfile, Task, TaskStatus, Team, User, UserRole, Weekday, WorkerTag
from ..services import TaskAssignmentService


class TaskCreateDuplicateToStudentsTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username="dup-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
        )
        self.worker_a = User.objects.create_user(
            username="dup-worker-a",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            first_name="Ada",
            last_name="Worker",
        )
        self.worker_b = User.objects.create_user(
            username="dup-worker-b",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            first_name="Bo",
            last_name="Worker",
        )
        for worker in (self.worker_a, self.worker_b):
            StudentWorkerProfile.objects.create(
                user=worker,
                display_name=f"{worker.first_name} {worker.last_name}",
                email=f"{worker.username}@example.com",
                normal_shift_availability="",
            )
        self.client.force_login(self.supervisor)

    def _post_data(self, **overrides):
        data = {
            "title": "Prep welcome packets",
            "description": "Assemble packets for new hires",
            "priority": Priority.MEDIUM,
            "status": TaskStatus.NEW,
            "due_date": "",
            "respond_to_text": "",
            "estimated_minutes": "",
            "assigned_to": "",
            "recurring_task": "",
            "recurrence_pattern": "",
            "recurrence_interval": "",
            "recurrence_day_of_week": "",
            "recurrence_day_of_month": "",
        }
        data.update(overrides)
        return data

    def test_duplicate_to_students_creates_independent_tasks(self):
        response = self.client.post(
            reverse("task-create"),
            self._post_data(duplicate_to_students=[str(self.worker_a.pk), str(self.worker_b.pk)]),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        tasks = list(Task.objects.filter(title="Prep welcome packets").order_by("pk"))
        self.assertEqual(len(tasks), 2)
        self.assertEqual({task.assigned_to_id for task in tasks}, {self.worker_a.pk, self.worker_b.pk})
        self.assertEqual(tasks[0].additional_assignees.count(), 0)
        self.assertEqual(tasks[1].additional_assignees.count(), 0)

        # Fully independent copies: updating one leaves the other untouched.
        tasks[0].status = TaskStatus.IN_PROGRESS
        tasks[0].save()
        self.assertEqual(Task.objects.get(pk=tasks[1].pk).status, TaskStatus.NEW)

    def test_duplicate_to_students_rejects_student_missing_required_tag(self):
        required_tag = WorkerTag.objects.create(name="Front desk trained")
        self.worker_a.worker_profile.tags.add(required_tag)

        response = self.client.post(
            reverse("task-create"),
            self._post_data(
                duplicate_to_students=[str(self.worker_a.pk), str(self.worker_b.pk)],
                required_worker_tags=[str(required_tag.pk)],
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Task.objects.filter(title="Prep welcome packets").exists())


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


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
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

    def test_task_create_auto_assignment_uses_temporary_override_for_target_day(self):
        self.morning_worker.worker_profile.schedule_overrides.create(
            override_date=date(2026, 3, 16),
            note="Morning worker is out.",
        )
        afternoon_override = self.afternoon_worker.worker_profile.schedule_overrides.create(
            override_date=date(2026, 3, 16),
            note="Afternoon worker is covering the morning.",
        )
        afternoon_override.blocks.create(start_time=time(9, 0), end_time=time(11, 0), position=1)

        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Override-driven morning coverage",
                "description": "Use the temporary override instead of the weekly schedule.",
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
        task = Task.objects.get(title="Override-driven morning coverage")
        self.assertEqual(task.assigned_to, self.afternoon_worker)
        self.assertEqual(task.scheduled_date, date(2026, 3, 16))
        self.assertEqual(task.scheduled_start_time, time(9, 30))
        self.assertEqual(task.scheduled_end_time, time(10, 30))

    def test_task_create_accepts_manual_assignee_when_temporary_override_matches_window(self):
        afternoon_override = self.afternoon_worker.worker_profile.schedule_overrides.create(
            override_date=date(2026, 3, 16),
            note="Afternoon worker is covering the morning.",
        )
        afternoon_override.blocks.create(start_time=time(9, 0), end_time=time(11, 0), position=1)

        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Manual override assignment",
                "description": "Manual assignment should respect the temporary override.",
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
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        task = Task.objects.get(title="Manual override assignment")
        self.assertEqual(task.assigned_to, self.afternoon_worker)
        self.assertEqual(task.scheduled_date, date(2026, 3, 16))
        self.assertEqual(task.scheduled_start_time, time(9, 30))
        self.assertEqual(task.scheduled_end_time, time(10, 30))

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

    def test_daily_recurring_task_with_multi_day_window_is_created_without_a_fixed_window(self):
        # Picking a window on two different days for a repeating task (e.g.
        # trying to represent "every day" by selecting Monday and Tuesday,
        # rather than relying on the Daily cadence) isn't supported as a
        # per-day schedule - rather than blocking creation, TaskForge drops
        # the ambiguous window and creates the task anyway, relying on the
        # recurrence cadence and each cycle's normal assignment instead.
        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Daily standup",
                "description": "",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "",
                "scheduled_week_of": "2026-03-16",
                "task_window_day_0_segments": '[["09:00", "09:30"]]',
                "task_window_day_1_segments": '[["09:00", "09:30"]]',
                "respond_to_text": "",
                "estimated_minutes": "30",
                "assigned_to": "",
                "requested_by": "",
                "recurring_task": "on",
                "recurrence_pattern": "daily",
                "recurrence_interval": "1",
                "recurrence_day_of_week": "",
                "recurrence_day_of_month": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        task = Task.objects.get(title="Daily standup")
        self.assertTrue(task.recurring_task)
        self.assertIsNone(task.scheduled_date)
        self.assertIsNone(task.scheduled_start_time)
        self.assertIsNone(task.scheduled_end_time)
        self.assertIsNotNone(task.recurring_template)
        self.assertIsNone(task.recurring_template.scheduled_start_time)
        self.assertContains(response, "only one scheduled time block is supported")

    def test_daily_recurring_task_accepts_a_single_day_scheduled_window(self):
        response = self.client.post(
            reverse("task-create"),
            {
                "title": "Daily standup",
                "description": "",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "",
                "scheduled_week_of": "2026-03-16",
                "task_window_day_0_segments": '[["09:00", "09:30"]]',
                "respond_to_text": "",
                "estimated_minutes": "30",
                "assigned_to": "",
                "requested_by": "",
                "recurring_task": "on",
                "recurrence_pattern": "daily",
                "recurrence_interval": "1",
                "recurrence_day_of_week": "",
                "recurrence_day_of_month": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        task = Task.objects.get(title="Daily standup")
        self.assertTrue(task.recurring_task)
        self.assertIsNotNone(task.recurring_template)
        self.assertEqual(task.recurring_template.recurrence_pattern, "daily")

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
        # The assignment service sizes each candidate's remaining capacity
        # from "now" through the due date, so "now" needs to stay fixed
        # before that date - otherwise this silently loses its capacity
        # window (and thus its rotating candidates) once the real calendar
        # catches up to the hardcoded due date below.
        with patch("workboard.task_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 13, 9, 0))):
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


class TaskCreateAsAdminWithNoTeamTests(TestCase):
    # A superuser (is_admin) has no team of its own. The create-task form's
    # "Team" field is required and visible for admins, and previously had no
    # pre-selected value when nothing else resolved a team - the browser's
    # own required-field validation then silently blocked submission with a
    # native "please select an item" popup, and the assignment pickers
    # (Assign to, additional assignees, worker tags) rendered with zero
    # options since they were scoped to a None team. Both should now default
    # sensibly to the app's default team.
    def setUp(self):
        self.admin = User.objects.create_superuser(username="admin-no-team", password="password123", email="admin@example.com")
        self.assertIsNone(self.admin.team)

    def test_task_create_form_defaults_team_for_admin(self):
        response = self.client.force_login(self.admin) or self.client.get(reverse("task-create"))

        self.assertEqual(response.status_code, 200)
        default_team = Team.get_default_team()
        self.assertContains(response, f'<option value="{default_team.pk}" selected>', html=False)

    def test_admin_can_create_task_without_manually_choosing_team(self):
        # Simulates the admin leaving the form's pre-selected "Team" option
        # as-is (rather than the old behavior of the select showing a blank
        # placeholder) and submitting - "team" is included with the value
        # the form itself would have pre-filled.
        self.client.force_login(self.admin)
        default_team = Team.get_default_team()

        response = self.client.post(
            reverse("task-create"),
            {
                "team": str(default_team.pk),
                "title": "Admin task with default team",
                "description": "",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.NEW,
                "due_date": "",
                "respond_to_text": "",
                "estimated_minutes": "",
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
        task = Task.objects.get(title="Admin task with default team")
        self.assertEqual(task.team, Team.get_default_team())
