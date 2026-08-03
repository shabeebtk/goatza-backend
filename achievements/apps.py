from django.apps import AppConfig


class AchievementsConfig(AppConfig):
    name = 'achievements'

    def ready(self):
        # Registers the Organization pre_delete hook that keeps the
        # verification-pairing constraint satisfiable. See achievements/signals.py.
        from achievements import signals  # noqa: F401
