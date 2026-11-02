from django.apps import AppConfig


class MediaConfig(AppConfig):
    name = 'apps.media'
    label = 'media'

    def ready(self):
        # Connects media_job_finished → publishing held posts/highlights.
        from apps.media import receivers  # noqa: F401
        # Connects worker_ready → the media worker's startup recovery. Here,
        # not in tasks.py: the worker imports tasks lazily, possibly after
        # worker_ready has already fired.
        from apps.media import worker_signals  # noqa: F401
