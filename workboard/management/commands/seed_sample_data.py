from django.core.management.base import BaseCommand

from workboard.models import Priority, StudentWorkerProfile, Task, TaskStatus, User, UserRole


class Command(BaseCommand):
    help = "Create sample supervisors, student workers, and tasks for local development."

    def handle(self, *args, **options):
        supervisor, _ = User.objects.get_or_create(
            username="supervisor1",
            defaults={"role": UserRole.SUPERVISOR, "email": "supervisor@example.com"},
        )
        supervisor.set_password("password123")
        supervisor.role = UserRole.SUPERVISOR
        supervisor.save()

        workers = [
            {
                "username": "alex",
                "display_name": "Alex Carter",
                "email": "alex@example.com",
                "availability": "Mon/Wed/Fri 1pm-5pm",
                "skills": "Spreadsheet cleanup, content updates",
            },
            {
                "username": "jordan",
                "display_name": "Jordan Lee",
                "email": "jordan@example.com",
                "availability": "Tue/Thu 9am-1pm",
                "skills": "Research, inventory tracking",
            },
        ]

        for item in workers:
            user, _ = User.objects.get_or_create(
                username=item["username"],
                defaults={"role": UserRole.STUDENT_WORKER, "email": item["email"]},
            )
            user.set_password("password123")
            user.role = UserRole.STUDENT_WORKER
            user.email = item["email"]
            user.save()
            StudentWorkerProfile.objects.update_or_create(
                user=user,
                defaults={
                    "display_name": item["display_name"],
                    "email": item["email"],
                    "normal_shift_availability": item["availability"],
                    "max_hours_per_day": 4,
                    "skill_notes": item["skills"],
                },
            )

        alex = User.objects.get(username="alex")
        jordan = User.objects.get(username="jordan")
        task_defaults = [
            {
                "title": "Update office signage",
                "description": "Revise outdated signage and replace printed copies.",
                "priority": Priority.HIGH,
                "status": TaskStatus.ASSIGNED,
                "assigned_to": alex,
            },
            {
                "title": "Compile supply inventory",
                "description": "Count storage room supplies and note reorders.",
                "priority": Priority.MEDIUM,
                "status": TaskStatus.IN_PROGRESS,
                "assigned_to": jordan,
            },
            {
                "title": "Respond to faculty request for archive scan",
                "description": "Need scanned copy of last semester event flyer.",
                "priority": Priority.URGENT,
                "status": TaskStatus.NEW,
                "assigned_to": None,
            },
        ]
        for task_data in task_defaults:
            Task.objects.get_or_create(
                title=task_data["title"],
                defaults={
                    **task_data,
                    "created_by": supervisor,
                    "requested_by": supervisor,
                    "estimated_minutes": 45,
                    "raw_message": task_data["description"],
                },
            )

        self.stdout.write(self.style.SUCCESS("Sample data seeded."))
