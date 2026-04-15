import random
import string
from accounts.models import User

def generate_unique_username(base: str) -> str:
    """
    Generate unique username based on base string
    """
    base = base.lower().replace(" ", "")
    suffix = "".join(random.choices(string.digits, k=2))
    username = f"{base}{suffix}"

    while User.objects.filter(username=username).exists():
        suffix = "".join(random.choices(string.digits, k=4))
        username = f"{base}{suffix}"

    return username


class UserService:

    @staticmethod
    def get_user_by_id(user_id):
        try:
            return User.objects.get(id=user_id)
        except User.DoesNotExist:
            return None