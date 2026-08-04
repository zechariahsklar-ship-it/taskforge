from datetime import date
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from ..models import Priority, RecurringTaskTemplate, StudentWorkerProfile, User, UserRole


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
