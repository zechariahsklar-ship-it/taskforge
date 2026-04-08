from django.shortcuts import render

from .task_views import supervisor_required


@supervisor_required
def admin_guide_view(request):
    return render(request, "workboard/admin_guide.html")
