# DigitalOcean Deployment

## App Platform
- Source: GitHub repo `zechariahsklar-ship-it/taskforge`
- Type: Web Service
- Environment: Python
- Build command: `python manage.py collectstatic --noinput`
- Run command: `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT`

## Recurring Task Runner
- The recurring rollover logic is handled by `python manage.py generate_recurring_tasks`.
- `.do/app.yaml` now defines a DigitalOcean App Platform scheduled job named `recurring-runner`.
- The schedule runs every 15 minutes with cron `*/15 * * * *` in `America/New_York`.
- If your DigitalOcean app was created in the control panel, update the app spec or add the scheduled job in the App Platform UI so the live app actually uses this config.

## Database
Use a managed PostgreSQL database.

Recommended environment variables:
- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG=False`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `DJANGO_TIME_ZONE=America/New_York`
- `USE_SQLITE=False`
- `DATABASE_URL` or the individual `POSTGRES_*` values
- `POSTGRES_SSL_REQUIRE=True`
- `OPENAI_API_KEY` if you want live intake parsing
- `USE_MOCK_TASK_PARSER=False` for live intake parsing

## First-time commands
Run these once after the app is live:
- `python manage.py migrate`
- `python manage.py createsuperuser`

## Media uploads
Static files are production-ready in this repo.
Task/file uploads should use DigitalOcean Spaces in production.
Set these if you want persistent uploads:
- `USE_SPACES=True`
- `DO_SPACES_KEY`
- `DO_SPACES_SECRET`
- `DO_SPACES_BUCKET`
- `DO_SPACES_REGION`
- `DO_SPACES_ENDPOINT`
- `DO_SPACES_CDN_DOMAIN`
