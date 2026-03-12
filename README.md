# TaskForge

TaskForge is a simple internal Django app for supervising student workers. Supervisors can paste a freeform request, review AI-extracted fields, create and assign tasks, manage recurring work, and track progress on a shared board. Student workers can review assigned tasks, update status, and add notes.

## MVP scope

- Django monolith with server-rendered templates
- PostgreSQL as the primary database
- Custom user model with `supervisor` and `student_worker` roles
- Student worker profiles and workload summary
- Task intake flow with a placeholder AI parsing service
- Shared task board and worker task view
- Recurring task templates with a generation command placeholder
- Django admin and sample seed data

## Local setup

1. Create PostgreSQL database and user:
   - Database: `taskforge`
   - User: `taskforge`
   - Password: set to match `.env`
2. Copy `.env.example` to `.env` and update values.
3. Create and activate a virtual environment.
4. Install dependencies:

```bash
pip install -r requirements.txt
```

5. Run migrations:

```bash
python manage.py migrate
```

6. Create a superuser:

```bash
python manage.py createsuperuser
```

7. Seed sample data if desired:

```bash
python manage.py seed_sample_data
```

8. Start the development server:

```bash
python manage.py runserver
```

## PostgreSQL quick start

Example SQL:

```sql
CREATE DATABASE taskforge;
CREATE USER taskforge WITH PASSWORD 'taskforge';
GRANT ALL PRIVILEGES ON DATABASE taskforge TO taskforge;
```

## Development shortcuts

If you need to run local checks without PostgreSQL temporarily, set `USE_SQLITE=True` in `.env`. PostgreSQL remains the main configured database for the app.

## Demo accounts after seeding

- Supervisor: `supervisor1` / `password123`
- Student worker: `alex` / `password123`
- Student worker: `jordan` / `password123`

## Management commands

- `python manage.py seed_sample_data`
- `python manage.py generate_recurring_tasks`

The recurring task command is intended to be wired into Windows Task Scheduler, cron, or another scheduler later.
