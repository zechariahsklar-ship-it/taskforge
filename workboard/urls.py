from django.urls import path

from . import views


urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("board/", views.board_view, name="board"),
    path("board/tasks/<int:pk>/move/", views.board_task_move_view, name="board-task-move"),
    path("account/password/", views.password_change_view, name="password-change"),
    path("tasks/mine/", views.my_tasks_view, name="my-tasks"),
    path("tasks/intake/", views.task_intake_view, name="task-intake"),
    path("tasks/intake/<int:pk>/review/", views.task_intake_review_view, name="task-intake-review"),
    path("tasks/new/", views.task_create_view, name="task-create"),
    path("tasks/<int:pk>/", views.task_detail_view, name="task-detail"),
    path("tasks/<int:pk>/edit/", views.task_edit_view, name="task-edit"),
    path("tasks/<int:pk>/delete/", views.task_delete_view, name="task-delete"),
    path("recurring/", views.recurring_template_list_view, name="recurring-list"),
    path("recurring/new/", views.recurring_template_list_view, name="recurring-create"),
    path("recurring/<int:pk>/", views.recurring_template_detail_view, name="recurring-detail"),
    path("recurring/<int:pk>/edit/", views.recurring_template_edit_view, name="recurring-edit"),
    path("recurring/<int:pk>/move/", views.recurring_template_move_view, name="recurring-move"),
    path("workers/", views.worker_list_view, name="worker-list"),
    path("workers/new/", views.worker_profile_create_view, name="worker-create"),
    path("workers/student-supervisors/new/", views.student_supervisor_create_view, name="student-supervisor-create"),
    path("workers/supervisors/new/", views.supervisor_create_view, name="supervisor-create"),
    path("workers/<int:pk>/edit/", views.worker_edit_view, name="worker-edit"),
    path("workers/<int:pk>/schedule/", views.worker_schedule_view, name="worker-schedule"),
    path("workers/<int:pk>/availability/", views.worker_schedule_view, name="worker-availability"),
    path("workers/<int:pk>/delete/", views.worker_profile_delete_view, name="worker-delete"),
    path("workers/<int:pk>/reset-password/", views.worker_password_reset_view, name="worker-password-reset"),
    path("workers/supervisors/<int:pk>/edit/", views.supervisor_edit_view, name="supervisor-edit"),
    path("workers/supervisors/<int:pk>/delete/", views.supervisor_delete_view, name="supervisor-delete"),
]
