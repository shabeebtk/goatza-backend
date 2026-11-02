"""
``media_job_finished`` — sent once a MediaJob reaches ``ready`` or ``failed``.

    media_job_finished.send_robust(sender=MediaJob, job=job)

apps/media/receivers.py listens: every PendingPublication waiting on the job
gets a try_publish. The worker never has to know who is waiting.

SENT WITH ``send_robust``, by MediaJobService.finish: a receiver that raises is
logged at ERROR and never un-finishes the job. The files are already in the
bucket and the row already says ``ready``; a listener's bug must not turn that
into a failed job or a Celery retry of a transcode that succeeded.
"""

from django.dispatch import Signal

media_job_finished = Signal()
