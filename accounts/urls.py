from django.urls import path
from accounts.views.user_auth_views import (
    UserSignupAPIView,
    VerifySignupOTPAPIView,
    ResendSignupOTPAPIView,
    UserLoginAPIView,
    ForgotPasswordAPIView,
    ResetPasswordAPIView,
    ChangePasswordAPIView,
    TokenRefreshAPIView,
    UserLogoutAPIView
)
from accounts.views.user_google_auth_views import (
    GoogleLoginUrlView,
    GoogleAuthCallbackView
)
from accounts.views.user_views import (
    GetUserDetails, GetUserDetailsByID, UpdateUserMediaAPIView, UpdateUserProfileAPIView,
    CheckUsernameAvailabilityAPIView, SetUserRoleAPIView, CompleteOnboardingAPIView
)
from accounts.views.account_deletion_views import (
    AccountDeleteInitiateAPIView,
    AccountDeleteConfirmAPIView
)
from accounts.views.email_change_views import (
    EmailChangeInitiateAPIView,
    EmailChangeConfirmAPIView
)
from accounts.views.phone_change_views import PhoneChangeAPIView
from accounts.views.user_privacy_views import UserPublicProfilePrivacyAPIView
from accounts.views.user_upload_signature_views import GetUploadConfigAPIView
# base url - /user/

urlpatterns = [
    path('signup', UserSignupAPIView.as_view()),
    path('verify/otp', VerifySignupOTPAPIView.as_view()),
    # Beside verify/otp because it is the same step of the same flow — the
    # code this sends is the one that route spends (same no-purpose OTP key).
    path('resend/otp', ResendSignupOTPAPIView.as_view()),
    path('login', UserLoginAPIView.as_view()),
    path('forgot/password', ForgotPasswordAPIView.as_view()),
    path('reset/password', ResetPasswordAPIView.as_view()),
    path('change/password', ChangePasswordAPIView.as_view()),
    path('token/refresh', TokenRefreshAPIView.as_view()),
    path('logout', UserLogoutAPIView.as_view()),
    
    # google auth 
    path('auth/google/login/url', GoogleLoginUrlView.as_view()),
    path('auth/google/callback', GoogleAuthCallbackView.as_view()),
    
    # user details 
    path('check/username/availability', CheckUsernameAvailabilityAPIView.as_view()),
    path('<str:username>/details', GetUserDetails.as_view()),
    path('details', GetUserDetailsByID.as_view()),
    path('update/profile/cover', UpdateUserMediaAPIView.as_view()),
    path('update/profile/data', UpdateUserProfileAPIView.as_view()),
    path('role', SetUserRoleAPIView.as_view()),
    path('onboarding/complete', CompleteOnboardingAPIView.as_view()),

    # privacy
    path('privacy/public-profile', UserPublicProfilePrivacyAPIView.as_view()),

    # account deletion (30-day purge)
    #
    # ABOVE '<str:username>/details' would not matter — these are two segments
    # deep and that route is one — but they are grouped here with the other
    # account-lifecycle writes rather than with the by-handle reads.
    path('account/delete/initiate', AccountDeleteInitiateAPIView.as_view()),
    path('account/delete/confirm', AccountDeleteConfirmAPIView.as_view()),

    # changing the sign-in email — password gated, then an OTP to the NEW
    # address. Beside deletion because they are the same kind of write: the two
    # things a user can do that decide who owns this account.
    path('email/change/initiate', EmailChangeInitiateAPIView.as_view()),
    path('email/change/confirm', EmailChangeConfirmAPIView.as_view()),

    # phone is a contact detail, not a credential — one call, no verification
    # (see accounts/services/phone_change_service.py).
    path('phone/change', PhoneChangeAPIView.as_view()),

    # user upload media signature
    path('get/upload/signature', GetUploadConfigAPIView.as_view()),
]