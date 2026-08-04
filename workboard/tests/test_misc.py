from django.test import TestCase, override_settings
from django.urls import reverse
from ..models import StudentWorkerProfile, User, UserRole


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class PasswordToggleUiTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username="password-ui-supervisor",
            password="password123",
            role=UserRole.SUPERVISOR,
        )
        self.worker = User.objects.create_user(
            username="password-ui-worker",
            password="password123",
            role=UserRole.STUDENT_WORKER,
            first_name="Taylor",
            last_name="Helper",
        )
        self.worker_profile = StudentWorkerProfile.objects.create(
            user=self.worker,
            display_name="Taylor Helper",
            email="taylor.helper@example.com",
            normal_shift_availability="",
        )

    def test_login_page_renders_password_toggle_hook(self):
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "password_toggle.js")
        self.assertContains(response, 'data-password-toggle="true"', html=False)

    def test_password_change_and_reset_pages_render_password_toggle_hooks(self):
        self.client.force_login(self.supervisor)

        change_response = self.client.get(reverse("password-change"))
        reset_response = self.client.get(reverse("worker-password-reset", args=[self.worker.pk]))

        self.assertEqual(change_response.status_code, 200)
        self.assertContains(change_response, "password_toggle.js")
        self.assertContains(change_response, 'data-password-toggle="true"', count=3, html=False)
        self.assertEqual(reset_response.status_code, 200)
        self.assertContains(reset_response, 'data-password-toggle="true"', count=2, html=False)

    def test_create_account_pages_use_password_fields_with_toggle_hooks(self):
        self.client.force_login(self.supervisor)

        worker_response = self.client.get(reverse("worker-create"))
        supervisor_response = self.client.get(reverse("supervisor-create"))

        self.assertEqual(worker_response.status_code, 200)
        self.assertContains(worker_response, 'type="password"', html=False)
        self.assertContains(worker_response, 'name="password"', html=False)
        self.assertContains(worker_response, 'data-password-toggle="true"', html=False)
        self.assertEqual(supervisor_response.status_code, 200)
        self.assertContains(supervisor_response, 'type="password"', html=False)
        self.assertContains(supervisor_response, 'name="password"', html=False)
        self.assertContains(supervisor_response, 'data-password-toggle="true"', html=False)


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
