import random
import string
import requests
import secrets
import logging
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.conf import settings
from django.core.cache import cache
from rest_framework.views import APIView
from accounts.models import (
    User, UserProfile
)
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from accounts.serializers.user_serializers import UserSerializer
from utils.response import response_data
from accounts.services.user_services import generate_unique_username
from utils.passwords import generate_random_password
from utils.cookies import set_refresh_key_cookie


GOOGLE_AUTH_URL='https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL='https://oauth2.googleapis.com/token'
GOOGLE_USER_INFO_URL='https://www.googleapis.com/oauth2/v2/userinfo'

logger = logging.getLogger(__name__)

class GoogleLoginUrlView(APIView):
    """
    generates a Google OAuth2 login URL 
    """
    def get(self, request):        
        # Build the Google OAuth2 authorization URL
        state = secrets.token_urlsafe(16)
        cache.set(f"google_oauth_state_{state}", True, timeout=300)
        
        auth_url = (
            f"{GOOGLE_AUTH_URL}?response_type=code"
            f"&client_id={settings.GOOGLE_AUTH_CLIENT_ID}"
            f"&redirect_uri={settings.GOOGLE_CALLBACK_URI}"
            f"&scope={settings.GOOGLE_AUTH_SCOPE}"
            f"&state={state}"
            f"&access_type=offline"
            f"&prompt=consent"
        )
        data = {
            "auth_url": auth_url
        }
        return response_data(success=True, data=data)
        


class GoogleAuthCallbackView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        code = request.query_params.get("code")
        state = request.query_params.get("state")

        if not code:
            return response_data(False, "Authorization code is required", status_code=400)

        # Verify state from cache
        cache_key = f"google_oauth_state_{state}"
        if not state or not cache.get(cache_key):
            return response_data(False, "Invalid or expired state", status_code=400)

        # delete after use
        cache.delete(cache_key)

        try:
            #  Exchange code for token
            token_res = requests.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_AUTH_CLIENT_ID,
                    "client_secret": settings.GOOGLE_AUTH_CLIENT_SECRET,
                    "redirect_uri": settings.GOOGLE_CALLBACK_URI,
                    "grant_type": "authorization_code",
                },
                timeout=10
            )
            token_res.raise_for_status()

            access_token = token_res.json().get("access_token")

            if not access_token:
                logger.error("Google access token missing")
                return response_data(False, "Google login failed", status_code=400)

            #  Get user info
            userinfo_res = requests.get(
                GOOGLE_USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10
            )
            userinfo_res.raise_for_status()

            userinfo = userinfo_res.json()

            email = userinfo.get("email")
            name = userinfo.get("name") or (email.split("@")[0] if email else "user")

            if not email:
                logger.warning("Google email missing in response")
                return response_data(False, "Email not found", status_code=400)

            #  Create or get user
            with transaction.atomic():
                user, created = User.objects.get_or_create(
                    email=email,
                    defaults={
                        "is_email_verified": True
                    }
                )

                if created:
                    username = generate_unique_username(email.split("@")[0])
                    user.username = username
                    user.set_password(generate_random_password())
                    user.save()

                    logger.info(f"New Google user created: {email}")

                else:
                    if not user.is_email_verified:
                        user.is_email_verified = True
                        user.save(update_fields=["is_email_verified"])

                # Profile
                UserProfile.objects.get_or_create(
                    user=user,
                    defaults={"name": name}
                )

            # Step 4: Generate JWT
            refresh = RefreshToken.for_user(user)
            logger.info(f"Google login success: {email}")
            
            response = response_data(
                True,
                "Login successful",
                {
                    "access": str(refresh.access_token),
                    "user": UserSerializer(user).data
                }
            )
            # Set refresh token in cookie
            set_refresh_key_cookie(response, refresh_token=refresh)
            return response

        except requests.exceptions.RequestException as e:
            logger.error(f"Google API error: {str(e)}")
            return response_data(False, "Google authentication failed", status_code=400)

        except Exception as e:
            logger.error(f"Google login error: {str(e)}")
            return response_data(False, "Internal server error", status_code=500)