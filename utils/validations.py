import re
import uuid

from django.core.validators import validate_email
from django.core.exceptions import ValidationError


def is_valid_email(email: str) -> bool:
    """
    Validate email using Django's built-in EmailValidator.
    Returns True if valid, False otherwise.
    """
    try:
        validate_email(email)
        return True
    except ValidationError:
        return False


def is_valid_password(password: str) -> bool:
    """
    Validate password.
    Rule: minimum 6 characters
    Returns True if valid, False otherwise
    """
    if not password:
        return False
    return len(password) >= 6


# ─────────────────────────────────────────────
# PHONE
# ─────────────────────────────────────────────
#
# Phone is NOT a login identifier — the login view authenticates by email and
# password only — so this is a format check, not an identity check. Nothing
# here proves the number belongs to the person who typed it; that arrives with
# SMS verification.

# accounts.User.phone is CharField(max_length=15), and the "+" counts against
# it. A number that cannot be stored is not a valid number, so the bound lives
# here rather than being discovered by the database.
PHONE_MAX_LENGTH = 15

# E.164-ish: an optional leading "+" and 8-15 digits. Deliberately loose about
# country prefixes — the app has users in India today and a hard-coded +91
# would be a rule to unpick the first time it does not.
PHONE_RE = re.compile(r"^\+?\d{8,15}$")


def is_valid_phone(phone: str) -> bool:
    """
    True if `phone` is a storable, plausibly-dialable number.

    Callers must strip() and store the SAME string they checked — the length
    bound is on the stored value, so "+" plus fifteen digits fails here rather
    than being truncated by the column.
    """
    if not phone or not isinstance(phone, str):
        return False

    phone = phone.strip()

    if len(phone) > PHONE_MAX_LENGTH:
        return False

    return bool(PHONE_RE.match(phone))


# ─────────────────────────────────────────────
# USERNAMES — the single source of truth
# ─────────────────────────────────────────────
#
# Users and organizations draw from ONE namespace (see the `usernames` app).
# Everything that writes a handle — profile edits, org create/update, the
# auto-generators behind signup and Google OAuth — normalizes through
# validate_username_format() below, so there is exactly one charset and one
# length bound in the codebase. The mention parser
# (posts/services/post_content_service.py) and its mirror in the frontend's
# PostCard.tsx tokenize the SAME charset; widening it here means widening it
# in all three.

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 30

# Lowercase letters, digits and underscore. NO dot: organizations used to allow
# one, which made "@kochi.fc" a handle a user could never hold and a token the
# mention parser had to special-case. Dropping it is what lets the two tables
# share a namespace.
USERNAME_CHARSET_RE = re.compile(r"^[a-z0-9_]+$")

# Handles that may never be claimed by either actor type.
#
# The first block mirrors the frontend's live route segments — `/[username]`
# sits directly beside them, so a user holding "matches" would shadow a real
# page. WHEN A ROUTE IS ADDED TO THE FRONTEND, ADD IT HERE.
RESERVED_USERNAMES = frozenset({
    # ── frontend route segments (src/app/**) ──────────────────
    "auth", "card", "chat", "coaching", "cv", "explore", "guidelines",
    "highlights", "home", "join", "matches", "messages", "notifications",
    "organization", "posts", "recruitments", "safety", "scouting", "search",

    # ── pre-existing entries (kept) ───────────────────────────
    "admin", "root", "support", "help", "api", "system",
    "null", "undefined", "owner", "moderator", "staff",
    "login", "signup", "me", "settings", "profile",
    "user", "users", "dashboard",

    # ── infrastructure ────────────────────────────────────────
    "www", "static", "assets", "media", "cdn", "img",
    "favicon", "robots", "sitemap", ".well-known",

    # ── auth surface ──────────────────────────────────────────
    "logout", "register", "verify", "reset", "password",
    "oauth", "callback", "token", "refresh",

    # ── product surface ───────────────────────────────────────
    "feed", "discover", "trending", "saved", "mentions",
    "verifications", "squad", "squads", "team", "teams", "club",
    "academy", "trials", "about", "terms", "privacy", "legal",
    "contact", "download", "app", "pricing", "blog", "careers", "press",

    # ── impersonation risk ────────────────────────────────────
    "goatza", "goatzaapp", "official", "verified",
    "security", "billing", "noreply", "no-reply",
})


def validate_username_format(username: str) -> str:
    """
    Normalize and validate a handle.

    RETURNS the normalized (stripped, lowercased) value — callers must use the
    return value, never their own input, or the thing that lands in the
    database is not the thing that was checked.

    Raises ValueError with a user-facing message on the first rule that fails.
    """
    username = (username or "").strip().lower()

    if len(username) < USERNAME_MIN_LENGTH:
        raise ValueError(
            f"Username must be at least {USERNAME_MIN_LENGTH} characters"
        )

    if len(username) > USERNAME_MAX_LENGTH:
        raise ValueError(
            f"Username cannot be longer than {USERNAME_MAX_LENGTH} characters"
        )

    if not USERNAME_CHARSET_RE.match(username):
        raise ValueError("Only letters, numbers, and underscores allowed")

    if username.startswith("_") or username.endswith("_"):
        raise ValueError("Username cannot start or end with underscore")

    if "__" in username:
        raise ValueError("Username cannot contain consecutive underscores")

    # A purely numeric handle is indistinguishable from an id in a URL.
    if username.isdigit():
        raise ValueError("Username cannot be only numbers")

    if username in RESERVED_USERNAMES:
        raise ValueError("This username is not allowed")

    return username


def is_valid_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False
