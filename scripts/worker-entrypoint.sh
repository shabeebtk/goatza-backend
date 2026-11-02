#!/bin/bash
# The worker container's two Celery processes — Dockerfile.worker's CMD.
#
#   default  every queue but media: emails, pushes, deletes, the sweeper, the
#            heartbeat. Embeds beat (-B), which is correct for exactly ONE
#            process, and this is it.
#   media    media.process_job only (CELERY_TASK_ROUTES), one at a time. A
#            conversion holds its slot for minutes; on its own process it can
#            no longer starve the heartbeat and every other job, and
#            concurrency 1 keeps a 512 MB instance to one ffmpeg. The child is
#            recycled every 20 jobs, before decoder memory creeps.
#
# If EITHER exits, the other is stopped and the script exits non-zero, so
# Render restarts the whole container instead of running half a worker.
# SIGTERM (a deploy, a restart) is forwarded to both: Celery's warm shutdown.
# Bash, not sh: `wait -n` is a bash builtin. As PID 1 a signal with no trap is
# ignored, which is why the trap is not optional.

set -u

pids=()

stop_workers() {
    trap - TERM INT
    kill -TERM "${pids[@]}" 2>/dev/null || true
}
trap stop_workers TERM INT

celery -A core worker -Q celery -B -l info --concurrency=2 -n default@%h &
pids+=("$!")

celery -A core worker -Q media -l info --concurrency=1 -n media@%h \
    --max-tasks-per-child=20 &
pids+=("$!")

# Returns as soon as either child exits, or when a trapped signal arrives.
wait -n
status=$?

stop_workers
wait

# A worker that exited on its own is a failure even if it reported 0.
if [ "$status" -eq 0 ]; then
    status=1
fi
exit "$status"
