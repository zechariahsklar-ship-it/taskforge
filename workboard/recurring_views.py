from django.contrib import messages
from django.db.models import Count, F
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import RecurringTaskTemplateForm
from .models import RecurringTaskTemplate
from .recurring_service import RecurringTaskService
from .task_views import (
    _backfill_orphan_recurring_tasks,
    _ordered_recurring_templates,
    _resequence_recurring_templates,
    _scope_queryset_to_user_team,
    supervisor_required,
)


def _scoped_recurring_templates(user, queryset=None):
    queryset = queryset or RecurringTaskTemplate.objects.all()
    return _scope_queryset_to_user_team(queryset, user)


@supervisor_required
def recurring_template_list_view(request):
    _backfill_orphan_recurring_tasks(user=request.user)
    templates = (
        _scoped_recurring_templates(
            request.user,
            RecurringTaskTemplate.objects.select_related("team", "assign_to", "requested_by")
            .prefetch_related("additional_assignees", "required_worker_tags")
            .annotate(generated_task_count=Count("generated_tasks")),
        )
        .order_by(F("display_order").asc(nulls_last=True), "next_run_date", "title", "pk")
    )
    return render(request, "workboard/recurring_template_list.html", {"templates": templates})


@supervisor_required
def recurring_template_detail_view(request, pk):
    template = get_object_or_404(
        _scoped_recurring_templates(
            request.user,
            RecurringTaskTemplate.objects.select_related("team", "assign_to", "requested_by").prefetch_related(
                "additional_assignees", "generated_tasks", "required_worker_tags"
            ),
        ),
        pk=pk,
    )
    recent_tasks = list(template.generated_tasks.order_by("-created_at")[:10])
    next_run_preview = RecurringTaskService.preview_next_run(template)
    upcoming_run_dates = RecurringTaskService.upcoming_run_dates(template, count=5)
    fixed_additional_users = list(template.additional_assignees.exclude(pk=getattr(next_run_preview.assignee, "pk", None)))
    return render(
        request,
        "workboard/recurring_template_detail.html",
        {
            "template": template,
            "recent_tasks": recent_tasks,
            "next_run_preview": next_run_preview,
            "upcoming_run_dates": upcoming_run_dates,
            "fixed_additional_users": fixed_additional_users,
        },
    )


@supervisor_required
def recurring_template_run_now_view(request, pk):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")

    template = get_object_or_404(_scoped_recurring_templates(request.user), pk=pk)
    run_date = template.next_run_date
    if RecurringTaskService.preview_next_run(template, run_date=run_date).current_task_open:
        messages.warning(request, "This recurring task already has an open run on the board. Finish it before running it again.")
        return redirect("recurring-detail", pk=template.pk)

    task, outcome = RecurringTaskService.run_template(template, run_date=run_date)
    if outcome == "skipped":
        messages.warning(request, "This recurring task already has an open run on the board. Finish it before running it again.")
        return redirect("recurring-detail", pk=template.pk)

    messages.success(request, f"Recurring task queued for {run_date.isoformat()}.")
    return redirect("task-detail", pk=task.pk)


@supervisor_required
def recurring_template_move_view(request, pk):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")

    template = get_object_or_404(_scoped_recurring_templates(request.user), pk=pk)
    before_template_id = request.POST.get("before_template_id", "").strip()
    ordered_templates = _ordered_recurring_templates(exclude_pk=template.pk, team=template.team)
    if before_template_id:
        try:
            before_template_id_int = int(before_template_id)
        except ValueError:
            return HttpResponseBadRequest("Invalid target position.")
        before_template = next((item for item in ordered_templates if item.pk == before_template_id_int), None)
        if before_template is None:
            return HttpResponseBadRequest("Target recurring task not found.")
        insert_index = ordered_templates.index(before_template)
    else:
        insert_index = len(ordered_templates)

    ordered_templates.insert(insert_index, template)
    _resequence_recurring_templates(ordered_templates)
    return JsonResponse({"ok": True})


@supervisor_required
def recurring_template_edit_view(request, pk):
    template = get_object_or_404(_scoped_recurring_templates(request.user), pk=pk)
    previous_team = template.team
    if request.method == "POST":
        form = RecurringTaskTemplateForm(request.POST, instance=template, actor=request.user)
        if form.is_valid():
            updated_template = form.save(commit=False)
            team_changed = previous_team != updated_template.team
            if team_changed:
                updated_template.display_order = None
            updated_template.save()
            form.save_m2m()
            if team_changed:
                if previous_team is not None:
                    _resequence_recurring_templates(_ordered_recurring_templates(team=previous_team))
                _resequence_recurring_templates(_ordered_recurring_templates(team=updated_template.team))
            messages.success(request, "Recurring task updated.")
            return redirect("recurring-detail", pk=updated_template.pk)
    else:
        form = RecurringTaskTemplateForm(instance=template, actor=request.user)
    return render(
        request,
        "workboard/recurring_template_form.html",
        {"form": form, "page_title": "Edit recurring task", "submit_label": "Save changes", "template_obj": template},
    )


@supervisor_required
def recurring_template_delete_view(request, pk):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")

    template = get_object_or_404(_scoped_recurring_templates(request.user), pk=pk)
    previous_team = template.team
    template.generated_tasks.update(
        recurring_task=False,
        recurring_template=None,
        recurrence_pattern="",
        recurrence_interval=None,
        recurrence_day_of_week=None,
        recurrence_day_of_month=None,
        updated_at=timezone.now(),
    )
    template.delete()
    if previous_team is not None:
        _resequence_recurring_templates(_ordered_recurring_templates(team=previous_team))
    messages.success(request, "Recurring task removed.")
    return redirect("recurring-list")
