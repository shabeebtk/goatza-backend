# Generated for the post-signup onboarding flow.

from django.db import migrations, models


def mark_existing_users_onboarded(apps, schema_editor):
    """Existing users are already active — they must never be forced into onboarding."""
    User = apps.get_model("accounts", "User")
    User.objects.update(is_onboarding_completed=True)


def reverse_noop(apps, schema_editor):
    # Nothing to undo: dropping the column removes the state entirely.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_add_org_user_role_and_is_role_confirmed'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='is_onboarding_completed',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(mark_existing_users_onboarded, reverse_noop),
    ]
