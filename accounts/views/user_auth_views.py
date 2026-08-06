import random
import string
import logging
import jwt as pyjwt
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.hashers import make_password
from django.core.cache import cache
from django.db import IntegrityError, transaction
from rest_framework.views import APIView
from accounts.models import (
    User, UserProfile
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken, TokenError
from rest_framework_simplejwt.token_blacklist.models import (
    OutstandingToken, BlacklistedToken,
)
from accounts.serializers.user_serializers import UserSerializer
from accounts.services.user_services import generate_unique_username
from utils.response import response_data
from utils.validations import is_valid_email, is_valid_password
from utils.otp_validation import generate_otp, verify_otp
from utils.emails import send_email, send_email_async
from accounts.throttles import (
    SignupThrottle, LoginThrottle, OTPThrottle, ForgotPasswordThrottle,
    ChangePasswordThrottle
)
from utils.cookies import set_refresh_key_cookie, delete_refresh_key_cookie

logger = logging.getLogger(__name__)

GRACE_TTL_SECONDS = 60
GRACE_CACHE_KEY = "auth:refresh-grace:{jti}"


# Views here 
class UserSignupAPIView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [SignupThrottle]

    def post(self, request):
        email = request.data.get("email")
        password = request.data.get("password")
        name = request.data.get("name")
        role = request.data.get("role")

        if not email or not password:
            return response_data(False, "Email and password are required", status_code=400)

        if not is_valid_email(email):
            return response_data(False, "Invalid email", status_code=400)

        if not is_valid_password(password):
            return response_data(False, "Password must be at least 6 characters", status_code=400)

        if role not in User.Role.values:
            return response_data(False, "Invalid role", status_code=400)

        if not name:
            name = email.split("@")[0]

        base_username = name
        username = base_username
        username = generate_unique_username(base=name)

        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    email=email,
                    username=username,
                    password=password,
                    role=role,
                    is_active=False
                )

                UserProfile.objects.create(user=user, name=name)

                otp = generate_otp(email)

                send_email_async(
                    subject="Goatza OTP Verification",
                    message=f"Hello {name},\n\nYour OTP is: {otp}\nValid for 10 minutes.",
                    to_email=email
                )

                logger.info(f"User signup initiated: {email}")

                return response_data(
                    True,
                    "OTP sent to email",
                    {
                        "email": email,
                        "verification_required": True
                    }
                )

        except IntegrityError:
            logger.warning(f"Duplicate signup attempt: {email}")
            return response_data(False, "User already exists", status_code=400)

        except Exception as e:
            logger.error(f"Signup error: {str(e)}")
            return response_data(False, "Server error", status_code=500)
        

class VerifySignupOTPAPIView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [OTPThrottle]

    def post(self, request):
        email = request.data.get("email")
        otp_input = request.data.get("otp")

        print(otp_input)

        if not email or not otp_input:
            return response_data(False, "Email and OTP required", status_code=400)

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return response_data(False, "User not found", status_code=404)

        if not verify_otp(email, otp_input):
            logger.warning(f"Invalid OTP for {email}")
            return response_data(False, "Invalid or expired OTP", status_code=400)

        user.is_email_verified = True
        user.is_active = True
        user.save()

        refresh = RefreshToken.for_user(user)

        logger.info(f"User verified: {email}")

        response = response_data(
            True,
            "verification successful",
            {
                "access": str(refresh.access_token),
                "user": UserSerializer(user).data
            }
        )
        # Set refresh token in cookie
        set_refresh_key_cookie(response, refresh_token=refresh)
        return response 


class UserLoginAPIView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request):
        email = request.data.get("email")
        password = request.data.get("password")

        if not email or not password:
            return response_data(
                success=False,
                message="Email and password are required",
                status_code=400
            )

        # Authenticate user
        user = authenticate(request, email=email, password=password)
        if user is None:
            return response_data(
                success=False,
                message="Invalid email or password",
                status_code=401
            )
        
        if not user.is_email_verified:
            # Generate OTP
            otp = generate_otp(email)

            # Send email
            send_email_async(
                subject="Your OTP for GOATZA",
                message=f"Hello {user.profile_name},\n\nYour OTP is: {otp}\nIt is valid for 10 minutes.",
                to_email=email
            )

            return response_data(
                success=True,
                message="Login success please verify the email",
                data={
                    "email": user.email,
                    "verification_required" : True
                },
                status_code=200
            )
            
        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)

        # Prepare response data
        user_data = UserSerializer(user).data
                
        response = response_data(
            success=True,
            message="Login successful",
            data={
                "access": access_token,
                "user": user_data
            },
            status_code=200
        )

        # Set refresh token in cookie
        set_refresh_key_cookie(response, refresh_token=refresh)

        return response
        
        
class ForgotPasswordAPIView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ForgotPasswordThrottle]

    def post(self, request):
        email = request.data.get("email")

        if not email:
            return response_data(
                success=False,
                message="Email is required",
                status_code=400
            )

        try:
            user = User.objects.get(email=email)
        except Exception as e:
            return response_data(
                success=False,
                message="No user found with this email, please register",
                status_code=404
            )

        # Generate OTP
        otp = generate_otp(email)

        # Send OTP via email
        send_email_async(
            subject="Password Reset OTP - LearningMate AI",
            message=f"Hello {user.profile_name},\n\nYour OTP to reset your password is: {otp}\nIt is valid for 10 minutes.",
            to_email=email
        )
        
        return response_data(
            success=True,
            message="OTP has been sent to your email.",
            data={"email": email},
            status_code=200
        )


class ResetPasswordAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email")
        otp_input = request.data.get("otp")
        new_password = request.data.get("new_password")

        if not email or not otp_input or not new_password:
            return response_data(
                success=False,
                message="Email, OTP, and new password are required",
                status_code=400
            )

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return response_data(
                success=False,
                message="No user found with this email",
                status_code=404
            )

        # Verify OTP
        if not verify_otp(email, otp_input):
            return response_data(
                success=False,
                message="Invalid or expired OTP",
                status_code=400
            )
            
        if not is_valid_password(new_password):
            return response_data(
                success=False,
                message="Invalid password, should be at least 6 characters",
                status_code=400
            )

        # Update password
        user.password = make_password(new_password)
        user.save()

        return response_data(
            success=True,
            message="Password has been reset successfully. Please login with your new password.",
            status_code=200
        )


class ChangePasswordAPIView(APIView):
    """
    Change the password of the logged-in user.

    Every other session is killed (all outstanding refresh tokens blacklisted),
    then THIS device is handed a brand-new session so it stays logged in.
    Error contract (frontend maps these codes to messages):
      400 {"code": "missing_fields"}
      400 {"code": "invalid_current_password"}
      400 {"code": "invalid_new_password"}
      400 {"code": "same_password"}
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ChangePasswordThrottle]

    def post(self, request):
        user = request.user
        current_password = request.data.get("current_password")
        new_password = request.data.get("new_password")

        if not current_password or not new_password:
            return response_data(
                success=False,
                message="Current and new password are required",
                data={"code": "missing_fields"},
                status_code=400,
            )

        if not user.check_password(current_password):
            return response_data(
                success=False,
                message="Current password is incorrect",
                data={"code": "invalid_current_password"},
                status_code=400,
            )

        if not is_valid_password(new_password):
            return response_data(
                success=False,
                message="Password must be at least 6 characters",
                data={"code": "invalid_new_password"},
                status_code=400,
            )

        if new_password == current_password:
            return response_data(
                success=False,
                message="New password must be different from the current one",
                data={"code": "same_password"},
                status_code=400,
            )

        user.set_password(new_password)
        user.save()

        # Kill every existing session (other devices die on their next refresh).
        # Must happen BEFORE minting the new token, or we'd blacklist it too.
        for token in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=token)

        # ...then keep THIS device signed in on a fresh session.
        new_refresh = RefreshToken.for_user(user)

        logger.info(f"Password changed: {user.email}")

        response = response_data(
            success=True,
            message="Password changed successfully",
            data={"access_token": str(new_refresh.access_token)},
            status_code=200,
        )
        set_refresh_key_cookie(response, new_refresh)
        return response


class TokenRefreshAPIView(APIView):
    """
    Rotating refresh:
      - verifies the refresh cookie (signature, expiry, blacklist)
      - blacklists the old token, issues a NEW 30-day refresh token
        (sliding session) + a new 15-min access token
      - stores {old_jti -> new pair} in cache for 60s so an immediate replay
        of the just-rotated token (multi-tab race, lost Set-Cookie on mobile)
        gets the SAME new pair back instead of a logout.
    Error contract (frontend depends on it):
      401 {"code": "refresh_missing"} - no cookie
      401 {"code": "refresh_invalid"} - definitively dead session
    """
    permission_classes = [AllowAny]

    def post(self, request):
        raw = request.COOKIES.get("refresh_token")
        if not raw:
            return response_data(
                success=False,
                message="Refresh token not found",
                data={"code": "refresh_missing"},
                status_code=401,
            )

        try:
            refresh = RefreshToken(raw)  # verifies signature, expiry, blacklist
        except TokenError:
            replay = self._grace_replay(raw)
            if replay is not None:
                return replay
            return response_data(
                success=False,
                message="Invalid or expired refresh token",
                data={"code": "refresh_invalid"},
                status_code=401,
            )

        User = get_user_model()
        user = User.objects.filter(id=refresh["user_id"], is_active=True).first()
        if user is None:
            return response_data(
                success=False,
                message="Invalid or expired refresh token",
                data={"code": "refresh_invalid"},
                status_code=401,
            )

        old_jti = refresh["jti"]

        # Rotate: retire the presented token, mint a fresh 30-day one.
        refresh.blacklist()
        new_refresh = RefreshToken.for_user(user)
        access_token = str(new_refresh.access_token)

        # Grace window: allow an immediate replay of the old token to succeed
        # idempotently. Slightly relaxes strict reuse-detection for 60s —
        # an accepted trade-off for reliability on flaky mobile networks.
        cache.set(
            GRACE_CACHE_KEY.format(jti=old_jti),
            {"access": access_token, "refresh": str(new_refresh)},
            timeout=GRACE_TTL_SECONDS,
        )

        response = response_data(
            success=True,
            message="Access token refreshed successfully",
            data={"access_token": access_token},
            status_code=200,
        )
        set_refresh_key_cookie(response, new_refresh)  # restart 30-day clock
        return response

    def _grace_replay(self, raw):
        jti = self._unverified_jti(raw)
        if not jti:
            return None
        cached = cache.get(GRACE_CACHE_KEY.format(jti=jti))
        if not cached:
            return None
        response = response_data(
            success=True,
            message="Access token refreshed successfully",
            data={"access_token": cached["access"]},
            status_code=200,
        )
        set_refresh_key_cookie(response, cached["refresh"])  # re-send same new cookie
        return response

    @staticmethod
    def _unverified_jti(raw):
        # Signature already failed/blacklisted upstream; we only need jti to
        # look up the grace entry. Never trust any other claim from this.
        try:
            return pyjwt.decode(raw, options={"verify_signature": False}).get("jti")
        except Exception:
            return None


class UserLogoutAPIView(APIView):
    permission_classes = [AllowAny]  # was IsAuthenticated — logout must work
                                     # even when the access token is expired

    def post(self, request):
        raw = request.COOKIES.get("refresh_token")
        if raw:
            try:
                RefreshToken(raw).blacklist()
            except TokenError:
                pass  # already expired/blacklisted — nothing to do

        response = response_data(
            success=True,
            message="Logout successful",
            status_code=200,
        )
        delete_refresh_key_cookie(response)
        return response
