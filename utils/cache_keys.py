class CacheKeys:
    @staticmethod
    def user_details(username, list_type="mini"):
        return f"user:{username}:list_type:{list_type}"

    @staticmethod
    def email_otp(email):
        return f"otp:{email}"

    @staticmethod
    def google_state(state):
        return f"google:state:{state}"
    

    @staticmethod
    def sports_list():
        return f"sports:list"

    # ── Public (logged-out) profile bundles ──────────────────────
    # Keyed by username, not id: that is what the URL carries, and it is the
    # only thing the anonymous request knows. A rename therefore leaves a stale
    # entry under the old key — harmless, since nothing resolves to it any more
    # and it expires within the minute.

    @staticmethod
    def public_user_profile(username):
        return f"public:profile:user:{username}"

    @staticmethod
    def public_org_profile(username):
        return f"public:profile:org:{username}"