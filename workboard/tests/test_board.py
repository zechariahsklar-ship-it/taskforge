from datetime import date, datetime, time
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from ..models import Priority, RecurringTaskTemplate, StudentWorkerProfile, Task, TaskStatus, Team, User, UserRole


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
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

    def test_board_has_no_horizontal_scroll_markup(self):
        response = self.client.get(reverse("board"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'board-shell-horizontal', html=False)
        self.assertNotContains(response, 'board-grid-single-row', html=False)

    def test_board_redirects_to_my_tasks_when_viewport_too_narrow(self):
        self.client.cookies["tf_vw"] = "600"
        response = self.client.get(reverse("board"))

        self.assertRedirects(response, reverse("my-tasks"))

    def test_board_loads_normally_when_viewport_wide_enough(self):
        self.client.cookies["tf_vw"] = "1400"
        response = self.client.get(reverse("board"))

        self.assertEqual(response.status_code, 200)

    def test_board_loads_normally_when_viewport_cookie_missing(self):
        response = self.client.get(reverse("board"))

        self.assertEqual(response.status_code, 200)

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

    def test_board_overdue_view_excludes_today_tasks_before_evening_cutoff(self):
        with patch("workboard.task_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 17, 55))):
            response = self.client.get(reverse("board"), {"saved_view": "overdue"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Overdue archive cleanup")
        self.assertNotContains(response, "Front desk shift prep")

    def test_board_overdue_view_excludes_completed_tasks_even_after_evening_cutoff(self):
        self.scheduled_task.status = TaskStatus.DONE
        self.scheduled_task.completed_at = timezone.make_aware(datetime(2026, 3, 20, 17, 0))
        self.scheduled_task.save(update_fields=["status", "completed_at", "updated_at"])

        with patch("workboard.task_views.timezone.now", return_value=timezone.make_aware(datetime(2026, 3, 20, 18, 5))):
            response = self.client.get(reverse("board"), {"saved_view": "overdue"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Overdue archive cleanup")
        self.assertNotContains(response, "Front desk shift prep")

    def test_board_filter_bar_hides_schedule_and_status_controls(self):
        response = self.client.get(reverse("board"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="schedule_scope"', html=False)
        self.assertNotContains(response, 'name="completion_scope"', html=False)
        self.assertNotContains(response, '>Schedule</label>', html=False)
        self.assertNotContains(response, '>Status</label>', html=False)

    def test_board_hides_done_tasks_older_than_two_days(self):
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
            completed_at=timezone.make_aware(datetime(2026, 3, 19, 9, 0)),
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

    def test_my_tasks_hides_done_tasks_older_than_two_days(self):
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
            completed_at=timezone.make_aware(datetime(2026, 3, 19, 8, 0)),
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
        self.assertContains(response, "Completed in last 2 days")
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


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
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

    def test_task_delete_closes_board_order_gap(self):
        self.client.force_login(self.supervisor)
        response = self.client.post(reverse("task-delete", args=[self.second_task.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.third_task.refresh_from_db()
        self.assertEqual(self.task.board_order, 1)
        self.assertEqual(self.third_task.board_order, 2)


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

    def test_student_supervisor_can_create_tasks(self):
        self.client.force_login(self.student_supervisor)
        response = self.client.get(reverse("task-create"))
        self.assertEqual(response.status_code, 200)

    def test_student_supervisor_can_move_other_workers_task_on_board(self):
        self.client.force_login(self.student_supervisor)
        response = self.client.post(reverse("board-task-move", args=[self.task.pk]), {"status": TaskStatus.IN_PROGRESS})
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TaskStatus.IN_PROGRESS)
