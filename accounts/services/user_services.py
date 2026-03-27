import random
import string
from accounts.models import User

def generate_unique_username(base: str) -> str:
    """
    Generate unique username based on base string
    """
    base = base.lower().replace(" ", "")
    username = base

    while User.objects.filter(username=username).exists():
        suffix = "".join(random.choices(string.digits, k=4))
        username = f"{base}{suffix}"

    return username