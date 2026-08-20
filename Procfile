release: python manage.py migrate --noinput
web: gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --threads 2
# A `worker: python manage.py process_tasks` line joins this file with issue #5,
# which builds the Postgres-backed queue and the management command behind it.
