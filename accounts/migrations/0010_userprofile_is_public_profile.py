from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Public profiles, on by default.

    default=True is the whole point: every profile that exists today keeps
    working as a public link the moment the feature ships, and only someone who
    deliberately turns the toggle off disappears from the logged-out web.
    """

    dependencies = [
        ("accounts", "0009_user_is_onboarding_completed"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="is_public_profile",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Visible to logged-out visitors. "
                    "Does not affect in-app visibility."
                ),
            ),
        ),
    ]
