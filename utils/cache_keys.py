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

    # ── Handle resolution ────────────────────────────────────────

    @staticmethod
    def profile_lookup(username):
        """
        Handle -> {"type", "id"}, the hot path behind /[username] and every
        by-handle endpoint (UsernameService.resolve).

        UNLIKE the public bundles below, this one is NOT allowed to go stale:
        it is an identity mapping, and a freed handle that keeps resolving to
        its old owner for five minutes sends the next visitor to the wrong
        profile. UsernameService.claim/release delete it for the old AND the
        new handle on every rename.
        """
        return f"profile_lookup:{username}"

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

    @staticmethod
    def public_cv(username):
        """
        The anonymous rendering of a player's Sports CV.

        A SEPARATE key from public_user_profile even though the two overlap:
        the CV's contents depend on the CV's own toggles, so one key could not
        be invalidated correctly by either writer. Both are cleared when
        is_public_profile goes off — the CV is gated on it (see get_cv_user),
        and a hidden profile that keeps serving a cached CV for a minute is the
        toggle silently not working.
        """
        return f"public:cv:{username}"

    @staticmethod
    def cv_view_counted(username, ident):
        """
        Marker that this caller has already been counted against a CV's
        views_count. Short-lived by design — the counter is a rough "how much
        interest is this getting", not analytics, and a refresh loop must not
        be able to inflate it.
        """
        return f"cv:viewed:{username}:{ident}"

    # ── Pre-launch waitlist ──────────────────────────────────────

    @staticmethod
    def waitlist_signup_count():
        """
        How many players have joined the waitlist.

        One global key, not per-anything: the counter is the same number for
        every visitor, and it is the headline on the landing page, so it is
        read on every hit and written once a signup. The selector busts it on
        create rather than relying on the TTL — "you're #413" on the success
        screen followed by "412 joined" on the same page reads as broken.
        """
        return "waitlist:signup:count"
