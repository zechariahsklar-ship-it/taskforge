from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    RecurringTaskTemplate,
    StudentAvailability,
    StudentWorkerProfile,
    Task,
    TaskAttachment,
    TaskAuditEvent,
    TaskChecklistItem,
    TaskIntakeDraft,
    TaskIntakeDraftAttachment,
    TaskNote,
    Team,
    User,
)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "description", "created_at")
    search_fields = ("name", "description")


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (("Role", {"fields": ("role", "team", "must_change_password", "assignable_to_tasks")}),)
    list_display = ("username", "email", "role", "team", "must_change_password", "assignable_to_tasks", "is_staff")
    list_filter = ("role", "team", "must_change_password", "assignable_to_tasks", "is_staff")


@admin.register(StudentWorkerProfile)
class StudentWorkerProfileAdmin(admin.ModelAdmin):
    list_display = ("display_name", "email", "active_status", "user")
    search_fields = ("display_name", "email", "user__username")


@admin.register(StudentAvailability)
class StudentAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("profile", "weekday", "hours_available")
    list_filter = ("weekday",)


class TaskNoteInline(admin.TabularInline):
    model = TaskNote
    extra = 0


class TaskChecklistItemInline(admin.TabularInline):
    model = TaskChecklistItem
    extra = 0
    fields = ("title", "position")
    ordering = ("position", "id")


class TaskAttachmentInline(admin.TabularInline):
    model = TaskAttachment
    extra = 0


class TaskAuditEventInline(admin.TabularInline):
    model = TaskAuditEvent
    extra = 0
    can_delete = False
    readonly_fields = ("created_at", "actor", "action", "summary", "details")
    fields = ("created_at", "actor", "action", "summary", "details")
    ordering = ("-created_at", "-id")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "team", "priority", "status", "assigned_to", "due_date", "recurring_task")
    list_filter = ("team", "priority", "status", "recurring_task")
    search_fields = ("title", "description", "raw_message")
    filter_horizontal = ("additional_assignees",)
    inlines = [TaskChecklistItemInline, TaskAttachmentInline, TaskAuditEventInline, TaskNoteInline]


@admin.register(RecurringTaskTemplate)
class RecurringTaskTemplateAdmin(admin.ModelAdmin):
    list_display = ("title", "team", "recurrence_pattern", "next_run_date", "assign_to", "active")
    list_filter = ("team", "recurrence_pattern", "active")


class TaskIntakeDraftAttachmentInline(admin.TabularInline):
    model = TaskIntakeDraftAttachment
    extra = 0


@admin.register(TaskIntakeDraft)
class TaskIntakeDraftAdmin(admin.ModelAdmin):
    list_display = ("id", "created_by", "created_at")
    search_fields = ("raw_message",)
    inlines = [TaskIntakeDraftAttachmentInline]
