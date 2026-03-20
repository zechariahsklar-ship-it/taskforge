from datetime import date, datetime, timedelta

from django.db.models import Max, Q, Sum
from django.utils import timezone

from .models import StudentAvailability, StudentScheduleOverride, StudentWorkerProfile, Task, TaskStatus, User, UserRole


def _format_time_label(value):
    return value.strftime("%I:%M %p").lstrip("0")


class TaskAssignmentService:
    @staticmethod
    def _worker_profiles(*, exclude_user_ids=None):
        workers = StudentWorkerProfile.objects.filter(
            active_status=True,
            user__role__in=UserRole.worker_roles(),
        ).select_related("user")
        if exclude_user_ids:
            workers = workers.exclude(user_id__in=exclude_user_ids)
        return workers

    @staticmethod
    def _worker_task_membership_filter(user):
        return (
            Q(assigned_to=user)
            | Q(additional_assignees=user)
            | Q(rotating_additional_assignees=user)
            | Q(rotating_additional_assignee=user)
        )

    @staticmethod
    def _candidate_sort_key(candidate):
        _, capacity, open_tasks, last_assigned_at = candidate
        return (
            last_assigned_at is not None,
            last_assigned_at or timezone.make_aware(datetime(2000, 1, 1)),
            open_tasks,
            -capacity,
        )

    @staticmethod
    def _candidate_metrics(profile, *, due_date):
        active_tasks = TaskAssignmentService._active_task_queryset_for_user(profile.user)
        return (
            TaskAssignmentService._remaining_capacity_minutes(profile, due_date),
            active_tasks.count(),
            active_tasks.aggregate(last_assigned=Max("created_at"))["last_assigned"],
        )

    @staticmethod
    def _candidate_pool(
        *,
        due_date,
        exclude_user_ids=None,
        scheduled_date=None,
        scheduled_start_time=None,
        scheduled_end_time=None,
        exclude_task_id=None,
    ):
        workers = TaskAssignmentService._worker_profiles(exclude_user_ids=exclude_user_ids)
        candidates = []
        for profile in workers:
            if scheduled_date and scheduled_start_time and scheduled_end_time:
                if not TaskAssignmentService.user_is_available_for_window(
                    profile.user,
                    scheduled_date=scheduled_date,
                    scheduled_start_time=scheduled_start_time,
                    scheduled_end_time=scheduled_end_time,
                    exclude_task_id=exclude_task_id,
                ):
                    continue
            capacity, open_tasks, last_assigned_at = TaskAssignmentService._candidate_metrics(
                profile,
                due_date=due_date,
            )
            candidates.append((profile, capacity, open_tasks, last_assigned_at))
        candidates.sort(key=TaskAssignmentService._candidate_sort_key)
        return candidates

    @staticmethod
    def suggest_assignee(
        *,
        due_date,
        estimated_minutes,
        fallback_supervisor=None,
        exclude_user_ids=None,
        scheduled_date=None,
        scheduled_start_time=None,
        scheduled_end_time=None,
        exclude_task_id=None,
    ):
        viable = TaskAssignmentService._matching_worker_candidates(
            due_date=due_date,
            estimated_minutes=estimated_minutes,
            exclude_user_ids=exclude_user_ids,
            scheduled_date=scheduled_date,
            scheduled_start_time=scheduled_start_time,
            scheduled_end_time=scheduled_end_time,
            exclude_task_id=exclude_task_id,
        )

        if viable:
            profile, capacity, _, _ = viable[0]
            if scheduled_date and scheduled_start_time and scheduled_end_time:
                summary = (
                    f"Suggested worker: {profile.display_name} based on schedule availability for "
                    f"{scheduled_date.isoformat()} {_format_time_label(scheduled_start_time)} - {_format_time_label(scheduled_end_time)}."
                )
                rationale = [
                    f"Recommended assignee: {profile.display_name}",
                    f"Available during the scheduled window on {scheduled_date.isoformat()}",
                    "Selection favors worker-level teammates who are scheduled at that time and do not have overlapping work.",
                ]
            else:
                summary = f"Suggested worker: {profile.display_name} based on current availability and assignment rotation. Remaining capacity before due date: {int(capacity)} minutes."
                rationale = [
                    f"Recommended assignee: {profile.display_name}",
                    f"Estimated open capacity before due date: {int(capacity)} minutes",
                    "Selection favors worker-level teammates with enough capacity and lighter recent assignment rotation.",
                ]
            return profile.user, summary, rationale

        if fallback_supervisor and fallback_supervisor.role == UserRole.SUPERVISOR and fallback_supervisor.assignable_to_tasks:
            return fallback_supervisor, "No worker has enough available capacity before the due date, so this task should stay with the supervising user.", [
                f"Recommended assignee: {fallback_supervisor.display_label}",
                "No worker currently has enough available hours before the due date window.",
                "Fallback rule assigned the task to the supervising user instead of rotating among supervisors.",
            ]
        return None, "No eligible worker has enough available capacity before the due date, and no supervisor fallback is available.", [
            "No worker currently has enough available hours before the due date window.",
            "No supervising fallback is available for automatic assignment.",
        ]

    @staticmethod
    def suggest_worker_assignee(
        *,
        due_date,
        estimated_minutes,
        exclude_user_ids=None,
        scheduled_date=None,
        scheduled_start_time=None,
        scheduled_end_time=None,
        exclude_task_id=None,
    ):
        viable = TaskAssignmentService._matching_worker_candidates(
            due_date=due_date,
            estimated_minutes=estimated_minutes,
            exclude_user_ids=exclude_user_ids,
            scheduled_date=scheduled_date,
            scheduled_start_time=scheduled_start_time,
            scheduled_end_time=scheduled_end_time,
            exclude_task_id=exclude_task_id,
        )
        if viable:
            return viable[0][0].user
        return None

    @staticmethod
    def worker_can_take_task(
        user,
        *,
        due_date,
        estimated_minutes,
        scheduled_date=None,
        scheduled_start_time=None,
        scheduled_end_time=None,
        exclude_task_id=None,
    ):
        if not user or user.role not in UserRole.worker_roles():
            return False
        try:
            profile = user.worker_profile
        except StudentWorkerProfile.DoesNotExist:
            return False
        if not profile.active_status:
            return False
        if scheduled_date and scheduled_start_time and scheduled_end_time:
            return TaskAssignmentService.user_is_available_for_window(
                user,
                scheduled_date=scheduled_date,
                scheduled_start_time=scheduled_start_time,
                scheduled_end_time=scheduled_end_time,
                exclude_task_id=exclude_task_id,
            )
        if estimated_minutes is None:
            return True
        return TaskAssignmentService._remaining_capacity_minutes(profile, due_date) >= estimated_minutes

    @staticmethod
    def suggest_worker_assignees(
        *,
        due_date,
        estimated_minutes,
        count,
        exclude_user_ids=None,
        preferred_user_ids=None,
        avoid_user_ids=None,
        scheduled_date=None,
        scheduled_start_time=None,
        scheduled_end_time=None,
        exclude_task_id=None,
    ):
        if count <= 0:
            return []

        selected_users = []
        excluded_ids = {user_id for user_id in (exclude_user_ids or []) if user_id}

        def maybe_add(user_id):
            if len(selected_users) >= count or not user_id or user_id in excluded_ids:
                return
            candidate = User.objects.filter(
                pk=user_id,
                role__in=UserRole.worker_roles(),
                worker_profile__active_status=True,
            ).first()
            if not candidate:
                return
            if not TaskAssignmentService.worker_can_take_task(
                candidate,
                due_date=due_date,
                estimated_minutes=estimated_minutes,
                scheduled_date=scheduled_date,
                scheduled_start_time=scheduled_start_time,
                scheduled_end_time=scheduled_end_time,
                exclude_task_id=exclude_task_id,
            ):
                return
            selected_users.append(candidate)
            excluded_ids.add(candidate.pk)

        # Keep preferred teammates when possible, then fill from fresh rotation choices,
        # and only fall back to previously deferred teammates if more coverage is needed.
        for user_id in preferred_user_ids or []:
            maybe_add(user_id)

        deferred_ids = [user_id for user_id in (avoid_user_ids or []) if user_id and user_id not in excluded_ids]
        while len(selected_users) < count:
            candidate = TaskAssignmentService.suggest_worker_assignee(
                due_date=due_date,
                estimated_minutes=estimated_minutes,
                exclude_user_ids=list(excluded_ids.union(deferred_ids)),
                scheduled_date=scheduled_date,
                scheduled_start_time=scheduled_start_time,
                scheduled_end_time=scheduled_end_time,
                exclude_task_id=exclude_task_id,
            )
            if not candidate:
                break
            selected_users.append(candidate)
            excluded_ids.add(candidate.pk)

        for user_id in deferred_ids:
            maybe_add(user_id)

        while len(selected_users) < count:
            candidate = TaskAssignmentService.suggest_worker_assignee(
                due_date=due_date,
                estimated_minutes=estimated_minutes,
                exclude_user_ids=list(excluded_ids),
                scheduled_date=scheduled_date,
                scheduled_start_time=scheduled_start_time,
                scheduled_end_time=scheduled_end_time,
                exclude_task_id=exclude_task_id,
            )
            if not candidate:
                break
            selected_users.append(candidate)
            excluded_ids.add(candidate.pk)

        return selected_users

    @staticmethod
    def _block_minutes(start_time, end_time):
        start_dt = datetime.combine(date.today(), start_time)
        end_dt = datetime.combine(date.today(), end_time)
        return int((end_dt - start_dt).total_seconds() // 60)

    @staticmethod
    def _block_tuples_from_availability(availability):
        blocks = [
            (block.start_time, block.end_time)
            for block in availability.blocks.order_by("position", "start_time", "end_time", "pk")
        ]
        if blocks:
            return blocks
        if availability.start_time and availability.end_time:
            return [(availability.start_time, availability.end_time)]
        return []

    @staticmethod
    def _schedule_override_for_date(profile, target_date):
        return (
            StudentScheduleOverride.objects.filter(profile=profile, override_date=target_date)
            .prefetch_related("blocks")
            .first()
        )

    @staticmethod
    def _schedule_blocks_for_date(profile, target_date):
        schedule_override = TaskAssignmentService._schedule_override_for_date(profile, target_date)
        if schedule_override is not None:
            return [
                (block.start_time, block.end_time)
                for block in schedule_override.blocks.order_by("position", "start_time", "end_time", "pk")
            ]

        availability = (
            StudentAvailability.objects.filter(profile=profile, weekday=target_date.weekday())
            .prefetch_related("blocks")
            .first()
        )
        if not availability:
            return []
        return TaskAssignmentService._block_tuples_from_availability(availability)

    @staticmethod
    def _scheduled_minutes_for_date(profile, target_date):
        schedule_override = TaskAssignmentService._schedule_override_for_date(profile, target_date)
        if schedule_override is not None:
            return sum(
                TaskAssignmentService._block_minutes(block.start_time, block.end_time)
                for block in schedule_override.blocks.order_by("position", "start_time", "end_time", "pk")
            )

        availability = StudentAvailability.objects.filter(profile=profile, weekday=target_date.weekday()).prefetch_related("blocks").first()
        if not availability:
            return 0
        blocks = TaskAssignmentService._block_tuples_from_availability(availability)
        if blocks:
            return sum(TaskAssignmentService._block_minutes(start_time, end_time) for start_time, end_time in blocks)
        return int(float(availability.hours_available or 0) * 60)

    @staticmethod
    def user_is_available_for_window(user, *, scheduled_date, scheduled_start_time, scheduled_end_time, exclude_task_id=None):
        if not user or user.role not in UserRole.worker_roles():
            return False
        try:
            profile = user.worker_profile
        except StudentWorkerProfile.DoesNotExist:
            return False
        if not profile.active_status:
            return False

        blocks = TaskAssignmentService._schedule_blocks_for_date(profile, scheduled_date)
        if not any(start_time <= scheduled_start_time and end_time >= scheduled_end_time for start_time, end_time in blocks):
            return False
        return not TaskAssignmentService._scheduled_conflict_exists(
            user,
            scheduled_date=scheduled_date,
            scheduled_start_time=scheduled_start_time,
            scheduled_end_time=scheduled_end_time,
            exclude_task_id=exclude_task_id,
        )

    @staticmethod
    def task_membership_filter(user):
        return TaskAssignmentService._worker_task_membership_filter(user)

    @staticmethod
    def active_task_queryset_for_user(user):
        return TaskAssignmentService._active_task_queryset_for_user(user)

    @staticmethod
    def scheduled_minutes_for_range(profile, start_date, end_date):
        total_minutes = 0
        cursor = start_date
        while cursor <= end_date:
            total_minutes += TaskAssignmentService._scheduled_minutes_for_date(profile, cursor)
            cursor += timedelta(days=1)
        return total_minutes

    @staticmethod
    def _active_task_queryset_for_user(user):
        return Task.objects.exclude(status=TaskStatus.DONE).filter(
            TaskAssignmentService._worker_task_membership_filter(user)
        ).distinct()

    @staticmethod
    def _matching_worker_candidates(
        *,
        due_date,
        estimated_minutes,
        exclude_user_ids=None,
        scheduled_date=None,
        scheduled_start_time=None,
        scheduled_end_time=None,
        exclude_task_id=None,
    ):
        pool = TaskAssignmentService._candidate_pool(
            due_date=due_date or scheduled_date,
            exclude_user_ids=exclude_user_ids,
            scheduled_date=scheduled_date,
            scheduled_start_time=scheduled_start_time,
            scheduled_end_time=scheduled_end_time,
            exclude_task_id=exclude_task_id,
        )
        if scheduled_date and scheduled_start_time and scheduled_end_time:
            return pool
        if estimated_minutes is None:
            return pool
        return [candidate for candidate in pool if candidate[1] >= estimated_minutes]

    @staticmethod
    def _scheduled_conflict_exists(user, *, scheduled_date, scheduled_start_time, scheduled_end_time, exclude_task_id=None):
        overlapping = Task.objects.exclude(status=TaskStatus.DONE).filter(
            scheduled_date=scheduled_date,
            scheduled_start_time__lt=scheduled_end_time,
            scheduled_end_time__gt=scheduled_start_time,
        )
        if exclude_task_id:
            overlapping = overlapping.exclude(pk=exclude_task_id)
        overlapping = overlapping.filter(
            TaskAssignmentService._worker_task_membership_filter(user)
        ).distinct()
        return overlapping.exists()

    @staticmethod
    def _minutes_remaining_in_workday(start_date):
        local_now = timezone.localtime()
        if local_now.date() != start_date:
            return None
        cutoff = local_now.replace(hour=17, minute=0, second=0, microsecond=0)
        if local_now >= cutoff:
            return 0
        return int((cutoff - local_now).total_seconds() // 60)

    @staticmethod
    def _remaining_capacity_minutes(profile, due_date):
        start_date = timezone.localdate()
        end_date = due_date or (start_date + timedelta(days=7))
        if end_date < start_date:
            end_date = start_date

        remaining_workday_minutes = TaskAssignmentService._minutes_remaining_in_workday(start_date)

        total_minutes = 0
        cursor = start_date
        while cursor <= end_date:
            available_minutes = TaskAssignmentService._scheduled_minutes_for_date(profile, cursor)
            if cursor == start_date and remaining_workday_minutes is not None:
                available_minutes = min(available_minutes, remaining_workday_minutes)
            total_minutes += max(available_minutes, 0)
            cursor += timedelta(days=1)
        reserved_minutes = (
            TaskAssignmentService._active_task_queryset_for_user(profile.user)
            .filter(Q(due_date__isnull=True) | Q(due_date__lte=end_date))
            .aggregate(total=Sum("estimated_minutes"))["total"]
            or 0
        )
        return max(total_minutes - reserved_minutes, 0)
