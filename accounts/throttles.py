from rest_framework.throttling import UserRateThrottle

class SignupThrottle(UserRateThrottle):
    scope = 'signup'

class LoginThrottle(UserRateThrottle):
    scope = 'login'

class OTPThrottle(UserRateThrottle):
    scope = 'otp'

class ForgotPasswordThrottle(UserRateThrottle):
    scope = 'forgot_password'
