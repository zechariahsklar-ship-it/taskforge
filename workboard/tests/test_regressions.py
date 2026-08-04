from django.test import TestCase
from ..models import BlackoutDate, RecurringTaskTemplate, Task, TaskStatus, User, UserRole, WorkerTag


class ScopedQuerysetEmptyInputRegressionTests(TestCase):
    # `queryset or SomeModel.objects.all()` silently discards an explicitly
    # passed-in queryset whenever it happens to be empty, because an empty
    # QuerySet is falsy in Python - `or` then falls back to "everything".
    # `_backfill_orphan_recurring_tasks` hit this on every steady-state page
    # load (zero true orphans -> the "no orphans" queryset looked falsy ->
    # every recurring task got needlessly re-synced to its template). These
    # tests lock in that every such helper uses an explicit `is None` check
    # instead, so passing a real-but-empty queryset is respected as "these
    # zero rows", not silently swapped for the unfiltered default.
    def setUp(self):
        self.supervisor = User.objects.create_user(username="scope-empty-sup", password="password123", role=UserRole.SUPERVISOR)
        Task.objects.create(title="Should stay excluded", status=TaskStatus.NEW, created_by=self.supervisor)

    def test_team_scoped_task_queryset_respects_empty_input(self):
        from ..task_views import _team_scoped_task_queryset

        empty_queryset = Task.objects.none()
        result = _team_scoped_task_queryset(self.supervisor, empty_queryset)

        self.assertEqual(result.count(), 0)

    def test_scoped_users_respects_empty_input(self):
        from ..people_views import _scoped_users

        empty_queryset = User.objects.none()
        result = _scoped_users(self.supervisor, empty_queryset)

        self.assertEqual(result.count(), 0)

    def test_scoped_worker_tags_respects_empty_input(self):
        from ..people_views import _scoped_worker_tags

        empty_queryset = WorkerTag.objects.none()
        result = _scoped_worker_tags(self.supervisor, empty_queryset)

        self.assertEqual(result.count(), 0)

    def test_scoped_blackout_dates_respects_empty_input(self):
        from ..people_views import _scoped_blackout_dates

        empty_queryset = BlackoutDate.objects.none()
        result = _scoped_blackout_dates(self.supervisor, empty_queryset)

        self.assertEqual(result.count(), 0)

    def test_scoped_recurring_templates_respects_empty_input(self):
        from ..recurring_views import _scoped_recurring_templates

        empty_queryset = RecurringTaskTemplate.objects.none()
        result = _scoped_recurring_templates(self.supervisor, empty_queryset)

        self.assertEqual(result.count(), 0)

    def test_backfill_orphan_recurring_tasks_is_a_noop_with_zero_orphans(self):
        from ..task_views import _backfill_orphan_recurring_tasks

        template_count_before = RecurringTaskTemplate.objects.count()
        # No recurring-flagged tasks exist in this test's data, so there are
        # zero orphans; this must not fall back to processing every task.
        _backfill_orphan_recurring_tasks(user=self.supervisor)

        self.assertEqual(RecurringTaskTemplate.objects.count(), template_count_before)
