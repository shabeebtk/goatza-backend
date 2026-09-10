class CacheKeys:
    @staticmethod
    def user_details(username, list_type="mini"):
        return f"user:{username}:list_type:{list_type}"

    @staticmethod
    def email_otp(email, purpose=None):
        """
        The one-time code sent to an address.

        ``purpose`` is optional and DEFAULTS TO THE ORIGINAL KEY, so signup,
        login-verification and forgot-password keep the exact key they have
        always used and keep sharing one code per address — changing that would
        break a code already in flight at deploy time.

        A purpose-scoped key exists for flows where a code must not be
        interchangeable with those. Account deletion is the first: without it a
        forgot-password code, which arrives by mail for the asking, would also
        confirm the deletion of a Google-only account.
        """
        if purpose:
            return f"otp:{purpose}:{email}"
        return f"otp:{email}"

    @staticmethod
    def otp_resend_cooldown(email):
        """
        The 30-second gap between two "send me the code again" requests for one
        address (POST /user/resend/otp).

        SEPARATE from the throttle on that view, and not a duplicate of it. The
        throttle is keyed on the CALLER — one IP, one budget across every
        address it types — and it is there to stop abuse. This is keyed on the
        ADDRESS, and it is there so an inbox cannot be made to receive a code
        every second by a client that keeps asking, however many callers are
        asking.

        Written on EVERY accepted request, not only on the ones that actually
        send. The endpoint answers the same generic success whether or not the
        address has an unverified account behind it, and a cooldown set only on
        the real sends would undo that in one extra call: 200 then 200 means no
        account, 200 then 429 means there is one.

        The value stored is the unix timestamp the cooldown lifts at, so the
        429 can say how many seconds are left rather than just "later" — the
        TTL itself is not readable back out of the cache API.
        """
        return f"otp:resend:cooldown:{email}"

    @staticmethod
    def email_change_pending(user_id):
        """
        The address a user is part-way through moving to.

        SERVER-SIDE BINDING, and that is the whole reason it exists: the
        confirm step takes only a code, never an address. Without this key the
        client would have to hand the new email back, and a caller who could
        change what it sent between the two steps would be able to spend a code
        proved against one inbox on a different one.

        Written by initiate with the OTP's own TTL so the two die together, and
        deleted the moment the change lands.
        """
        return f"email_change:pending:{user_id}"

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
    def public_sitemap_urls():
        """
        The whole /public/sitemap/urls payload under ONE key.

        Not keyed by anything: the response has no inputs. Deliberately NOT
        invalidated when a profile is hidden either — the sitemap's only reader
        is a crawler-facing build step, the entry expires within the hour, and
        a URL that appears in a sitemap it should not be in still 404s when the
        crawler follows it (the page itself is the enforcement point, see
        get_public_user). Adding a bust to every profile-visibility write would
        buy an hour of tidiness in a file nobody reads directly.
        """
        return "public:sitemap:urls"

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

    # ── Recruitments ─────────────────────────────────────────────

    @staticmethod
    def recruitment_view_counted(recruitment_id, ident):
        """
        Marker that this viewer has already been counted against a
        recruitment's ``views_count``.

        ``ident`` is the VIEWER, not the request: ``user:<id>`` /
        ``org:<id>`` for a signed-in caller, ``ip:<addr>`` for an anonymous
        one (see RecruitmentViewService.viewer_ident). Keying on the actor
        rather than the IP where we have one is what makes the two detail
        views — the authenticated one and the public one — agree that a
        person who opened the link logged out and then signed in is the same
        viewer, on the same posting.

        Same spirit as ``cv_view_counted``: the counter is a rough "how much
        interest is this getting" the owning org reads off its own dashboard,
        not analytics, so a refresh loop must count once and nothing here is
        worth a database row.
        """
        return f"recruitment:viewed:{recruitment_id}:{ident}"

    # ── Moderation ───────────────────────────────────────────────

    @staticmethod
    def blocked_ids(actor_type, actor_id):
        """
        The union of "identities this actor blocked" and "identities that
        blocked this actor" — the one set every read path filters against.

        Keyed by (type, id) rather than username because the actor here is
        resolved from a token, not from a URL: the id is what the request
        already has, and unlike a handle it never moves.

        SYMMETRIC by construction, so one block invalidates TWO keys — the
        blocker's and the blocked party's (BlockService.invalidate_blocked_ids
        is called for both on every write). A short TTL on top of that is a
        backstop, not the mechanism: a stale entry here means a blocked account
        keeps appearing in a feed, which is the one thing blocking promises not
        to do.
        """
        return f"blocked_ids:{actor_type}:{actor_id}"
