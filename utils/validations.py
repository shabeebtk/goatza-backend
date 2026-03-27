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
