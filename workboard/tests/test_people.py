from datetime import date, datetime, time
import json
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from ..models import Priority, RecurringTaskTemplate, StudentAvailability, StudentAvailabilityBlock, StudentWorkerProfile, Task, TaskStatus, Team, User, UserRole, Weekday, WorkerTag
from ..services import TaskAssignmentService


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
        self.assertContains(response, "Student managers")
        self.assertContains(response, reverse("worker-edit", args=[self.profile.pk]))
        self.assertContains(response, reverse("worker-schedule", args=[self.profile.pk]))
        self.assertContains(response, reverse("worker-edit", args=[self.student_supervisor_profile.pk]))
        self.assertContains(response, reverse("worker-schedule", args=[self.student_supervisor_profile.pk]))
        self.assertContains(response, reverse("supervisor-edit", args=[self.other_supervisor.pk]))
        self.assertContains(response, "Edit worker")
        self.assertContains(response, "Edit student manager")
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

    def test_worker_edit_page_can_promote_a_student_worker_to_student_manager(self):
        response = self.client.post(
            reverse("worker-edit", args=[self.profile.pk]),
            {"action": "toggle_role"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.student.refresh_from_db()
        self.assertEqual(self.student.role, UserRole.STUDENT_SUPERVISOR)
        self.assertContains(response, "is now a Student Manager")

    def test_worker_edit_page_can_demote_a_student_manager_to_student_worker(self):
        response = self.client.post(
            reverse("worker-edit", args=[self.student_supervisor_profile.pk]),
            {"action": "toggle_role"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.student_supervisor.refresh_from_db()
        self.assertEqual(self.student_supervisor.role, UserRole.STUDENT_WORKER)
        self.assertContains(response, "is now a Student Worker")

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
        self.assertNotContains(create_response, 'data-copy-day="monday"')
        self.assertNotContains(create_response, 'data-clear-day="monday"')
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

        self.assertContains(student_supervisor_response, "Remove student manager")
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


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
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

    def test_supervisor_run_now_generates_task_on_their_team(self):
        self.client.force_login(self.supervisor_alpha)
        self.alpha_template.next_run_date = date(2026, 4, 6)
        self.alpha_template.save(update_fields=["next_run_date", "updated_at"])

        with patch("workboard.recurring_service.timezone.now", return_value=timezone.make_aware(datetime(2026, 4, 1, 9, 0))):
            response = self.client.post(reverse("recurring-run-now", args=[self.alpha_template.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        generated = Task.objects.get(recurring_template=self.alpha_template)
        self.assertEqual(generated.team, self.team_alpha)
        self.assertEqual(generated.assigned_to, self.worker_alpha)
        self.assertEqual(generated.created_by, self.supervisor_alpha)

    def test_supervisor_cannot_open_other_team_task_edit(self):
        self.client.force_login(self.supervisor_alpha)

        response = self.client.get(reverse("task-edit", args=[self.beta_task.pk]))

        self.assertEqual(response.status_code, 404)

    def test_supervisor_cannot_reassign_task_to_other_team_worker(self):
        self.client.force_login(self.supervisor_alpha)

        response = self.client.post(
            reverse("task-edit", args=[self.alpha_task.pk]),
            {
                "title": self.alpha_task.title,
                "description": self.alpha_task.description,
                "priority": self.alpha_task.priority,
                "status": self.alpha_task.status,
                "due_date": "2026-03-31",
                "scheduled_date": "",
                "scheduled_start_time": "",
                "scheduled_end_time": "",
                "respond_to_text": "",
                "estimated_minutes": "",
                "required_worker_tags": [],
                "assigned_to": str(self.worker_beta.pk),
                "additional_assignees": [],
                "rotating_additional_assignee_count": "0",
                "recurring_task": "",
                "recurrence_pattern": "",
                "recurrence_interval": "",
                "recurrence_day_of_week": "",
                "recurrence_day_of_month": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        self.alpha_task.refresh_from_db()
        self.assertEqual(self.alpha_task.assigned_to, self.worker_alpha)

    def test_supervisor_cannot_move_other_team_task_on_board(self):
        self.client.force_login(self.supervisor_alpha)

        response = self.client.post(
            reverse("board-task-move", args=[self.beta_task.pk]),
            {"status": TaskStatus.IN_PROGRESS},
        )

        self.assertEqual(response.status_code, 404)

    def test_supervisor_cannot_open_other_team_recurring_routes(self):
        self.client.force_login(self.supervisor_alpha)

        detail_response = self.client.get(reverse("recurring-detail", args=[self.beta_template.pk]))
        edit_response = self.client.get(reverse("recurring-edit", args=[self.beta_template.pk]))

        self.assertEqual(detail_response.status_code, 404)
        self.assertEqual(edit_response.status_code, 404)

    def test_supervisor_cannot_mutate_other_team_recurring_template(self):
        self.client.force_login(self.supervisor_alpha)
        original_task_count = Task.objects.filter(recurring_template=self.beta_template).count()
        original_next_run_date = self.beta_template.next_run_date

        move_response = self.client.post(
            reverse("recurring-move", args=[self.beta_template.pk]),
            {"before_template_id": str(self.alpha_template.pk)},
        )
        delete_response = self.client.post(reverse("recurring-delete", args=[self.beta_template.pk]))
        run_response = self.client.post(reverse("recurring-run-now", args=[self.beta_template.pk]))

        self.assertEqual(move_response.status_code, 404)
        self.assertEqual(delete_response.status_code, 404)
        self.assertEqual(run_response.status_code, 404)
        self.assertTrue(RecurringTaskTemplate.objects.filter(pk=self.beta_template.pk).exists())
        self.beta_template.refresh_from_db()
        self.assertEqual(Task.objects.filter(recurring_template=self.beta_template).count(), original_task_count)
        self.assertEqual(self.beta_template.next_run_date, original_next_run_date)

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
