from django.core.cache import cache
import random
from utils.cache import cache_set, cache_delete, cache_get
from utils.cache_keys import CacheKeys

OTP_EXPIRE_MINUTES = 10  # OTP valid for 10 minutes

def generate_otp(email: str, purpose: str = None) -> str:
    """
    Generate and store OTP in cache.

    ``purpose`` is optional. Omitted (signup, login verification, forgot
    password) it keeps the shared per-address key those three have always used.
    Passed, the code lands under its own key and can only be spent by a flow
    asking for the same purpose — see CacheKeys.email_otp.
    """
    otp = str(random.randint(1001, 9999))
    key = CacheKeys.email_otp(email, purpose)
    cache_set(key, otp, timeout=OTP_EXPIRE_MINUTES * 60)
    return otp

def verify_otp(email: str, otp_input: str, purpose: str = None) -> bool:
    """Check OTP validity. ``purpose`` must match the one it was issued under."""
    key = CacheKeys.email_otp(email, purpose)
    otp = cache_get(key)
    if otp and otp == otp_input:
        cache_delete(key) # invalidate after successful verification
        return True
    
    return False
