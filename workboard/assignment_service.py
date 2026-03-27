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
    def _single_window_blocks(*, scheduled_date=None, scheduled_start_time=None, scheduled_end_time=None):
        if scheduled_date and scheduled_start_time and scheduled_end_time:
            return {scheduled_date: [(scheduled_start_time, scheduled_end_time)]}
        return {}

    @staticmethod
    def _candidate_metrics(profile, *, due_date, task_window_blocks=None, exclude_task_id=None):
        active_tasks = TaskAssignmentService._active_task_queryset_for_user(profile.user)
        if task_window_blocks:
            capacity = TaskAssignmentService._remaining_capacity_minutes_in_task_windows(
                profile,
                task_window_blocks,
                exclude_task_id=exclude_task_id,
            )
        else:
            capacity = TaskAssignmentService._remaining_capacity_minutes(profile, due_date)
        return (
            capacity,
            active_tasks.count(),
            active_tasks.aggregate(last_assigned=Max("created_at"))["last_assigned"],
        )

    @staticmethod
    def _candidate_pool(
        *,
        due_date,
        exclude_user_ids=None,
        task_window_blocks=None,
        exclude_task_id=None,
    ):
        workers = TaskAssignmentService._worker_profiles(exclude_user_ids=exclude_user_ids)
        candidates = []
        for profile in workers:
            capacity, open_tasks, last_assigned_at = TaskAssignmentService._candidate_metrics(
                profile,
                due_date=due_date,
                task_window_blocks=task_window_blocks,
                exclude_task_id=exclude_task_id,
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
        task_window_blocks=None,
        exclude_task_id=None,
    ):
        task_window_blocks = task_window_blocks or TaskAssignmentService._single_window_blocks(
            scheduled_date=scheduled_date,
            scheduled_start_time=scheduled_start_time,
            scheduled_end_time=scheduled_end_time,
        )
        viable = TaskAssignmentService._matching_worker_candidates(
            due_date=due_date,
            estimated_minutes=estimated_minutes,
            exclude_user_ids=exclude_user_ids,
            task_window_blocks=task_window_blocks,
            exclude_task_id=exclude_task_id,
        )

        if viable:
            profile, capacity, _, _ = viable[0]
            if task_window_blocks:
                summary = (
                    f"Suggested worker: {profile.display_name} based on scheduled availability inside the task windows. "
                    f"Remaining matching time: {int(capacity)} minutes."
                )
                rationale = [
                    f"Recommended assignee: {profile.display_name}",
                    f"Estimated matching availability inside the task windows: {int(capacity)} minutes",
                    "Selection favors worker-level teammates who can complete this work inside the allowed schedule windows.",
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
            message = (
                "No worker has enough scheduled availability inside those task windows, so this task should stay with the supervising user."
                if task_window_blocks
                else "No worker has enough available capacity before the due date, so this task should stay with the supervising user."
            )
            return fallback_supervisor, message, [
                f"Recommended assignee: {fallback_supervisor.display_label}",
                "No worker currently has enough available hours for this assignment rule.",
                "Fallback rule assigned the task to the supervising user instead of rotating among supervisors.",
            ]
        return None, "No eligible worker has enough available capacity for this task, and no supervisor fallback is available.", [
            "No worker currently has enough available hours for this assignment rule.",
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
        task_window_blocks=None,
        exclude_task_id=None,
    ):
        task_window_blocks = task_window_blocks or TaskAssignmentService._single_window_blocks(
            scheduled_date=scheduled_date,
            scheduled_start_time=scheduled_start_time,
            scheduled_end_time=scheduled_end_time,
        )
        viable = TaskAssignmentService._matching_worker_candidates(
            due_date=due_date,
            estimated_minutes=estimated_minutes,
            exclude_user_ids=exclude_user_ids,
            task_window_blocks=task_window_blocks,
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
        task_window_blocks=None,
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

        task_window_blocks = task_window_blocks or TaskAssignmentService._single_window_blocks(
            scheduled_date=scheduled_date,
            scheduled_start_time=scheduled_start_time,
            scheduled_end_time=scheduled_end_time,
        )
        if task_window_blocks:
            remaining_minutes = TaskAssignmentService._remaining_capacity_minutes_in_task_windows(
                profile,
                task_window_blocks,
                exclude_task_id=exclude_task_id,
            )
            if estimated_minutes is None:
                return remaining_minutes > 0
            return remaining_minutes >= estimated_minutes

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
        task_window_blocks=None,
        exclude_task_id=None,
    ):
        if count <= 0:
            return []

        task_window_blocks = task_window_blocks or TaskAssignmentService._single_window_blocks(
            scheduled_date=scheduled_date,
            scheduled_start_time=scheduled_start_time,
            scheduled_end_time=scheduled_end_time,
        )
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
                task_window_blocks=task_window_blocks,
                exclude_task_id=exclude_task_id,
            ):
                return
            selected_users.append(candidate)
            excluded_ids.add(candidate.pk)

        for user_id in preferred_user_ids or []:
            maybe_add(user_id)

        deferred_ids = [user_id for user_id in (avoid_user_ids or []) if user_id and user_id not in excluded_ids]
        while len(selected_users) < count:
            candidate = TaskAssignmentService.suggest_worker_assignee(
                due_date=due_date,
                estimated_minutes=estimated_minutes,
                exclude_user_ids=list(excluded_ids.union(deferred_ids)),
                task_window_blocks=task_window_blocks,
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
                task_window_blocks=task_window_blocks,
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
    def _time_overlap_minutes(start_a, end_a, start_b, end_b):
        overlap_start = max(start_a, start_b)
        overlap_end = min(end_a, end_b)
        if overlap_end <= overlap_start:
            return 0
        return TaskAssignmentService._block_minutes(overlap_start, overlap_end)

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
    def _normalize_task_window_blocks(task_window_blocks):
        normalized = {}
        if not task_window_blocks:
            return normalized
        for work_date, blocks in task_window_blocks.items():
            if not blocks:
                continue
            sorted_blocks = sorted(blocks, key=lambda block: (block[0], block[1]))
            merged_blocks = []
            for start_value, end_value in sorted_blocks:
                if not merged_blocks:
                    merged_blocks.append([start_value, end_value])
                    continue
                last_start, last_end = merged_blocks[-1]
                if start_value <= last_end:
                    merged_blocks[-1][1] = max(last_end, end_value)
                else:
                    merged_blocks.append([start_value, end_value])
            normalized[work_date] = [(start_value, end_value) for start_value, end_value in merged_blocks]
        return normalized

    @staticmethod
    def _task_window_blocks_for_task(task):
        scheduled_blocks = list(task.scheduled_blocks.order_by("work_date", "position", "start_time", "end_time", "pk"))
        if scheduled_blocks:
            grouped = {}
            for block in scheduled_blocks:
                grouped.setdefault(block.work_date, []).append((block.start_time, block.end_time))
            return TaskAssignmentService._normalize_task_window_blocks(grouped)
        if task.scheduled_date and task.scheduled_start_time and task.scheduled_end_time:
            return {task.scheduled_date: [(task.scheduled_start_time, task.scheduled_end_time)]}
        return {}

    @staticmethod
    def _minutes_remaining_in_interval(target_date, start_time, end_time):
        if target_date != timezone.localdate():
            return TaskAssignmentService._block_minutes(start_time, end_time)
        local_now = timezone.localtime()
        now_time = local_now.time().replace(second=0, microsecond=0)
        if now_time >= end_time:
            return 0
        effective_start = max(start_time, now_time)
        if effective_start >= end_time:
            return 0
        return TaskAssignmentService._block_minutes(effective_start, end_time)

    @staticmethod
    def _task_window_overlap_minutes(task_window_blocks, other_window_blocks):
        total_minutes = 0
        normalized_target = TaskAssignmentService._normalize_task_window_blocks(task_window_blocks)
        normalized_other = TaskAssignmentService._normalize_task_window_blocks(other_window_blocks)
        for work_date, target_blocks in normalized_target.items():
            other_blocks = normalized_other.get(work_date, [])
            for target_start, target_end in target_blocks:
                for other_start, other_end in other_blocks:
                    total_minutes += TaskAssignmentService._time_overlap_minutes(
                        target_start,
                        target_end,
                        other_start,
                        other_end,
                    )
        return total_minutes

    @staticmethod
    def _minutes_available_in_task_windows(profile, task_window_blocks):
        total_minutes = 0
        normalized_blocks = TaskAssignmentService._normalize_task_window_blocks(task_window_blocks)
        for work_date, task_blocks in normalized_blocks.items():
            worker_blocks = TaskAssignmentService._schedule_blocks_for_date(profile, work_date)
            for task_start, task_end in task_blocks:
                for worker_start, worker_end in worker_blocks:
                    overlap_start = max(task_start, worker_start)
                    overlap_end = min(task_end, worker_end)
                    if overlap_end <= overlap_start:
                        continue
                    total_minutes += TaskAssignmentService._minutes_remaining_in_interval(
                        work_date,
                        overlap_start,
                        overlap_end,
                    )
        return total_minutes

    @staticmethod
    def _reserved_window_minutes_for_user(user, task_window_blocks, *, exclude_task_id=None):
        normalized_blocks = TaskAssignmentService._normalize_task_window_blocks(task_window_blocks)
        if not normalized_blocks:
            return 0

        active_tasks = TaskAssignmentService._active_task_queryset_for_user(user)
        if exclude_task_id:
            active_tasks = active_tasks.exclude(pk=exclude_task_id)
        relevant_dates = list(normalized_blocks.keys())
        active_tasks = active_tasks.filter(
            Q(scheduled_date__in=relevant_dates) | Q(scheduled_blocks__work_date__in=relevant_dates)
        ).prefetch_related("scheduled_blocks").distinct()

        reserved_minutes = 0
        for other_task in active_tasks:
            other_blocks = TaskAssignmentService._task_window_blocks_for_task(other_task)
            overlap_minutes = TaskAssignmentService._task_window_overlap_minutes(normalized_blocks, other_blocks)
            if overlap_minutes <= 0:
                continue
            reserved_minutes += min(other_task.estimated_minutes or overlap_minutes, overlap_minutes)
        return reserved_minutes

    @staticmethod
    def _remaining_capacity_minutes_in_task_windows(profile, task_window_blocks, *, exclude_task_id=None):
        available_minutes = TaskAssignmentService._minutes_available_in_task_windows(profile, task_window_blocks)
        reserved_minutes = TaskAssignmentService._reserved_window_minutes_for_user(
            profile.user,
            task_window_blocks,
            exclude_task_id=exclude_task_id,
        )
        return max(available_minutes - reserved_minutes, 0)

    @staticmethod
    def user_is_available_for_window(user, *, scheduled_date, scheduled_start_time, scheduled_end_time, exclude_task_id=None):
        required_minutes = TaskAssignmentService._block_minutes(scheduled_start_time, scheduled_end_time)
        return TaskAssignmentService.worker_can_take_task(
            user,
            due_date=scheduled_date,
            estimated_minutes=required_minutes,
            task_window_blocks={scheduled_date: [(scheduled_start_time, scheduled_end_time)]},
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
        task_window_blocks=None,
        exclude_task_id=None,
    ):
        task_window_blocks = task_window_blocks or TaskAssignmentService._single_window_blocks(
            scheduled_date=scheduled_date,
            scheduled_start_time=scheduled_start_time,
            scheduled_end_time=scheduled_end_time,
        )
        pool = TaskAssignmentService._candidate_pool(
            due_date=due_date or scheduled_date,
            exclude_user_ids=exclude_user_ids,
            task_window_blocks=task_window_blocks,
            exclude_task_id=exclude_task_id,
        )
        if task_window_blocks:
            if estimated_minutes is None:
                return [candidate for candidate in pool if candidate[1] > 0]
            return [candidate for candidate in pool if candidate[1] >= estimated_minutes]
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
