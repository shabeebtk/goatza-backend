"""Throttle for the public enquiry form."""

from rest_framework.throttling import AnonRateThrottle


class EnquirySubmitThrottle(AnonRateThrottle):
    """
    30/hour per IP on POST /enquiries/submit/<org_username> (``enquiry_submit``
    in DEFAULT_THROTTLE_RATES).

    PER IP FOR EVERY CALLER, signed in or not — the same key as
    ``apps.pwa.throttles.AppEventsThrottle``. A plain AnonRateThrottle returns
    no key for an authenticated request, so a signed-in caller would otherwise
    have no limit at all on an anonymous-friendly write. Every request counts,
    refused ones included — a typo, a missing field, a too_many_enquiries.

    GENEROUS ON PURPOSE. Families on one Wi-Fi and phones behind one carrier
    NAT share an address, and a QR poster at a trial day puts many parents at
    one ground in the same hour; at 10 an hour that crowd was refused.

    This is the coarse limit. The fine ones — the same phone at one org and
    across all orgs in 24 hours — are database checks in the submit service,
    and with the honeypot they do the real anti-spam work, because a family
    behind one NAT and a bot behind many addresses look nothing alike to an IP.
    """

    scope = "enquiry_submit"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }
