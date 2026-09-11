"""
Rate limits for the Google Places proxy (docs/PLACES_MIGRATION.md section 4.3).

|  Scope                 | Authenticated | Anonymous (by IP) |
|------------------------|---------------|-------------------|
| places_autocomplete    | 60/min        | 20/min            |
| places_details         | 20/min        | 10/min            |

Each row is TWO classes, not one, because DRF resolves exactly one rate per
scope name — the ``*_anon`` scopes in DEFAULT_THROTTLE_RATES carry the
anonymous half. Both classes go on the view; whichever one does not apply to
the caller returns None and steps aside.

Per USER rather than per actor (unlike messaging.throttles.ActorScopedThrottle):
searching for a city is something a person does, and the same person switching
to their org header is not a second budget's worth of Google calls.

Autocomplete is the loose one and Details the tight one, which is the reverse
of how they read: a completed search is 3–5 autocompletes and exactly ONE
details call (section 3 rule 4), so 20 details a minute is twenty completed
searches, while 60 autocompletes a minute is a fast typist finding four or five
places. A client burning through the Details budget is calling it for the list
instead of the selection, and that is precisely what should be stopped.
"""

from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class PlacesUserThrottle(UserRateThrottle):
    """
    The authenticated half. Subclasses set ``scope``.

    Overrides ``get_cache_key`` so an anonymous caller is skipped entirely:
    DRF's UserRateThrottle falls back to the IP for anonymous requests, which
    would put anonymous callers in BOTH buckets and make the tighter anonymous
    rate a fiction that only appears to hold because it trips first.
    """

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)

        if not user or not user.is_authenticated:
            return None

        return self.cache_format % {"scope": self.scope, "ident": user.pk}


class AutocompleteUserThrottle(PlacesUserThrottle):
    scope = "places_autocomplete"


class AutocompleteAnonThrottle(AnonRateThrottle):
    scope = "places_autocomplete_anon"


class DetailsUserThrottle(PlacesUserThrottle):
    scope = "places_details"


class DetailsAnonThrottle(AnonRateThrottle):
    scope = "places_details_anon"
