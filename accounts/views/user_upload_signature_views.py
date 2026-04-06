from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from utils.response import response_data
from services.storage.factory import get_storage_service


class GetUploadConfigAPIView(APIView):
    permission_classes = [IsAuthenticated]

    ALLOWED_TYPES = {"profile", "cover", "posts"}

    def get(self, request):
        upload_type = request.query_params.get("type")
        count = int(request.query_params.get("count", 1))

        if upload_type not in self.ALLOWED_TYPES:
            return response_data(
                False,
                error="Invalid upload type",
                status_code=400
            )

        # Safety limits (important)
        if count < 1 or count > 10:
            return response_data(
                False,
                error="Invalid count (1–10 allowed)",
                status_code=400
            )

        try:
            storage = get_storage_service()

            config = storage.get_upload_config(
                user=request.user,
                upload_type=upload_type,
                count=count
            )

            return response_data(success=True, data=config)

        except ValueError as ve:
            return response_data(False, error=str(ve), status_code=400)

        except Exception as e:
            return response_data(
                False,
                error=f"Failed to generate upload config: {str(e)}",
                status_code=500
            )