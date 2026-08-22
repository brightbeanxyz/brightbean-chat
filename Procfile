release: python manage.py migrate --noinput
web: gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --threads 2
# The queue worker (SPEC §15). Safe to scale past one: the claim statement uses
# FOR UPDATE SKIP LOCKED, so concurrent workers take disjoint batches. Hosts
# that cannot run a long-lived process should instead point a cron service at
# /internal/tick (see TICK_TOKEN in .env.example).
worker: python manage.py process_tasks
