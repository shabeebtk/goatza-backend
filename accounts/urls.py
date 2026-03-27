from django.urls import path
from accounts.views.user_auth_views import (
    UserSignupAPIView,
    VerifySignupOTPAPIView,
    UserLoginAPIView,
    ForgotPasswordAPIView,
    ResetPasswordAPIView,
    TokenRefreshAPIView,
    UserLogoutAPIView
)
from accounts.views.user_google_auth_views import (
    GoogleLoginUrlView,
    GoogleAuthCallbackView
)
from accounts.views.user_views import GetUserDetails

# base url - /user/

urlpatterns = [
    path('signup', UserSignupAPIView.as_view()),
    path('verify/otp', VerifySignupOTPAPIView.as_view()),
    path('login', UserLoginAPIView.as_view()),
    path('forgot/password', ForgotPasswordAPIView.as_view()),
    path('reset/password', ResetPasswordAPIView.as_view()),
    path('token/refresh', TokenRefreshAPIView.as_view()),
    path('logout', UserLogoutAPIView.as_view()),
    
    # google auth 
    path('auth/google/login/url', GoogleLoginUrlView.as_view()),
    path('auth/google/callback', GoogleAuthCallbackView.as_view()),
    
    # user details 
    path('details', GetUserDetails.as_view())
]