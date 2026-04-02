from django.core.cache import cache
import random
from utils.cache import cache_set, cache_delete, cache_get
from utils.cache_keys import CacheKeys

OTP_EXPIRE_MINUTES = 10  # OTP valid for 10 minutes

def generate_otp(email: str) -> str:
    """Generate and store OTP in cache"""
    otp = str(random.randint(1001, 9999))
    key = CacheKeys.email_otp(email)
    cache_set(key, otp, timeout=OTP_EXPIRE_MINUTES * 60)
    return otp

def verify_otp(email: str, otp_input: str) -> bool:
    """Check OTP validity"""
    key = CacheKeys.email_otp(email)
    otp = cache_get(key)
    if otp and otp == otp_input:
        cache_delete(key) # invalidate after successful verification
        return True
    
    return False
