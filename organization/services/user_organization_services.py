from django.core.cache import cache
from accounts.models import User
from organization.models import Organization
from core.constant import TYPE_USER, TYPE_ORGANIZATION

class UserOrganizationService:
    
    def get_user_or_org_by_username(username):
        """
        Resolves a username to either a User or Organization.
        Returns:
            {
                "type": "user" | "org",
                "id": UUID
            }
        Raises:
            ValueError if not found
        """
        if not username:
            raise ValueError("Username is required")

        cache_key = f"profile_lookup:{username}"
        cached = cache.get(cache_key)

        if cached:
            return cached

        # Try user first (fast path)
        user = User.objects.only("id").filter(username=username).first()
        if user:
            result = {"type": TYPE_USER, "id": user.id}
            cache.set(cache_key, result, timeout=300)
            return result

        # Fallback to org
        org = Organization.objects.only("id").filter(username=username).first()
        if org:
            result = {"type": TYPE_ORGANIZATION, "id": org.id}
            cache.set(cache_key, result, timeout=300)
            return result

        raise ValueError("Profile not found")