def set_refresh_key_cookie(response, refresh_token):
    response.set_cookie(
        key="refresh_token",
        value=str(refresh_token),
        httponly=True,
        secure=True,
        samesite="Lax",   
        path="/",
    )