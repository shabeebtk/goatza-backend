"""
Show — and, when asked, apply — the R2 bucket's CORS policy.

    python manage.py r2_setup            # same as --show: read-only
    python manage.py r2_setup --apply    # write the CORS policy below

WHY THIS EXISTS. The browser PUTs media straight into the bucket, cross-origin,
so the bucket's CORS policy is part of the upload path — and it used to live
ONLY in the Cloudflare dashboard, where nothing in code could check it. A wrong
policy does not look like a CORS problem on a phone. Mobile Safari reports a
refused preflight, and a response it is not allowed to read, as a plain network
error, so the symptom is "Network error while uploading" — at 0% (or 70% for a
video, whose bar starts after the encode) when the preflight is refused, or at
~99% when the bytes went up and the response could not be read.

WHAT --apply WRITES, as one rule:

    AllowedOrigins  settings.CORS_ALLOWED_ORIGINS, then R2_CORS_EXTRA_ORIGINS
    AllowedMethods  GET, HEAD, PUT
    AllowedHeaders  Content-Type  — the one header a presigned PUT signs; a
                                    multipart part PUT sends none at all
    ExposeHeaders   ETag          — multipart completion needs every part's
                                    ETag, and a cross-origin script cannot read
                                    a header the policy does not expose
    MaxAgeSeconds   3600

It refuses — exit code 1, nothing written — when the origin list would be
empty or contain "*". An empty list blocks every upload; a wildcard lets any
site drive a visitor's browser at our bucket.

Run --show first: it proves the credentials and the bucket, prints what is
there now and what --apply would put in its place. --apply replaces the WHOLE
CORS policy, so an origin added by hand in the dashboard and missing from both
settings disappears — add it to R2_CORS_EXTRA_ORIGINS instead.

WITH AN UPLOAD GATEWAY (settings.R2_UPLOAD_GATEWAY_URL set), browsers never
talk to the bucket: they PUT to the goatza-upload-gateway Worker, which answers
CORS itself from its own ALLOWED_ORIGINS and forwards server-to-server, where
CORS does not exist. The bucket's policy then only matters again if the
gateway is switched off — which is exactly why it should stay applied: unsetting
R2_UPLOAD_GATEWAY_URL is the rollback, and it sends browsers straight back
here. --show prints which of the two paths uploads take.

LIFECYCLE IS READ, NEVER WRITTEN. R2 gives every bucket a "Default Multipart
Abort Rule" that clears an abandoned multipart upload after 7 days, and that is
all the multipart flow needs. PutBucketLifecycleConfiguration replaces the
whole rule list, so code that "just adds one rule" would silently delete
whatever an operator set up in the dashboard.

Nothing here prints a credential: the endpoint host and the bucket name only.
"""

import json
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from services.storage.factory import get_storage_service
from services.storage.r2 import endpoint_url

ALLOWED_METHODS = ["GET", "HEAD", "PUT"]

# The header production uploads already send and sign. A multipart part PUT
# sends no extra header, so nothing else needs allowing.
ALLOWED_HEADERS = ["Content-Type"]

# NEW with multipart: the client completes an upload with each part's ETag,
# read off the part's response — invisible to a cross-origin script unless
# exposed here. Safari is strict about it.
EXPOSE_HEADERS = ["ETag"]

MAX_AGE_SECONDS = 3600


def cors_origins():
    """
    CORS_ALLOWED_ORIGINS, then R2_CORS_EXTRA_ORIGINS — trimmed, without a
    trailing slash (an origin never has one, and the browser compares it
    exactly), de-duplicated, order kept.
    """
    origins = []
    for origin in [
        *settings.CORS_ALLOWED_ORIGINS,
        *getattr(settings, "R2_CORS_EXTRA_ORIGINS", []),
    ]:
        origin = origin.strip().rstrip("/")
        if origin and origin not in origins:
            origins.append(origin)
    return origins


def refusal_reason(origins):
    """Why --apply must not write this origin list, or None."""
    if not origins:
        return (
            "No origins to allow: CORS_ALLOWED_ORIGINS and "
            "R2_CORS_EXTRA_ORIGINS are both empty. Writing this policy would "
            "block every browser upload."
        )
    wildcards = [origin for origin in origins if "*" in origin]
    if wildcards:
        return (
            f"Refusing to allow {', '.join(wildcards)}: a wildcard origin lets "
            f"any site drive a visitor's browser at this bucket. List the "
            f"real origins instead."
        )
    return None


def desired_cors_rules(origins):
    """The whole policy --apply writes — one rule."""
    return [
        {
            "AllowedOrigins": origins,
            "AllowedMethods": ALLOWED_METHODS,
            "AllowedHeaders": ALLOWED_HEADERS,
            "ExposeHeaders": EXPOSE_HEADERS,
            "MaxAgeSeconds": MAX_AGE_SECONDS,
        }
    ]


def _normalised(rules):
    """A policy reduced to what matters, so key order and casing do not differ."""
    return sorted(
        (
            tuple(sorted(rule.get("AllowedOrigins", []))),
            tuple(sorted(m.upper() for m in rule.get("AllowedMethods", []))),
            tuple(sorted(h.lower() for h in rule.get("AllowedHeaders", []))),
            tuple(sorted(h.lower() for h in rule.get("ExposeHeaders", []))),
            rule.get("MaxAgeSeconds"),
        )
        for rule in rules
    )


class Command(BaseCommand):
    help = (
        "Show the R2 bucket's CORS and lifecycle rules (default), or apply "
        "the CORS policy browser uploads need (--apply)."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--show",
            action="store_true",
            help="Read-only (the default): what is set now, and what --apply "
                 "would write.",
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Replace the bucket's CORS policy with the one --show prints. "
                 "Never touches lifecycle rules.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        origins = cors_origins()
        refusal = refusal_reason(origins)

        # Before touching the bucket at all: a refused --apply must not even
        # look like it started.
        if apply and refusal:
            raise CommandError(refusal)

        storage = get_storage_service()
        host = urlparse(endpoint_url()).netloc

        try:
            storage.head_bucket()
        except Exception as e:
            raise CommandError(
                f"Cannot reach bucket {settings.R2_BUCKET!r} at {host}: {e}. "
                f"Check R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY "
                f"and R2_BUCKET."
            )

        gateway = settings.R2_UPLOAD_GATEWAY_URL

        self.stdout.write(self.style.MIGRATE_HEADING("R2 bucket"))
        self.stdout.write(f"  endpoint      {host}")
        self.stdout.write(
            f"  uploads via   {gateway} (gateway)" if gateway
            else "  uploads via   direct (browsers PUT to the endpoint above)"
        )
        self.stdout.write(f"  bucket        {settings.R2_BUCKET}")
        self.stdout.write(f"  public URL    {settings.MEDIA_PUBLIC_BASE_URL}")
        self.stdout.write("")

        if gateway:
            self.stdout.write(self.style.WARNING(
                "  Browsers upload through the gateway, which answers CORS "
                "itself (ALLOWED_ORIGINS in goatza-upload-gateway's "
                "wrangler.toml). The bucket's CORS policy below no longer "
                "matters to them — keep it applied anyway: unsetting "
                "R2_UPLOAD_GATEWAY_URL is the rollback, and it sends browsers "
                "straight back to the bucket."
            ))
            self.stdout.write("")

        current = storage.get_bucket_cors()

        if apply:
            rules = desired_cors_rules(origins)
            self._json("CORS before", current)
            storage.put_bucket_cors(rules)
            self._json("CORS after", storage.get_bucket_cors())
            self.stdout.write(self.style.SUCCESS("CORS policy applied."))
            return

        self._json("CORS rules now", current)

        lifecycle = storage.get_bucket_lifecycle_rules()
        self._json(
            "Lifecycle rules now (read-only: this command never writes them)",
            lifecycle,
        )
        if not any("AbortIncompleteMultipartUpload" in rule for rule in lifecycle):
            self.stdout.write(self.style.WARNING(
                "  No rule above aborts incomplete multipart uploads. R2 "
                "normally gives every bucket a 'Default Multipart Abort Rule' "
                "(7 days); check the bucket's object lifecycle rules in the "
                "Cloudflare dashboard."
            ))
            self.stdout.write("")

        if refusal:
            self.stdout.write(self.style.ERROR(f"--apply would refuse: {refusal}"))
            return

        rules = desired_cors_rules(origins)
        self._json("CORS rules --apply would write", rules)

        if _normalised(current) == _normalised(rules):
            self.stdout.write(self.style.SUCCESS(
                "The bucket already has exactly this policy."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "The bucket's policy differs. Run with --apply to write it."
            ))

    def _json(self, title, value):
        self.stdout.write(self.style.MIGRATE_HEADING(title))
        self.stdout.write(json.dumps(value, indent=2, default=str))
        self.stdout.write("")
