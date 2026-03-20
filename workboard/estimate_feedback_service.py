from .models import TaskEstimateFeedback


class TaskEstimateFeedbackService:
    @staticmethod
    def record_feedback(*, task, original_estimated_minutes, corrected_estimated_minutes, corrected_by=None, source=""):
        if corrected_estimated_minutes in (None, ""):
            return None
        try:
            corrected_value = int(corrected_estimated_minutes)
        except (TypeError, ValueError):
            return None
        original_value = None
        if original_estimated_minutes not in (None, ""):
            try:
                original_value = int(original_estimated_minutes)
            except (TypeError, ValueError):
                original_value = None
        if original_value == corrected_value:
            return None
        return TaskEstimateFeedback.objects.create(
            task=task,
            raw_message=getattr(task, "raw_message", "") or "",
            task_title=getattr(task, "title", "") or "",
            original_estimated_minutes=original_value,
            corrected_estimated_minutes=corrected_value,
            corrected_by=corrected_by,
            source=source[:64],
        )
