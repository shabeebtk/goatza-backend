from rest_framework.views import APIView
from accounts.models import (
    User, UserProfile
)
from rest_framework.permissions import IsAuthenticated
from accounts.serializers.user_serializers import UserSerializer, UserFullSerializer
from utils.response import response_data
from utils.cache import cache_set, cache_get
from utils.cache_keys import CacheKeys

class GetUserDetails(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        detail = request.query_params.get("detail")
        user_id = request.user.id

        # get cache 
        cache_key = CacheKeys.user_details(user_id, detail=detail)
        cached_data = cache_get(cache_key)

        if cached_data:
            return response_data(success=True, data=cached_data)

        # Force optimized query
        user = User.objects.select_related("profile").get(id=user_id)

        if detail == "full":
            serializer = UserFullSerializer(user)
        else:
            serializer = UserSerializer(user)

        data = serializer.data

        # Cache for 2 minutes
        cache_set(cache_key, data, timeout=120)

        return response_data(success=True, data=data)
    


