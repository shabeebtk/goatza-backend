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
