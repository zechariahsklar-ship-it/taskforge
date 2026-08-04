from datetime import date, datetime
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from ..models import Priority, RecurringTaskTemplate, StudentAvailability, StudentWorkerProfile, Task, TaskAuditAction, TaskAuditEvent, TaskStatus, Team, User, UserRole, Weekday


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
        self.assertEqual(response.context["anchor_value"], "2026-03-16")
        self.assertEqual(response.context["anchor_options"], [{"value": "2026-03-16", "label": "Mar 16, 2026"}])
        self.assertEqual(
            response.context["anchor_options_by_period"],
            {
                "week": [{"value": "2026-03-16", "label": "Mar 16, 2026"}],
                "month": [{"value": "2026-03-01", "label": "March 2026"}],
            },
        )
        self.assertEqual(
            response.context["selected_anchor_by_period"],
            {"week": "2026-03-16", "month": "2026-03-01"},
        )
        summary = {card["label"]: card["value"] for card in response.context["summary_cards"]}
        self.assertEqual(summary["Completed this week"], 1)
        self.assertEqual(summary["Created this week"], 4)
        self.assertEqual(summary["Due this week"], 2)
        self.assertEqual(summary["Open due by week end"], 2)
        self.assertEqual(summary["Recurring runs this week"], 1)
        self.assertContains(response, "Weekly report")
        self.assertNotContains(response, "Past reports")
        self.assertNotContains(response, "Report details")
        self.assertContains(response, "Export CSV")
        self.assertContains(response, "Taylor Reports")
        self.assertContains(response, '<select name="anchor" id="id_anchor">', html=False)
        self.assertContains(response, 'id="report-anchor-options"', html=False)
        self.assertContains(response, 'id="report-selected-anchors"', html=False)
        self.assertNotContains(response, 'type="date" name="anchor"', html=False)
        self.assertEqual(response.context["export_url"], f"{reverse('reports')}?period=week&anchor=2026-03-16&export=csv")

    def test_reports_view_supports_monthly_report_selection(self):
        with patch("workboard.report_views.timezone.localdate", return_value=date(2026, 3, 20)):
            response = self.client.get(reverse("reports"), {"period": "month", "anchor": "2026-03-01"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["period"], "month")
        self.assertEqual(response.context["period_start"], date(2026, 3, 1))
        self.assertEqual(response.context["period_end"], date(2026, 3, 31))
        self.assertEqual(response.context["anchor_options"], [{"value": "2026-03-01", "label": "March 2026"}])
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
            response = self.client.get(reverse("reports"), {"period": "month", "anchor": "2026-02-01"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["period_start"], date(2026, 2, 1))
        self.assertEqual(response.context["period_end"], date(2026, 2, 28))
        self.assertEqual(
            response.context["anchor_options"][:2],
            [
                {"value": "2026-03-01", "label": "March 2026"},
                {"value": "2026-02-01", "label": "February 2026"},
            ],
        )
        self.assertEqual(response.context["selected_anchor_by_period"]["week"], "2026-02-09")
        summary = {card["label"]: card["value"] for card in response.context["summary_cards"]}
        self.assertEqual(summary["Completed this month"], 1)
        self.assertEqual(summary["Created this month"], 2)
        self.assertEqual(summary["Due this month"], 2)
        self.assertEqual(summary["Open due by month end"], 1)
        self.assertEqual(summary["Recurring runs this month"], 1)
        self.assertContains(response, "Showing a past month")

    def test_reports_view_can_export_selected_report_as_csv(self):
        with patch("workboard.report_views.timezone.localdate", return_value=date(2026, 3, 20)):
            response = self.client.get(reverse("reports"), {"period": "month", "anchor": "2026-03-01", "export": "csv"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("taskforge-month-report-2026-03-01-to-2026-03-31.csv", response["Content-Disposition"])
        content = response.content.decode()
        self.assertIn("TaskForge report", content)
        self.assertIn("Report type,Monthly", content)
        self.assertIn("Completed this month,1", content)
        self.assertIn("Taylor Reports", content)
