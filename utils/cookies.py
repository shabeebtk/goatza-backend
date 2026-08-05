from django.conf import settings

# Keep equal to SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"] (30 days).
REFRESH_COOKIE_MAX_AGE = 60 * 60 * 24 * 30


def _cookie_domain():
    # None (host-only) is correct for the Vercel `/api` proxy topology.
    # Set AUTH_COOKIE_DOMAIN (e.g. ".goatza.com") only if the app later
    # moves to an api.goatza.com subdomain AND the cookie must be visible
    # to Next.js middleware.
    return getattr(settings, "AUTH_COOKIE_DOMAIN", None)


def set_refresh_key_cookie(response, refresh_token):
    response.set_cookie(
        key="refresh_token",
        value=str(refresh_token),
        max_age=REFRESH_COOKIE_MAX_AGE,  # persistent — survives browser restarts
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
        domain=_cookie_domain(),
    )


def delete_refresh_key_cookie(response):
    # Attributes must mirror set_refresh_key_cookie or deletion silently fails.
    response.delete_cookie(
        key="refresh_token",
        path="/",
        samesite="Lax",
        domain=_cookie_domain(),
    )
