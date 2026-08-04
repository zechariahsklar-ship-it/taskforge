# Re-export every test class so `manage.py test workboard.tests` and
# dotted references like `workboard.tests.SomeTestCase` keep working
# exactly as they did when this was a single tests.py file.
from .test_assignment import TaskAssignmentServiceTests, NextAvailableSupervisorTests
from .test_board import BoardFilterAndAlertTests, MyTasksViewOrderingTests, MyTasksOverdueSectionTests, CompletedTasksViewTests, BoardTaskMoveTests, AssignedBucketRemovalTests, TaskVisibilityAndAdditionalAssigneeTests, StudentSupervisorPermissionsTests
from .test_intake import TaskIntakeReviewViewTests, TaskIntakeViewTests
from .test_misc import PasswordToggleUiTests, AdminGuideViewTests
from .test_parsing import TaskParsingServiceTests, TaskParsingFallbackTests, AvailabilityParsingServiceTests, ParseClassScheduleViewTests
from .test_people import PeopleManagementTests, TeamHierarchyTests
from .test_permissions import SupervisorRoutePermissionTests, SecurityHardeningTests
from .test_recurring import RecurringTaskListViewTests, RecurringTaskGenerationRotationTests
from .test_regressions import ScopedQuerysetEmptyInputRegressionTests
from .test_reports import ReportsViewTests
from .test_scheduling import ScheduleAdjustmentRequestTests, SelfScheduleViewTests, BlackoutDateTests
from .test_task_detail import TaskDetailChecklistTests, TaskEstimateFeedbackTests, TaskAuditHistoryTests
from .test_task_forms import TaskCreateDueDateFallbackTests, TaskScheduledWindowTests, TaskCreateLabelTests, TaskCreateAdditionalAssigneeRotationTests

__all__ = [
    "AdminGuideViewTests",
    "AssignedBucketRemovalTests",
    "AvailabilityParsingServiceTests",
    "BlackoutDateTests",
    "BoardFilterAndAlertTests",
    "BoardTaskMoveTests",
    "CompletedTasksViewTests",
    "MyTasksOverdueSectionTests",
    "MyTasksViewOrderingTests",
    "NextAvailableSupervisorTests",
    "ParseClassScheduleViewTests",
    "PasswordToggleUiTests",
    "PeopleManagementTests",
    "RecurringTaskGenerationRotationTests",
    "RecurringTaskListViewTests",
    "ReportsViewTests",
    "ScheduleAdjustmentRequestTests",
    "ScopedQuerysetEmptyInputRegressionTests",
    "SecurityHardeningTests",
    "SelfScheduleViewTests",
    "StudentSupervisorPermissionsTests",
    "SupervisorRoutePermissionTests",
    "TaskAssignmentServiceTests",
    "TaskAuditHistoryTests",
    "TaskCreateAdditionalAssigneeRotationTests",
    "TaskCreateDueDateFallbackTests",
    "TaskCreateLabelTests",
    "TaskDetailChecklistTests",
    "TaskEstimateFeedbackTests",
    "TaskIntakeReviewViewTests",
    "TaskIntakeViewTests",
    "TaskParsingFallbackTests",
    "TaskParsingServiceTests",
    "TaskScheduledWindowTests",
    "TaskVisibilityAndAdditionalAssigneeTests",
    "TeamHierarchyTests",
]
