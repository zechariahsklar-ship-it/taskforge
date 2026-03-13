# DigitalOcean Deployment

## App Platform
- Source: GitHub repo `zechariahsklar-ship-it/taskforge`
- Type: Web Service
- Environment: Python
- Build command: `python manage.py collectstatic --noinput`
- Run command: `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT`

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
