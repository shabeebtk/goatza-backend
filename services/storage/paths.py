"""
Object-path scheme, shared by every storage provider.

Every object key in the bucket is built here, and nowhere else. A media path
that drifts is unrecoverable: it silently orphans everything already uploaded
under the old shape, and it breaks the ownership prefix checks in
services/storage/validators.py (validate_public_id) that assume
``users/<id>/`` / ``organizations/<id>/``.

R2Service joins the folder and the object name into a single key.
"""

import uuid

# Types that own exactly ONE slot per actor and replace themselves on re-upload
# Their folder already ends with the slot name, so there is no random
# component — re-uploading lands on the same path and overwrites in place.
FIXED_SLOT_TYPES = {
    "profile",
    "cover",
    "organization_logo",
    "organization_cover",
}

# Types whose upload is a BATCH that belongs to a not-yet-created row. The
# client is handed a temp id up front, uploads every file under it, then posts
# the temp id back with the create request.
TEMP_BATCH_TYPES = {
    "posts",
    "recruitments",
}


def new_temp_id() -> str:
    """The batch id handed to the client for posts/recruitments."""
    return str(uuid.uuid4())


def build_folder(actor, upload_type: str, temp_id: str = None) -> str:
    """
    The folder an upload of `upload_type` lands in for `actor`.

    `temp_id` is required for TEMP_BATCH_TYPES and ignored otherwise.
    """

    # -----------------------------------------
    # USER PROFILE / COVER
    # -----------------------------------------
    if upload_type in {"profile", "cover"}:
        return f"users/{actor.user.id}/{upload_type}"

    # -----------------------------------------
    # ORGANIZATION LOGO / COVER
    # -----------------------------------------
    # The slot name is NOT the upload_type here (organization_logo -> logo), so
    # unlike the user side these cannot be built from upload_type directly.
    if upload_type == "organization_logo":
        return f"organizations/{actor.organization.id}/logo"

    if upload_type == "organization_cover":
        return f"organizations/{actor.organization.id}/cover"

    # -----------------------------------------
    # POSTS  (user or org)
    # -----------------------------------------
    if upload_type == "posts":
        if actor.is_user:
            return f"users/{actor.user.id}/posts/{temp_id}"
        if actor.is_org:
            return f"organizations/{actor.organization.id}/posts/{temp_id}"
        raise ValueError("Invalid actor for posts upload")

    # -----------------------------------------
    # RECRUITMENTS  (org only)
    # -----------------------------------------
    if upload_type == "recruitments":
        return f"organizations/{actor.organization.id}/recruitments/{temp_id}"

    # -----------------------------------------
    # CHAT MEDIA  (user or org — direct messages)
    # -----------------------------------------
    # The per-actor subfolder lets the message service re-validate that a media
    # URL was uploaded by the SENDER (not replayed from someone else's folder)
    # before it trusts it — see MessageService._validate_chat_image.
    if upload_type == "chat":
        if actor.is_user:
            return f"chat/users/{actor.user.id}"
        if actor.is_org:
            return f"chat/organizations/{actor.organization.id}"
        raise ValueError("Invalid actor for chat upload")

    # -----------------------------------------
    # PROBLEM REPORT SCREENSHOTS  (user or org)
    # -----------------------------------------
    # Same per-actor shape as chat above, for the same reason: the create
    # endpoint re-validates that a submitted key sits under the CALLER's own
    # prefix before trusting it, and that check only means something if the
    # folder is scoped per actor. Here it lands under the standard
    # ``users/<id>/`` / ``organizations/<id>/`` roots, which is what
    # services/storage/validators.py (validate_public_id) already checks.
    if upload_type == "support":
        if actor.is_user:
            return f"users/{actor.user.id}/support"
        if actor.is_org:
            return f"organizations/{actor.organization.id}/support"
        raise ValueError("Invalid actor for support upload")

    # -----------------------------------------
    # ACHIEVEMENTS · MATCHES · HIGHLIGHTS  (user only)
    # -----------------------------------------
    # One file per award / match / clip, so a random name per upload — unlike
    # profile/cover, which each own a single fixed slot and are meant to replace
    # themselves. A user can hold 20 achievements and a season of matches;
    # replacing one image must never clobber another's.
    #
    # The folder is built from upload_type rather than hardcoded, so the next
    # person-owned media type is one entry in the policy and nothing here.
    if upload_type in {"achievements", "matches", "highlights"}:
        return f"users/{actor.user.id}/{upload_type}"

    raise ValueError("Invalid upload type")


def build_object_name(upload_type: str) -> str:
    """
    The name inside the folder. Fixed-slot types reuse the slot name (which is
    also the last folder segment, so an R2 key collapses to just the folder);
    everything else gets a fresh uuid per file.
    """
    if upload_type == "profile":
        return "profile"
    if upload_type == "cover":
        return "cover"
    if upload_type == "organization_logo":
        return "logo"
    if upload_type == "organization_cover":
        return "cover"
    return str(uuid.uuid4())


def is_canonical_uuid(value) -> bool:
    """
    True only for the exact spelling ``str(uuid.UUID(...))`` produces —
    lowercase, hyphenated, no braces, no ``urn:uuid:`` prefix.

    A batch id handed back by a client has to be spelled the way we minted it:
    ``uuid.UUID`` alone accepts five spellings of one id, and a folder that can
    be named five ways is five folders.
    """
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def actor_key_prefixes(actor) -> tuple:
    """
    Every prefix an object this actor uploaded can start with: their own root
    and their own chat folder, which sits OUTSIDE that root (see
    ``build_folder``). For a check that does not know the upload type — the
    multipart complete/abort endpoints.
    """
    if actor.is_org:
        org_id = actor.organization.id
        return (f"organizations/{org_id}/", f"chat/organizations/{org_id}/")
    user_id = actor.user.id
    return (f"users/{user_id}/", f"chat/users/{user_id}/")


def is_upload_key_for(actor, upload_type: str, key) -> bool:
    """
    Could the signature endpoint have handed THIS actor this key for
    ``upload_type``? The inverse of ``build_folder`` + ``build_object_name``.

    The multipart endpoints take a key BACK from the client — the one the
    signature endpoint issued for the video's batch, reused so the video lands
    in the folder its poster is in. The plain ``users/<id>/`` prefix check
    (``validate_public_id``) is not enough there, for two reasons: chat keys
    live under ``chat/…``, outside that root, and a per-type size cap only
    means something if a key claimed as a ``posts`` upload really sits in a
    posts folder — a highlights key would otherwise borrow the posts cap.

    So: the type's own folder for this actor, then — for batch types — one
    canonical batch id, then a single ``<uuid>.<ext>`` file name. Fixed-slot
    types never qualify; nothing uploads a profile picture in parts.
    """
    if not isinstance(key, str) or not key or upload_type in FIXED_SLOT_TYPES:
        return False

    try:
        if upload_type in TEMP_BATCH_TYPES:
            # An empty batch id builds the batch folder's PARENT, ending in
            # "/" — the batch id is the next segment.
            parent = build_folder(actor, upload_type, "")
            if not key.startswith(parent):
                return False
            batch_id, slash, name = key[len(parent):].partition("/")
            if not slash or not is_canonical_uuid(batch_id):
                return False
        else:
            folder = build_folder(actor, upload_type) + "/"
            if not key.startswith(folder):
                return False
            name = key[len(folder):]
    except (ValueError, AttributeError):
        # An actor/type pairing build_folder has no folder for (an org asking
        # for a user-only type). The views refuse those before they get here,
        # so this is a bug path, and the only safe answer is no.
        return False

    stem, dot, extension = name.partition(".")
    return (
        bool(dot)
        and bool(extension)
        and "/" not in name
        and is_canonical_uuid(stem)
    )
