from rest_framework.response import Response

def response_data(success=True, message="", data=None, status_code=200, error=None):
    """
    Standard API response format.
    success: Boolean (True/False)
    message: String (info or error message)
    data: Dict or List (payload data)
    error: Debug/system error details (optional, not for end-users)
    """
    response = {
        "success": success,
        "message": message,
        "data": data if data is not None else {},
    }

    if error:  # Only include when provided
        response["error"] = error

    return Response(response, status=status_code)
