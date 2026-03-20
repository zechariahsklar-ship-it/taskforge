# TaskForge

TaskForge is a standalone internal Django web app for supervising student workers. It is intended as a temporary, self-contained solution for intake, assignment, and tracking of student worker tasks. Supervisors can paste a freeform request, review AI-extracted fields, create and assign tasks, manage recurring work, and track progress on a shared board. Student workers can review assigned tasks, update status, and add notes.

## MVP scope

- Django monolith with server-rendered templates
- PostgreSQL as the primary database
- Custom user model with `supervisor` and `student_worker` roles
- Student worker profiles and workload summary
- Task intake flow with a placeholder AI parsing service
- Shared task board and worker task view
- Recurring task templates with a simple built-in generation command
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

## Supervisor guide

A short operating guide for supervisors is available in [ADMIN_GUIDE.md](ADMIN_GUIDE.md).
When the app is running, supervisors can also open the in-app `Guide` page from the top navigation.

## Testing

Useful commands during development:

```bash
python manage.py check
python manage.py test workboard.tests
python manage.py test workboard.tests.RecurringTaskListViewTests workboard.tests.ReportsViewTests
```

## OpenAI key setup

Store the real OpenAI key only in your local `.env` file or in deployment environment variables. Do not commit it to GitHub.

Example:

```env
OPENAI_API_KEY=your_real_key_here
OPENAI_TASK_PARSER_MODEL=gpt-5-mini
USE_MOCK_TASK_PARSER=True
```

Keep `USE_MOCK_TASK_PARSER=True` until you are ready to switch the task parser from the current mock implementation to the real OpenAI API integration.

## Demo accounts after seeding

- Supervisor: `supervisor1` / `password123`
- Student worker: `alex` / `password123`
- Student worker: `jordan` / `password123`

## Management commands

- `python manage.py seed_sample_data`
- `python manage.py generate_recurring_tasks`
