from datetime import date, datetime, time
import json
from unittest.mock import patch
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from ..models import BlackoutDate, Priority, RecurringTaskTemplate, ScheduleAdjustmentRequest, ScheduleAdjustmentRequestStatus, StudentAvailability, StudentAvailabilityBlock, StudentScheduleOverride, StudentWorkerProfile, Task, TaskAuditAction, TaskAuditEvent, TaskStatus, Team, User, UserRole, Weekday
from ..recurring_service import RecurringTaskService
from ..services import TaskAssignmentService


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
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
        with patch("workboard.people_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 9, 0))):
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

    def test_submitting_schedule_adjustment_request_creates_supervisor_review_task(self):
        self.client.force_login(self.student)
        with patch("workboard.people_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 9, 0))):
            self.client.post(
                reverse("schedule-adjustment-request"),
                {
                    "requested_date": "2026-03-24",
                    "note": "Need to swap tutoring hours.",
                    "request_segments": json.dumps([["13:00", "15:00"]]),
                },
                follow=True,
            )

        review_task = Task.objects.get(title__startswith="Review schedule change")
        self.assertEqual(review_task.assigned_to, self.supervisor)
        self.assertEqual(review_task.status, TaskStatus.NEW)
        self.assertEqual(review_task.due_date, date(2026, 3, 24))
        self.assertIn("Schedule Student", review_task.description)
        self.assertTrue(TaskAuditEvent.objects.filter(task=review_task, action=TaskAuditAction.CREATED).exists())

    def test_student_supervisor_can_open_schedule_adjustment_page(self):
        self.client.force_login(self.student_supervisor)
        response = self.client.get(reverse("schedule-adjustment-request"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Request a schedule adjustment")

    def test_student_request_page_auto_declines_past_pending_request(self):
        adjustment_request = ScheduleAdjustmentRequest.objects.create(
            profile=self.profile,
            requested_by=self.student,
            requested_date=date(2026, 3, 24),
            note="Old request should be auto-declined.",
        )
        adjustment_request.blocks.create(start_time=time(10, 0), end_time=time(12, 0), position=1)

        self.client.force_login(self.student)
        with patch("workboard.people_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 26, 9, 0))):
            response = self.client.get(reverse("schedule-adjustment-request"))

        self.assertEqual(response.status_code, 200)
        adjustment_request.refresh_from_db()
        self.assertEqual(adjustment_request.status, ScheduleAdjustmentRequestStatus.DECLINED)
        self.assertIsNotNone(adjustment_request.reviewed_at)
        self.assertContains(response, "Declined")
        self.assertNotContains(response, "Pending")

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
        with patch("workboard.people_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 9, 0))):
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
        self.assertContains(response, "Edit temporary schedule")
        self.assertContains(response, reverse("worker-schedule", args=[self.profile.pk]) + "?override_date=2026-03-24")
        self.assertTrue(
            TaskAssignmentService.user_is_available_for_window(
                self.student,
                scheduled_date=date(2026, 3, 24),
                scheduled_start_time=time(13, 30),
                scheduled_end_time=time(14, 30),
            )
        )

    def test_supervisor_can_decline_schedule_request_and_it_disappears_from_review_page(self):
        adjustment_request = ScheduleAdjustmentRequest.objects.create(
            profile=self.profile,
            requested_by=self.student,
            requested_date=date(2026, 3, 25),
            note="Need this declined request to disappear.",
        )
        adjustment_request.blocks.create(start_time=time(9, 0), end_time=time(11, 0), position=1)

        self.client.force_login(self.supervisor)
        with patch("workboard.people_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 9, 0))):
            response = self.client.post(
                reverse("schedule-adjustment-requests"),
                {"action": "decline_request", "schedule_request_id": str(adjustment_request.pk)},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        adjustment_request.refresh_from_db()
        self.assertEqual(adjustment_request.status, ScheduleAdjustmentRequestStatus.DECLINED)
        self.assertEqual(adjustment_request.reviewed_by, self.supervisor)
        self.assertIsNotNone(adjustment_request.reviewed_at)
        self.assertContains(response, "No pending schedule requests.")
        self.assertContains(response, "No applied schedule requests yet.")
        self.assertNotContains(response, "Need this declined request to disappear.")

    def test_supervisor_review_page_auto_declines_past_pending_request(self):
        expired_request = ScheduleAdjustmentRequest.objects.create(
            profile=self.profile,
            requested_by=self.student,
            requested_date=date(2026, 3, 24),
            note="Expired pending request.",
        )
        expired_request.blocks.create(start_time=time(8, 0), end_time=time(10, 0), position=1)
        active_request = ScheduleAdjustmentRequest.objects.create(
            profile=self.profile,
            requested_by=self.student,
            requested_date=date(2026, 3, 28),
            note="Still pending request.",
        )
        active_request.blocks.create(start_time=time(13, 0), end_time=time(15, 0), position=1)

        self.client.force_login(self.supervisor)
        with patch("workboard.people_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 26, 9, 0))):
            response = self.client.get(reverse("schedule-adjustment-requests"))

        self.assertEqual(response.status_code, 200)
        expired_request.refresh_from_db()
        active_request.refresh_from_db()
        self.assertEqual(expired_request.status, ScheduleAdjustmentRequestStatus.DECLINED)
        self.assertIsNotNone(expired_request.reviewed_at)
        self.assertEqual(active_request.status, ScheduleAdjustmentRequestStatus.PENDING)
        self.assertContains(response, "Still pending request.")
        self.assertNotContains(response, "Expired pending request.")

    def test_non_supervisor_cannot_open_schedule_request_review_page(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("schedule-adjustment-requests"))
        self.assertEqual(response.status_code, 403)


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class SelfScheduleViewTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username="self-schedule-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
        )
        self.student = User.objects.create_user(
            username="self-schedule-student",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            first_name="Taylor",
            last_name="Student",
        )
        self.student_profile = StudentWorkerProfile.objects.create(
            user=self.student,
            display_name="Taylor Student",
            email="taylor.student@example.com",
            normal_shift_availability="",
        )
        self.student_supervisor = User.objects.create_user(
            username="self-schedule-lead",
            password="password123",
            role=UserRole.STUDENT_SUPERVISOR,
            first_name="Jordan",
            last_name="Lead",
        )
        self.student_supervisor_profile = StudentWorkerProfile.objects.create(
            user=self.student_supervisor,
            display_name="Jordan Lead",
            email="jordan.lead@example.com",
            normal_shift_availability="",
        )
        self._set_blocks(self.student_profile, Weekday.MONDAY, [(time(9, 0), time(12, 0))])
        self._set_blocks(self.student_supervisor_profile, Weekday.TUESDAY, [(time(13, 0), time(16, 0))])
        override = self.student_profile.schedule_overrides.create(
            override_date=date(2026, 3, 24),
            note="Cover afternoon lab.",
            created_by=self.supervisor,
        )
        override.blocks.create(start_time=time(13, 0), end_time=time(15, 0), position=1)

    def _set_blocks(self, profile, weekday, blocks):
        availability, _ = StudentAvailability.objects.update_or_create(
            profile=profile,
            weekday=weekday,
            defaults={
                "start_time": blocks[0][0] if blocks else None,
                "end_time": blocks[-1][1] if blocks else None,
                "hours_available": sum(
                    (datetime.combine(date.today(), end_value) - datetime.combine(date.today(), start_value)).total_seconds() // 60
                    for start_value, end_value in blocks
                ) / 60 if blocks else 0,
            },
        )
        availability.blocks.all().delete()
        for position, (start_value, end_value) in enumerate(blocks, start=1):
            StudentAvailabilityBlock.objects.create(
                availability=availability,
                start_time=start_value,
                end_time=end_value,
                position=position,
            )

    def test_worker_can_view_read_only_self_schedule_and_nav_order(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("self-schedule"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "My Schedule")
        self.assertContains(response, "Taylor Student")
        self.assertContains(response, "9:00 AM - 12:00 PM (3 hrs)")
        self.assertContains(response, "Mar 24, 2026")
        self.assertContains(response, "1:00 PM - 3:00 PM (2 hrs)")
        self.assertContains(response, "Cover afternoon lab.")
        self.assertContains(response, 'data-weekly-schedule-picker', html=False)
        self.assertContains(response, 'data-read-only-schedule="true"', html=False)
        self.assertContains(response, 'data-slot-value="09:00"', html=False)
        self.assertNotContains(response, "Save weekly schedule")
        self.assertNotContains(response, "Save temporary schedule")
        self.assertNotContains(response, "Remove temporary schedule")
        self.assertNotContains(response, "Edit temporary schedule")
        self.assertNotContains(response, 'data-clear-week', html=False)
        self.assertNotContains(response, 'data-copy-day="monday"', html=False)
        content = response.content.decode()
        self.assertLess(content.index(reverse("completed-tasks")), content.index(reverse("self-schedule")))
        self.assertLess(content.index(reverse("self-schedule")), content.index(reverse("schedule-adjustment-request")))

    def test_student_supervisor_can_view_their_own_schedule(self):
        self.client.force_login(self.student_supervisor)
        response = self.client.get(reverse("self-schedule"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Jordan Lead")
        self.assertContains(response, "1:00 PM - 4:00 PM (3 hrs)")
        self.assertNotContains(response, "Cover afternoon lab.")

    def test_supervisor_cannot_open_self_schedule_page(self):
        self.client.force_login(self.supervisor)
        response = self.client.get(reverse("self-schedule"))

        self.assertEqual(response.status_code, 403)


class BlackoutDateTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Blackout Team")
        self.supervisor = User.objects.create_user(
            username="blackout-supervisor", password="password123", role=UserRole.SUPERVISOR, team=self.team,
        )
        self.worker = User.objects.create_user(
            username="blackout-worker", password="password123", role=UserRole.STUDENT_WORKER, team=self.team,
        )
        self.worker_profile = StudentWorkerProfile.objects.create(
            user=self.worker, display_name="Blackout Worker", email="blackout-worker@example.com",
        )
        self.template = RecurringTaskTemplate.objects.create(
            team=self.team,
            title="Lobby check",
            priority=Priority.MEDIUM,
            estimated_minutes=15,
            assign_to=self.worker,
            requested_by=self.supervisor,
            recurrence_pattern="daily",
            recurrence_interval=1,
            next_run_date=date(2026, 3, 20),
        )

    def test_blackout_date_is_unique_per_team(self):
        BlackoutDate.objects.create(team=self.team, date=date(2026, 3, 20), label="Break")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BlackoutDate.objects.create(team=self.team, date=date(2026, 3, 20), label="Duplicate")

    def test_supervisor_can_add_and_remove_blackout_date(self):
        self.client.force_login(self.supervisor)

        add_response = self.client.post(
            reverse("schedule-adjustment-requests"),
            {"action": "add_blackout_date", "date": "2026-11-26", "label": "Thanksgiving break"},
            follow=True,
        )
        self.assertEqual(add_response.status_code, 200)
        blackout_date = BlackoutDate.objects.get(team=self.team, date=date(2026, 11, 26))
        self.assertEqual(blackout_date.label, "Thanksgiving break")
        self.assertContains(add_response, "Thanksgiving break")

        delete_response = self.client.post(
            reverse("schedule-adjustment-requests"),
            {"action": "delete_blackout_date", "blackout_date_id": str(blackout_date.pk)},
            follow=True,
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertFalse(BlackoutDate.objects.filter(pk=blackout_date.pk).exists())

    def test_recurring_generation_skips_blackout_date_and_advances(self):
        # 2026-03-20 is a Friday, so the daily (weekday-only) template's next
        # cycle after skipping the blackout lands on Monday 2026-03-23, not
        # calendar-day Saturday 2026-03-21.
        BlackoutDate.objects.create(team=self.team, date=date(2026, 3, 20), label="Break")

        created_count, reopened_count = RecurringTaskService.run_templates_ready_today(
            now=timezone.make_aware(datetime(2026, 3, 20, 12, 0))
        )

        self.assertEqual(created_count, 0)
        self.assertEqual(reopened_count, 0)
        self.assertFalse(Task.objects.filter(recurring_template=self.template).exists())
        self.template.refresh_from_db()
        self.assertEqual(self.template.next_run_date, date(2026, 3, 23))

    def test_recurring_generation_creates_task_on_non_blackout_date(self):
        created_count, _ = RecurringTaskService.run_templates_ready_today(
            now=timezone.make_aware(datetime(2026, 3, 20, 12, 0))
        )

        self.assertEqual(created_count, 1)
        self.assertTrue(Task.objects.filter(recurring_template=self.template, due_date=date(2026, 3, 20)).exists())

    def test_student_can_still_request_schedule_change_on_blackout_date(self):
        BlackoutDate.objects.create(team=self.team, date=date(2026, 3, 20), label="Break")
        self.client.force_login(self.worker)

        with patch("workboard.people_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 15, 9, 0))):
            response = self.client.post(
                reverse("schedule-adjustment-request"),
                {
                    "requested_date": "2026-03-20",
                    "note": "Want to come in anyway.",
                    "request_segments": json.dumps([["09:00", "11:00"]]),
                },
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            ScheduleAdjustmentRequest.objects.filter(profile=self.worker_profile, requested_date=date(2026, 3, 20)).exists()
        )
