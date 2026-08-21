"""
Backfill UsernameRegistry from the two display columns.

REFUSES TO RUN rather than paper over a problem. If two actors hold the same
handle across the tables, or a stored handle does not survive the new
validator, this migration raises and the deploy stops. Either outcome needs a
human decision (who keeps @kochifc? what does the invalid handle become?), and
a data migration that answered it by guessing — skipping the row, silently
renaming — would leave a registry that no longer describes the database while
claiming the namespace is locked.

The dev database is empty and there are no production users, so no resolution
strategy is coded here on purpose.
"""

from django.db import migrations

from utils.validations import validate_username_format


def backfill(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Organization = apps.get_model("organization", "Organization")
    UsernameRegistry = apps.get_model("usernames", "UsernameRegistry")

    seen = {}
    rows = []
    invalid = []

    def collect(queryset, kind, field):
        for pk, username in queryset.values_list("id", "username"):
            # Users may legitimately have no handle yet (the column is
            # nullable); there is nothing to register for them.
            if not username:
                continue

            try:
                normalized = validate_username_format(username)
            except ValueError as e:
                invalid.append(f"{kind} {pk}: {username!r} — {e}")
                continue

            if normalized in seen:
                raise RuntimeError(
                    "Cannot backfill UsernameRegistry: @"
                    f"{normalized} is held by both {seen[normalized]} and "
                    f"{kind} {pk}. Resolve the collision by hand (rename one "
                    "of them), then re-run the migration."
                )

            seen[normalized] = f"{kind} {pk}"
            rows.append(UsernameRegistry(username_lower=normalized, **{field: pk}))

    collect(User.objects.all(), "user", "user_id")
    collect(Organization.objects.all(), "organization", "organization_id")

    if invalid:
        raise RuntimeError(
            "Cannot backfill UsernameRegistry: "
            f"{len(invalid)} handle(s) fail the current validator. Fix them "
            "before migrating:\n  " + "\n  ".join(invalid)
        )

    UsernameRegistry.objects.bulk_create(rows, batch_size=500)


def unbackfill(apps, schema_editor):
    # The registry is derived data; dropping every row is the exact inverse.
    apps.get_model("usernames", "UsernameRegistry").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("usernames", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
