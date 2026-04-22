from django.core.validators import validate_email
from django.core.exceptions import ValidationError
import uuid


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


import re

RESERVED_USERNAMES = {
    "admin", "root", "support", "help", "api", "system",
    "null", "undefined", "owner", "moderator", "staff",
    "login", "signup", "me", "settings", "profile",
    "user", "users", "dashboard"
}


def validate_username_format(username: str):
    username = username.strip()

    if len(username) < 3:
        raise ValueError("Username must be at least 3 characters")

    if len(username) > 20:
        raise ValueError("Username too long")

    if username.lower() in RESERVED_USERNAMES:
        raise ValueError("This username is not allowed")

    # only letters, numbers, underscore
    if not re.match(r"^[a-zA-Z0-9_]+$", username):
        raise ValueError("Only letters, numbers, and underscores allowed")

    # cannot start or end with _
    if username.startswith("_") or username.endswith("_"):
        raise ValueError("Username cannot start or end with underscore")

    # optional: prevent double underscore
    if "__" in username:
        raise ValueError("Username cannot contain consecutive underscores")

    return username



def is_valid_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False