import random
import string
import logging
from django.contrib.auth import authenticate
from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from rest_framework.views import APIView
from accounts.models import (
    User, UserProfile
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken, TokenError
from accounts.serializers.user_serializers import UserSerializer
from accounts.services.user_services import generate_unique_username
from utils.response import response_data
from utils.validations import is_valid_email, is_valid_password
from utils.otp_validation import generate_otp, verify_otp
from utils.emails import send_email
from accounts.throttles import (
    SignupThrottle, LoginThrottle, OTPThrottle, ForgotPasswordThrottle
)

logger = logging.getLogger(__name__)

# Views here 
class UserSignupAPIView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [SignupThrottle]

    def post(self, request):
        email = request.data.get("email")
        password = request.data.get("password")

        data = generate_otp(email)
        print(data)

        if not email or not password:
            return response_data(False, "Email and password are required", status_code=400)

        if not is_valid_email(email):
            return response_data(False, "Invalid email", status_code=400)

        if not is_valid_password(password):
            return response_data(False, "Password must be at least 6 characters", status_code=400)

        name = email.split("@")[0]
        base_username = name
        username = base_username

        username = generate_unique_username(base=email)

        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    email=email,
                    username=username,
                    password=password
                )

                UserProfile.objects.create(user=user, name=name)

                otp = generate_otp(email)

                email_sent = send_email(
                    subject="Goatza OTP Verification",
                    message=f"Hello {name},\n\nYour OTP is: {otp}\nValid for 10 minutes.",
                    to_email=email
                )

                if not email_sent:
                    logger.error(f"Email sending failed for {email}")
                    raise Exception("Failed to send OTP")

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
        user.save()

        refresh = RefreshToken.for_user(user)

        logger.info(f"User verified: {email}")

        return response_data(
            True,
            "Signup successful",
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": UserSerializer(user).data
            }
        )


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
            email_sent = send_email(
                subject="Your OTP for GOATZA",
                message=f"Hello {user.profile_name},\n\nYour OTP is: {otp}\nIt is valid for 10 minutes.",
                to_email=email
            )
            
            if not email_sent:
                # Rollback user creation
                raise Exception("Failed to login user")

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

        return response_data(
            success=True,
            message="Login successful",
            data={
                "access": access_token,
                "refresh": str(refresh),
                "user": user_data
            },
            status_code=200
        )
        
        
        
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
        email_sent = send_email(
            subject="Password Reset OTP - LearningMate AI",
            message=f"Hello {user.profile_name},\n\nYour OTP to reset your password is: {otp}\nIt is valid for 10 minutes.",
            to_email=email
        )

        if not email_sent:
            return response_data(
                success=False,
                message="Failed to send OTP. Please try again later.",
                status_code=500
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




class TokenRefreshAPIView(APIView):
    """
    API view to refresh JWT access token using a valid refresh token.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        refresh_token = request.data.get("refresh_token")

        if not refresh_token:
            return response_data(
                success=False,
                message="Refresh token is required",
                status_code=400
            )

        try:
            # Attempt to create a new access token from the refresh token
            refresh = RefreshToken(refresh_token)
            access_token = str(refresh.access_token)

            return response_data(
                success=True,
                message="Access token refreshed successfully",
                data={"access_token": access_token},
                status_code=200
            )

        except TokenError as e:
            return response_data(
                success=False,
                message="Invalid or expired refresh token",
                error=str(e),
                status_code=401
            )
            
            

class UserLogoutAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            refresh_token = request.data["refresh_token"]
            token = RefreshToken(refresh_token)
            token.blacklist()
            return response_data(
                success=True,
                message="Logout successful",
                status_code=200
            )
        except Exception as e:
            return response_data(
                success=False,
                message=f"Invalid token {str(e)}",
                status_code=400
            )
        
