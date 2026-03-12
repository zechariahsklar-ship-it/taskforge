from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import RecurringTaskTemplate, StudentWorkerProfile, Task, TaskNote, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (("Role", {"fields": ("role",)}),)
    list_display = ("username", "email", "role", "is_staff")


@admin.register(StudentWorkerProfile)
class StudentWorkerProfileAdmin(admin.ModelAdmin):
    list_display = ("display_name", "email", "active_status", "max_hours_per_day")
    search_fields = ("display_name", "email", "user__username")


class TaskNoteInline(admin.TabularInline):
    model = TaskNote
    extra = 0


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "priority", "status", "assigned_to", "due_date", "recurring_task")
    list_filter = ("priority", "status", "recurring_task")
    search_fields = ("title", "description", "raw_message")
    inlines = [TaskNoteInline]


@admin.register(RecurringTaskTemplate)
class RecurringTaskTemplateAdmin(admin.ModelAdmin):
    list_display = ("title", "recurrence_pattern", "next_run_date", "assign_to", "active")
    list_filter = ("recurrence_pattern", "active")
