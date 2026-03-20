# TaskForge Supervisor Guide

## Daily workflow

1. Start on the board and check overdue, waiting, and today's scheduled work.
2. Use Intake for pasted requests that need parsing.
3. Use New Task for tasks you already understand.
4. Use Reports for weekly workload and completion summaries.

## Roles

- Supervisor: full admin access to tasks, recurring work, schedules, reports, and people management.
- Student Supervisor: can view the full board and edit tasks, but cannot create tasks or manage people.
- Student Worker: can view assigned work and update task progress.

## Recurring tasks

- Open the Recurring page to review repeating task templates.
- Each recurring detail page shows a next-run preview and upcoming run dates.
- Use Run now to place the next recurring task on the board immediately.
- TaskForge will not run a recurring task again while its current run is still open.

## Schedules and temporary changes

- Use Edit schedule from the Workers page to manage weekly availability.
- Drag across the weekly calendar to create 30-minute blocks.
- Use the copy buttons to copy one day across weekdays or the full week.
- Use Temporary schedule change for one-date exceptions.
- Load normal schedule copies that weekday's normal schedule into the override editor as a starting point.

## People management

- Edit worker and Edit supervisor pages handle name, email, notes, and role settings.
- Reset password forces the person to choose a new password at next login.
- Removing a person reassigns their open work to the acting supervisor.

## Useful commands

```bash
python manage.py check
python manage.py test workboard.tests
python manage.py generate_recurring_tasks
```

## Troubleshooting

- If a recurring task is missing from Recurring, open the page once to let older recurring-marked tasks backfill into templates.
- If a worker cannot be assigned to a scheduled task, verify both the weekly schedule and any temporary schedule on that date.
- If a page still shows an older layout after deploy, do a hard refresh.
