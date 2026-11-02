"""
What the backend does when a media job finishes — connected in
MediaConfig.ready().

Runs on the worker, inside MediaJobService.finish, which sends the signal with
send_robust: anything raised here is logged there and never un-finishes the
job. A publication this misses (another run held its lock) is settled by
media.sweep_jobs.
"""

from django.dispatch import receiver

from apps.media.models import PendingPublication
from apps.media.services.publication_service import PublicationService
from apps.media.signals import media_job_finished


@receiver(media_job_finished, dispatch_uid="media.publish_waiting_publications")
def publish_waiting_publications(sender, job, **kwargs):
    """Every post / highlight waiting on this job gets a chance to publish."""
    for publication in job.publications.filter(
        status=PendingPublication.Status.PROCESSING
    ):
        PublicationService.try_publish(publication)
