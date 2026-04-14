from .models import TaskAuditAction, TaskAuditEvent


TRACKED_TASK_FIELDS = [
    ("title", "Title"),
    ("description", "Task details"),
    ("priority", "Priority"),
    ("status", "Status"),
    ("due_date", "Due date"),
    ("scheduled_window", "Scheduled window"),
    ("estimated_minutes", "Time estimate"),
    ("assigned_to", "Assign to"),
    ("required_worker_tags", "Required worker tags"),
    ("additional_assignees", "Additional assignees"),
    ("notify_when_done", "Notify when done"),
    ("recurring", "Recurring schedule"),
]


def _display_value(value):
    if value in (None, "", []):
        return "-"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _recurring_summary(task):
    if not task.recurring_task:
        return "No"
    parts = [task.recurrence_pattern or "Repeats", f"every {task.recurrence_interval or 1}"]
    if task.recurrence_day_of_week is not None:
        parts.append(f"weekday {task.recurrence_day_of_week}")
    if task.recurrence_day_of_month:
        parts.append(f"day {task.recurrence_day_of_month}")
    return " | ".join(parts)


class TaskAuditService:
    @staticmethod
    def snapshot(task):
        return {
            "title": task.title,
            "description": task.description or task.raw_message or "",
            "priority": task.get_priority_display(),
            "status": task.get_status_display(),
            "due_date": task.due_date.isoformat() if task.due_date else "",
            "scheduled_window": task.scheduled_window_display,
            "estimated_minutes": f"{task.estimated_minutes} minutes" if task.estimated_minutes else "",
            "assigned_to": task.assigned_to.display_label if task.assigned_to else "Unassigned",
            "required_worker_tags": task.required_worker_tag_labels,
            "additional_assignees": task.additional_assignee_labels,
            "notify_when_done": task.respond_to_text or "",
            "recurring": _recurring_summary(task),
        }

    @staticmethod
    def record_event(task, *, actor, action, summary, details=None):
        return TaskAuditEvent.objects.create(
            task=task,
            task_title=getattr(task, "title", "Task"),
            actor=actor,
            action=action,
            summary=summary[:255],
            details=details or {},
        )

    @staticmethod
    def record_created(task, *, actor, source="manual"):
        summary = "Created task." if source == "manual" else f"Created task from {source.replace('_', ' ')}."
        return TaskAuditService.record_event(task, actor=actor, action=TaskAuditAction.CREATED, summary=summary)

    @staticmethod
    def record_deleted(task, *, actor):
        return TaskAuditService.record_event(task, actor=actor, action=TaskAuditAction.DELETED, summary="Deleted task.")

    @staticmethod
    def record_note_added(task, *, actor):
        return TaskAuditService.record_event(task, actor=actor, action=TaskAuditAction.NOTE_ADDED, summary="Added a note.")

    @staticmethod
    def record_attachment_added(task, *, actor, file_name):
        return TaskAuditService.record_event(
            task,
            actor=actor,
            action=TaskAuditAction.ATTACHMENT_ADDED,
            summary=f"Added attachment: {file_name}",
        )

    @staticmethod
    def record_checklist_updated(task, *, actor, summary="Updated checklist."):
        return TaskAuditService.record_event(task, actor=actor, action=TaskAuditAction.CHECKLIST_UPDATED, summary=summary)

    @staticmethod
    def record_recurring_reopened(task, *, summary):
        return TaskAuditService.record_event(task, actor=None, action=TaskAuditAction.RECURRING_RUN, summary=summary)

    @staticmethod
    def record_updated(task, *, actor, before_snapshot):
        after_snapshot = TaskAuditService.snapshot(task)
        changes = []
        for key, label in TRACKED_TASK_FIELDS:
            before_value = _display_value(before_snapshot.get(key))
            after_value = _display_value(after_snapshot.get(key))
            if before_value != after_value:
                changes.append({"field": label, "before": before_value, "after": after_value})

        if not changes:
            return None

        if len(changes) == 1 and changes[0]["field"] == "Status":
            summary = f"Changed status from {changes[0]['before']} to {changes[0]['after']}."
            action = TaskAuditAction.STATUS_CHANGED
        else:
            changed_fields = ", ".join(change["field"] for change in changes[:3])
            if len(changes) > 3:
                changed_fields += ", and more"
            summary = f"Updated task: {changed_fields}."
            action = TaskAuditAction.UPDATED

        return TaskAuditService.record_event(task, actor=actor, action=action, summary=summary, details={"changes": changes})
