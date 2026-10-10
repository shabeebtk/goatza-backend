# Goatza — backend

Django + DRF API. `CLAUDE.md` holds the working notes on each subsystem; this
file holds what you need to deploy and run it.

## Run it locally

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then fill it in
python manage.py migrate
python manage.py runserver
```

Redis is optional in development. Without it the cache degrades to a no-op,
background jobs run inline in the request, and realtime chat is the one thing
that genuinely needs it.

## Deployment

Two Render services on this repo, plus Postgres and Redis.

### Web service

Unchanged. Set the Health Check Path to `/healthz` in the service's settings —
there is no way to declare it from code, and until it is set Render falls back
to "the port is open", which a process with a dead database still satisfies.

### Worker service (Background Worker)

Start command:

```
celery -A core worker -B -l info --concurrency=2
```

- **`CELERY_ENABLED=True` on BOTH the web service and the worker.** The web
  service publishes and the worker consumes. With it off on the web service
  nothing is ever queued; with it off on the worker the worker runs its own
  jobs inline.
- The worker needs the **same env vars** as the web service — settings refuses
  to boot without the secrets, and the Firebase init in
  `apps/notifications/apps.py` crashes without its key. Put them in a shared
  Environment Group. Background workers are not on Render's free plan.
- **`-B` embeds beat, and that is correct for exactly ONE worker process.** The
  day a second worker is added, beat moves to its own process
  (`celery -A core beat -l info`) and the workers drop `-B`. Two embedded beats
  fire every schedule twice — two announcement drains, two nightly purges, two
  of everything.
- **Health:** the `"worker"` field in `GET /healthz`, which reports `ok`,
  `stale` or `unknown` from the worker's heartbeat. It never affects the HTTP
  status: only Postgres does that, because recycling the web container would
  not bring a dead worker back and would drop every in-flight request to
  replace a healthy process.

### Cutover from Render Cron

The scheduled jobs (`CELERY_BEAT_SCHEDULE` in `core/settings.py`) used to be
Render Cron services running the management commands directly. The commands are
unchanged and still the way to run any of them by hand.

**Retire the Cron services only after the worker's heartbeat reads `"ok"` at
`/healthz`.** Until then Cron is what is actually doing the work, and running
both is harmless: every scheduled task is idempotent.

See the "Background jobs (Celery)" section of `CLAUDE.md` for the full task
table, the schedule and the fallback behaviour when there is no worker.
