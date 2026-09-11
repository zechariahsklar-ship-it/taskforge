from datetime import date, datetime, timedelta
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from ..models import Priority, StudentAvailability, StudentWorkerProfile, Task, TaskStatus, Team, User, UserRole, Weekday, WorkerTag
from ..services import TaskAssignmentService


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

    def test_suggest_assignee_without_an_estimate_still_skips_workers_who_do_not_work_that_day(self):
        # Regression test: when a task has no estimated_minutes and no
        # scheduled window, the candidate filter used to skip capacity
        # checking entirely. A worker with zero availability on the due
        # date's weekday (0 open tasks, never assigned before) would then
        # outrank a worker who actually works that day but has one
        # existing task, since open-task count is compared before capacity.
        due_date = date(2026, 3, 17)  # a Tuesday
        off_on_tuesdays = self._create_worker("off-tuesdays", "Casey Off Tuesdays")
        off_on_tuesdays.worker_profile.weekly_availability.filter(weekday=due_date.weekday()).update(hours_available=0)
        Task.objects.create(
            title="Existing load for the Tuesday worker",
            description="",
            priority=Priority.MEDIUM,
            status=TaskStatus.IN_PROGRESS,
            assigned_to=self.jordan,
            estimated_minutes=30,
            due_date=due_date + timedelta(days=3),
        )

        with patch("workboard.services.timezone.localdate", return_value=due_date):
            assignee, summary, rationale = TaskAssignmentService.suggest_assignee(
                due_date=due_date,
                estimated_minutes=None,
                fallback_supervisor=self.supervisor,
                exclude_user_ids=[self.alex.pk],
            )

        self.assertNotEqual(assignee, off_on_tuesdays)
        self.assertEqual(assignee, self.jordan)

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


class NextAvailableSupervisorTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Front Office")
        self.busy_supervisor = User.objects.create_user(
            username="next-sup-busy", password="password123", role=UserRole.SUPERVISOR, team=self.team,
        )
        self.free_supervisor = User.objects.create_user(
            username="next-sup-free", password="password123", role=UserRole.SUPERVISOR, team=self.team,
        )

    def test_picks_supervisor_with_fewer_open_tasks(self):
        Task.objects.create(title="Existing 1", status=TaskStatus.NEW, assigned_to=self.busy_supervisor, team=self.team)
        Task.objects.create(title="Existing 2", status=TaskStatus.NEW, assigned_to=self.busy_supervisor, team=self.team)

        chosen = TaskAssignmentService.next_available_supervisor(team=self.team)

        self.assertEqual(chosen, self.free_supervisor)

    def test_returns_none_when_no_supervisor_on_team(self):
        empty_team = Team.objects.create(name="No Supervisors")

        self.assertIsNone(TaskAssignmentService.next_available_supervisor(team=empty_team))
