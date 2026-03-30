from .services import TaskParsingService

from .help_views import admin_guide_view
from .people_views import (
    schedule_adjustment_request_list_view,
    schedule_adjustment_request_view,
    student_supervisor_create_view,
    supervisor_create_view,
    supervisor_delete_view,
    supervisor_edit_view,
    team_create_view,
    team_delete_view,
    team_edit_view,
    worker_edit_view,
    worker_list_view,
    worker_password_reset_view,
    worker_profile_create_view,
    worker_profile_delete_view,
    worker_schedule_view,
)
from .recurring_views import (
    recurring_template_delete_view,
    recurring_template_detail_view,
    recurring_template_edit_view,
    recurring_template_list_view,
    recurring_template_move_view,
    recurring_template_run_now_view,
)
from .report_views import reports_view
from .task_views import (
    board_task_move_view,
    board_view,
    dashboard,
    my_tasks_view,
    password_change_view,
    task_create_view,
    task_delete_view,
    task_detail_view,
    task_edit_view,
    task_intake_review_view,
    task_intake_view,
)
