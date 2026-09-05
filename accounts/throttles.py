from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle

class SignupThrottle(UserRateThrottle):
    scope = 'signup'

class LoginThrottle(UserRateThrottle):
    scope = 'login'

class OTPThrottle(UserRateThrottle):
    scope = 'otp'

class ForgotPasswordThrottle(UserRateThrottle):
    scope = 'forgot_password'

class ChangePasswordThrottle(UserRateThrottle):
    scope = 'change_password'


class AccountDeleteThrottle(ScopedRateThrottle):
    """
    3/hour across BOTH account-deletion endpoints (``account_delete`` in
    DEFAULT_THROTTLE_RATES).

    ScopedRateThrottle rather than a UserRateThrottle subclass like its
    neighbours above, because the scope is declared by the VIEW
    (``throttle_scope = "account_delete"`` on both) instead of by the class.
    Two endpoints, one budget: initiate and confirm are halves of a single
    action, and a limit that let each run three times an hour would be a limit
    of six on the thing that actually matters.

    Keyed on the user, not the actor — an account belongs to a person, and no
    org header should buy a second allowance for deleting it.

    Deliberately tight. The honest flow is two calls, and the cost of tripping
    this is an hour's wait on something nobody does twice; the cost of leaving
    it loose is an attacker with a stolen access token brute-forcing the
    password confirm.
    """

    scope = "account_delete"
