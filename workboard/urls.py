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
    path("recurring/new/", views.recurring_template_create_view, name="recurring-create"),
    path("workers/", views.worker_list_view, name="worker-list"),
    path("workers/new/", views.worker_profile_create_view, name="worker-create"),
    path("workers/<int:pk>/availability/", views.worker_availability_view, name="worker-availability"),
    path("workers/<int:pk>/reset-password/", views.worker_password_reset_view, name="worker-password-reset"),
]
