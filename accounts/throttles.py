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


class EmailChangeThrottle(ScopedRateThrottle):
    """
    5/hour across BOTH email-change endpoints (``email_change`` in
    DEFAULT_THROTTLE_RATES).

    ScopedRateThrottle for the same reason AccountDeleteThrottle is one: the
    scope is declared by the VIEW (``throttle_scope = "email_change"`` on both)
    and the two endpoints share ONE budget, because initiate and confirm are
    halves of a single action.

    Tighter than it looks like it needs to be, for two separate reasons.
    Initiate MAILS A CODE TO AN ADDRESS THE CALLER TYPED — an endpoint that
    sends attacker-chosen mail from Goatza's domain is a spam relay if it is
    cheap. And confirm takes a 4-digit code (utils/otp_validation.py), so the
    rate limit is what stands between a stolen access token and guessing it.

    Keyed on the user, not the actor — an email address belongs to a person,
    and no org header should buy a second allowance for moving it.
    """

    scope = "email_change"


class PhoneChangeThrottle(ScopedRateThrottle):
    """
    10/hour on ``POST /user/phone/change`` (``phone_change`` in
    DEFAULT_THROTTLE_RATES).

    Looser than its email twin because it neither sends mail nor guards a
    secret: phone is not a login identifier and nothing is verified today. What
    it does guard is the ``unique=True`` column — without a limit this endpoint
    would answer "already taken" fast enough to enumerate which numbers have
    Goatza accounts. Modest rather than tight: setting and correcting a number
    in one sitting is normal, doing it ten times an hour is not.
    """

    scope = "phone_change"
