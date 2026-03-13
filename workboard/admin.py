from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    RecurringTaskTemplate,
    StudentAvailability,
    StudentAvailabilityOverride,
    StudentWorkerProfile,
    Task,
    TaskAttachment,
    TaskChecklistItem,
    TaskIntakeDraft,
    TaskIntakeDraftAttachment,
    TaskNote,
    User,
)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (("Role", {"fields": ("role", "must_change_password")}),)
    list_display = ("username", "email", "role", "must_change_password", "is_staff")


@admin.register(StudentWorkerProfile)
class StudentWorkerProfileAdmin(admin.ModelAdmin):
    list_display = ("display_name", "email", "active_status", "max_hours_per_day")
    search_fields = ("display_name", "email", "user__username")


@admin.register(StudentAvailability)
class StudentAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("profile", "weekday", "hours_available")
    list_filter = ("weekday",)


@admin.register(StudentAvailabilityOverride)
class StudentAvailabilityOverrideAdmin(admin.ModelAdmin):
    list_display = ("profile", "override_date", "hours_available", "created_by")
    list_filter = ("override_date",)


class TaskNoteInline(admin.TabularInline):
    model = TaskNote
    extra = 0


class TaskChecklistItemInline(admin.TabularInline):
    model = TaskChecklistItem
    extra = 0


class TaskAttachmentInline(admin.TabularInline):
    model = TaskAttachment
    extra = 0


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "priority", "status", "assigned_to", "due_date", "recurring_task")
    list_filter = ("priority", "status", "recurring_task")
    search_fields = ("title", "description", "raw_message")
    filter_horizontal = ("additional_assignees",)
    inlines = [TaskChecklistItemInline, TaskAttachmentInline, TaskNoteInline]


@admin.register(RecurringTaskTemplate)
class RecurringTaskTemplateAdmin(admin.ModelAdmin):
    list_display = ("title", "recurrence_pattern", "next_run_date", "assign_to", "active")
    list_filter = ("recurrence_pattern", "active")


class TaskIntakeDraftAttachmentInline(admin.TabularInline):
    model = TaskIntakeDraftAttachment
    extra = 0


@admin.register(TaskIntakeDraft)
class TaskIntakeDraftAdmin(admin.ModelAdmin):
    list_display = ("id", "created_by", "created_at")
    search_fields = ("raw_message",)
    inlines = [TaskIntakeDraftAttachmentInline]
