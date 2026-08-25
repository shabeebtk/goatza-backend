"""
Write path for the Match Diary.

Views stay thin: they hand ``request.actor`` and the payload straight in and
every rule is applied here — players only, acting as themselves, own entries
only, the sport ↔ position and sport ↔ stat_field integrity checks, and the
scheduled/played invariant the DB also holds.

The first argument is the resolved ``core.actor.Actor``, not a User. "Your own
diary" is not just an ownership check, it also means *acting as yourself*: a
person acting through one of their organizations is not logging their own
matches, and only the actor knows which it was.

Nothing here silently drops a field. A client that sends a result on a fixture,
a stat that belongs to another sport, or a visibility the API does not accept
yet gets a 400 naming the problem. Dropping it would leave the developer with a
diary entry that is quietly missing what they sent.

Failures raise DRF exceptions so the message reaches the client unchanged:

  * PermissionDenied (403) — not a player, acting as an org, not your match
  * NotFound (404)         — no such live match
  * ValidationError (400)  — everything else (bad ids, foreign or retired stat
    fields, a fixture carrying a result, a played match being un-played)
"""

import datetime
import logging
import uuid
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models.functions import ExtractIsoYear, ExtractWeek
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from accounts.models import User
from careers.models import CareerEntry
from matches.models import (
    MatchDiarySettings,
    MatchEntry,
    MatchEntryStat,
    SportMatchStatField,
)
from services.storage.factory import get_storage_service
from services.storage.validators import (
    allowed_image_extensions,
    validate_media,
)
from sports.models import Sport, SportPosition
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)


class MatchService:

    # DecimalField(max_digits=8, decimal_places=2). Enforced here so an absurd
    # number is a 400 naming the stat, not a numeric-overflow 500 from the DB.
    MAX_STAT_VALUE = Decimal("999999.99")

    # Bounds the backward walk in the streak calculation. Five years of weeks:
    # far beyond any real streak, and a hard stop if a corrupt date range ever
    # makes the set look continuous forever.
    MAX_STREAK_WEEKS = 260

    # =================================================================
    # GUARDS
    # =================================================================

    @staticmethod
    def require_player(actor) -> User:
        """
        The single write gate. Returns the owning user, or raises
        PermissionDenied (→ 403) when the actor may not manage a match diary:
        signed out, acting as an organization, or a non-player role.

        Public because the write views call it *before* validating the body:
        someone who may not write at all should get 403, not a critique of a
        payload that was never going to be accepted. Every write here calls it
        again anyway, so the rule still lives in exactly one place.

        A deliberate copy of ``HighlightService.require_player`` rather than an
        import, for the reason ``CVService`` gives: the rules read the same
        today but they are about different features, and the refusal message
        has to name the one the player is looking at.
        """
        if actor is None:
            raise PermissionDenied(
                "You must be signed in to manage your match diary."
            )

        if actor.is_org:
            raise PermissionDenied(
                "Organizations do not have a match diary. Switch to your "
                "personal account to manage yours."
            )

        user = actor.user if actor.is_user else None

        if user is None:
            raise PermissionDenied(
                "You must be signed in to manage your match diary."
            )

        if user.role != User.Role.PLAYER:
            raise PermissionDenied(
                f"Only players have a match diary. Your account role is "
                f"{user.get_role_display().lower()}."
            )

        return user

    @staticmethod
    def _get_owned_match(user, match_id) -> MatchEntry:
        """
        Load one live match and prove it belongs to ``user``. Missing, deleted
        and somebody else's are all 404 — deliberately indistinguishable.

        This is where the diary parts company with
        ``HighlightService._get_owned_highlight``, which answers 403 for
        another player's clip. A highlight is content other people are meant to
        see, so confirming one exists costs nothing. A diary is private, and a
        403 would tell whoever is walking ids that this one is real and belongs
        to someone — which is precisely what they were trying to learn.
        """
        if not is_valid_uuid(match_id):
            raise ValidationError(f"'{match_id}' is not a valid match id.")

        match = MatchEntry.objects.filter(
            id=match_id,
            user=user,
            is_deleted=False
        ).first()

        if match is None:
            raise NotFound("Match not found.")

        return match

    # =================================================================
    # FIELD RESOLUTION
    #
    # Each resolver accepts either a model instance or an id, so the shell and
    # any future internal caller can pass the object it already loaded while
    # the API passes what came off the wire.
    # =================================================================

    @staticmethod
    def _resolve_sport(value) -> Sport:
        """A match is always about one sport, and it has to be a real one."""
        if isinstance(value, Sport):
            return value

        if not value or not is_valid_uuid(value):
            raise ValidationError("A valid sport is required.")

        sport = Sport.objects.filter(id=value).first()

        if sport is None:
            raise ValidationError("That sport does not exist.")

        return sport

    @staticmethod
    def _resolve_position(sport, value):
        """
        The position played, when one was given. Proven to belong to the
        match's sport — a goalkeeper is not a cricket position, and attaching
        one would put a nonsense row in front of every recruiter who reads it.
        """
        if value in (None, ""):
            return None

        position = value if isinstance(value, SportPosition) else None

        if position is None:
            if not is_valid_uuid(value):
                raise ValidationError(
                    f"'{value}' is not a valid position id."
                )
            position = SportPosition.objects.filter(id=value).first()

        if position is None:
            raise ValidationError("That position no longer exists.")

        if position.sport_id != sport.id:
            raise ValidationError(
                f"{position.name} is not a {sport.name} position."
            )

        return position

    @staticmethod
    def _resolve_career_entry(user, value):
        """
        The club/academy stint this match belongs to, when one was given.

        Ownership is checked, not just existence: without this, a player could
        hang their matches off somebody else's career stint by guessing an id.
        A foreign id is a 400 rather than a 403 — it is a bad value in a body,
        and saying "that is not yours" would confirm the id exists.
        """
        if value in (None, ""):
            return None

        entry = value if isinstance(value, CareerEntry) else None

        if entry is None:
            if not is_valid_uuid(value):
                raise ValidationError(
                    f"'{value}' is not a valid career entry id."
                )
            entry = CareerEntry.objects.filter(id=value).first()

        if entry is None or entry.user_id != user.id:
            raise ValidationError(
                "That career entry is not one of yours."
            )

        return entry

    # =================================================================
    # FIELD CLEANING
    # =================================================================

    @staticmethod
    def _clean_choice(value, choices, label, default):
        """One of a TextChoices set, or the default when nothing was sent."""
        if value in (None, ""):
            return default

        if value not in choices.values:
            raise ValidationError(
                f"{label} must be one of: " + ", ".join(choices.values) + "."
            )

        return value

    @staticmethod
    def _clean_date(value):
        """The day the match was (or will be) played. Always required."""
        if value in (None, ""):
            raise ValidationError("A match date is required.")

        if isinstance(value, datetime.datetime):
            return value.date()

        if isinstance(value, datetime.date):
            return value

        parsed = parse_date(str(value))
        if parsed is None:
            raise ValidationError(
                f"'{value}' is not a valid date. Use YYYY-MM-DD."
            )

        return parsed

    @staticmethod
    def _clean_time(value):
        """Kick-off, when the player bothered to set one."""
        if value in (None, ""):
            return None

        if isinstance(value, datetime.time):
            return value

        parsed = parse_time(str(value))
        if parsed is None:
            raise ValidationError(
                f"'{value}' is not a valid time. Use HH:MM."
            )

        return parsed

    @staticmethod
    def _clean_text(value, field_name, label):
        """Trim a free-text field and hold it to its column width."""
        text = (value or "").strip()
        max_length = MatchEntry._meta.get_field(field_name).max_length

        if max_length and len(text) > max_length:
            raise ValidationError(
                f"{label} cannot be longer than {max_length} characters."
            )

        return text

    @staticmethod
    def _clean_minutes(value):
        """
        Minutes played. ``minutes_played`` is a PositiveIntegerField, which
        Postgres backs with its own check constraint — catching a negative here
        turns a 500 into a sentence the player can act on.
        """
        if value in (None, ""):
            return None

        try:
            minutes = int(value)
        except (TypeError, ValueError):
            raise ValidationError("Minutes played must be a whole number.")

        if minutes < 0:
            raise ValidationError("Minutes played cannot be negative.")

        return minutes

    @staticmethod
    def _clean_self_rating(value):
        """
        The player's own 1-5 mark. Mirrors the ``match_entry_valid_self_rating``
        check constraint, which would otherwise fail the insert with a 500.
        """
        if value in (None, ""):
            return None

        try:
            rating = int(value)
        except (TypeError, ValueError):
            raise ValidationError("Your rating must be a whole number from 1 to 5.")

        if rating < 1 or rating > 5:
            raise ValidationError("Your rating must be from 1 to 5.")

        return rating

    @staticmethod
    def _clean_photo(user, payload):
        """
        The optional match photo, as a ``(url, public_id)`` pair.

        Returns None when the payload said nothing about a photo, so an update
        can tell "leave it alone" from "clear it" (both keys sent empty).

        The two columns are meaningless apart — a URL with no public_id can
        never be cleaned up, and a public_id with no URL renders nothing — so
        one without the other is rejected rather than half-stored. The asset is
        then run through the shared ``validate_media``, which proves it is a
        stored file under this user's own ``users/<id>/`` prefix: without
        that, a player could attach anybody's uploaded image by pasting its URL.
        """
        if "photo_url" not in payload and "photo_public_id" not in payload:
            return None

        url = (payload.get("photo_url") or "").strip()
        public_id = (payload.get("photo_public_id") or "").strip()

        if not url and not public_id:
            return "", ""

        if not url or not public_id:
            raise ValidationError(
                "Send both photo_url and photo_public_id, or neither."
            )

        max_url = MatchEntry._meta.get_field("photo_url").max_length
        max_public_id = MatchEntry._meta.get_field("photo_public_id").max_length

        if len(url) > max_url:
            raise ValidationError(
                f"The photo URL cannot be longer than {max_url} characters."
            )

        if len(public_id) > max_public_id:
            raise ValidationError(
                f"The photo id cannot be longer than {max_public_id} characters."
            )

        try:
            validate_media(
                user=user,
                url=url,
                public_id=public_id,
                allowed_extensions=allowed_image_extensions(),
            )
        except ValueError as exc:
            raise ValidationError(f"Match photo: {exc}")

        return url, public_id

    @staticmethod
    def _reject_visibility(payload) -> None:
        """
        ``MatchEntry.visibility`` ships in the schema but is not part of the v1
        API (see the field's comment). A client sending it believes its diary
        can be shared; answering 201 and storing PRIVATE anyway would let that
        belief survive all the way to a player thinking their coach can read
        this.
        """
        if "visibility" in payload:
            raise ValidationError(
                "Match visibility cannot be set yet — every entry in your "
                "diary is private."
            )

    # =================================================================
    # STATS
    # =================================================================

    @staticmethod
    def _clean_stat_value(stat_field, raw, index) -> Decimal:
        """
        One stat's number, held to the catalog's own rules.

        ``value_type`` is what makes the shared DecimalField column safe: an
        INTEGER field rejects 2.5 here rather than storing it and letting the
        season total come out in half-goals.
        """
        if raw in (None, ""):
            raise ValidationError(
                f"stats[{index}]: {stat_field.name} needs a value."
            )

        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError(
                f"stats[{index}]: {stat_field.name} must be a number."
            )

        # Decimal("nan") and Decimal("inf") parse cleanly and would reach the DB.
        if not value.is_finite():
            raise ValidationError(
                f"stats[{index}]: {stat_field.name} must be a number."
            )

        if value < 0:
            raise ValidationError(
                f"stats[{index}]: {stat_field.name} cannot be negative."
            )

        if value > MatchService.MAX_STAT_VALUE:
            raise ValidationError(
                f"stats[{index}]: {stat_field.name} cannot be more than "
                f"{MatchService.MAX_STAT_VALUE}."
            )

        if stat_field.value_type == SportMatchStatField.ValueType.INTEGER:
            if value != value.to_integral_value():
                raise ValidationError(
                    f"stats[{index}]: {stat_field.name} must be a whole number."
                )

        return value.quantize(Decimal("0.01"))

    @staticmethod
    def _resolve_stats(sport, raw_stats) -> list:
        """
        Turn ``[{"stat_field": <id>, "value": <number>}, ...]`` into
        ``[(SportMatchStatField, Decimal), ...]``, proving every field is real,
        belongs to ``sport``, and is still being recorded.

        One query however many stats were sent.

        A repeated ``stat_field`` is a 400 rather than last-one-wins: the
        unique constraint means only one of them could survive anyway, and a
        client that sends "Goals: 1" and "Goals: 2" has a bug worth seeing —
        silently keeping the second is how a player ends up disputing their own
        season total.
        """
        if raw_stats in (None, ""):
            return []

        if not isinstance(raw_stats, (list, tuple)):
            raise ValidationError(
                "Stats must be a list of {stat_field, value} objects."
            )

        wanted = []
        seen = set()

        for index, item in enumerate(raw_stats):
            if not isinstance(item, dict):
                raise ValidationError(
                    f"stats[{index}]: expected an object with a stat_field "
                    f"and a value."
                )

            raw_id = item.get("stat_field")
            if not is_valid_uuid(raw_id):
                raise ValidationError(
                    f"stats[{index}]: '{raw_id}' is not a valid stat field id."
                )

            field_id = uuid.UUID(str(raw_id))
            if field_id in seen:
                raise ValidationError(
                    f"stats[{index}]: the same stat cannot be sent twice."
                )
            seen.add(field_id)

            wanted.append((index, field_id, item.get("value")))

        fields = {
            field.id: field
            for field in SportMatchStatField.objects.filter(id__in=seen)
        }

        resolved = []

        for index, field_id, raw_value in wanted:
            field = fields.get(field_id)

            if field is None:
                raise ValidationError(
                    f"stats[{index}]: that stat no longer exists."
                )

            if field.sport_id != sport.id:
                raise ValidationError(
                    f"stats[{index}]: {field.name} is not a {sport.name} stat."
                )

            if not field.is_active:
                raise ValidationError(
                    f"stats[{index}]: {field.name} is no longer being recorded."
                )

            resolved.append(
                (field, MatchService._clean_stat_value(field, raw_value, index))
            )

        return resolved

    @staticmethod
    def _write_stats(entry, stats) -> None:
        """
        Replace-on-update for the entry's stats: drop what is there and
        bulk_create the incoming set, the same shape
        ``RecruitmentService._sync_positions`` uses for its nested children. On
        a create the delete is a no-op, so both paths share one code path and
        cannot drift.

        Only ever called when the caller actually sent a ``stats`` key — an
        absent key means "leave them alone", not "delete them all".
        """
        entry.stats.all().delete()

        if stats:
            MatchEntryStat.objects.bulk_create([
                MatchEntryStat(
                    match_entry=entry,
                    stat_field=field,
                    value=value,
                )
                for field, value in stats
            ])

    # =================================================================
    # THE SCHEDULED/PLAYED INVARIANT
    # =================================================================

    @staticmethod
    def _assert_scheduled_is_blank(status, result, minutes, rating, stat_count) -> None:
        """
        A fixture has not happened yet, so it cannot carry a result, minutes, a
        rating or stats.

        The DB holds this too (``match_entry_scheduled_has_no_result``), and
        deliberately: a row saying a future match was won is worse than a
        rejected write. This check exists so the client gets a sentence instead
        of an IntegrityError, and so it also covers stats, which live in
        another table and are past the constraint's reach.
        """
        if status != MatchEntry.Status.SCHEDULED:
            return

        offenders = []
        if result and result != MatchEntry.Result.NA:
            offenders.append("a result")
        if minutes is not None:
            offenders.append("minutes played")
        if rating is not None:
            offenders.append("a rating")
        if stat_count:
            offenders.append("stats")

        if offenders:
            raise ValidationError(
                f"A scheduled match cannot have {', '.join(offenders)}. "
                f"Add those when you mark it as played."
            )

    # =================================================================
    # CREATE
    # =================================================================

    @staticmethod
    def create_match(actor, **payload) -> MatchEntry:
        """
        Log one match — either one already played, or a fixture to come.

        ``payload`` accepts: ``sport`` (required), ``date`` (required),
        ``status``, ``kickoff_time``, ``opponent_name``, ``match_type``,
        ``result``, ``minutes_played``, ``position``, ``self_rating``,
        ``notes``, ``career_entry``, ``photo_url`` + ``photo_public_id``, and
        ``stats`` as ``[{"stat_field": <id>, "value": <number>}, ...]``.

        The entry and its stats are written in one transaction, so a rejected
        stat leaves no half-logged match behind. A played entry then moves the
        streak inside that same transaction.
        """
        user = MatchService.require_player(actor)
        MatchService._reject_visibility(payload)

        sport = MatchService._resolve_sport(payload.get("sport"))

        status = MatchService._clean_choice(
            payload.get("status"),
            MatchEntry.Status,
            "Status",
            MatchEntry.Status.PLAYED,
        )
        result = MatchService._clean_choice(
            payload.get("result"),
            MatchEntry.Result,
            "Result",
            MatchEntry.Result.NA,
        )
        match_type = MatchService._clean_choice(
            payload.get("match_type"),
            MatchEntry.MatchType,
            "Match type",
            MatchEntry.MatchType.OTHER,
        )

        minutes = MatchService._clean_minutes(payload.get("minutes_played"))
        rating = MatchService._clean_self_rating(payload.get("self_rating"))
        stats = MatchService._resolve_stats(sport, payload.get("stats"))

        MatchService._assert_scheduled_is_blank(
            status, result, minutes, rating, len(stats)
        )

        photo = MatchService._clean_photo(user, payload) or ("", "")

        entry = MatchEntry(
            user=user,
            sport=sport,
            status=status,
            date=MatchService._clean_date(payload.get("date")),
            kickoff_time=MatchService._clean_time(payload.get("kickoff_time")),
            opponent_name=MatchService._clean_text(
                payload.get("opponent_name"), "opponent_name", "Opponent name"
            ),
            match_type=match_type,
            result=result,
            minutes_played=minutes,
            position=MatchService._resolve_position(
                sport, payload.get("position")
            ),
            self_rating=rating,
            notes=(payload.get("notes") or "").strip(),
            career_entry=MatchService._resolve_career_entry(
                user, payload.get("career_entry")
            ),
            photo_url=photo[0],
            photo_public_id=photo[1],
        )

        with transaction.atomic():
            entry.save()

            if stats:
                MatchService._write_stats(entry, stats)

            if entry.status == MatchEntry.Status.PLAYED:
                MatchService.recompute_streak(user)

        return entry

    # =================================================================
    # UPDATE
    # =================================================================

    @staticmethod
    def update_match(actor, match_id, **payload) -> MatchEntry:
        """
        Edit one of the player's own matches. PATCH semantics — only the keys
        actually present in ``payload`` are touched.

        The path this mainly serves is promoting a fixture: one call carrying
        ``status="played"`` alongside the result, minutes, rating and stats.
        That is the whole reason a fixture and a report are one row.

        Two rules are worth knowing before you send a PATCH:

          * ``stats`` is replace-on-update. Sending the key replaces the whole
            set; leaving it out leaves the existing stats alone. There is no
            way to edit one stat in place, because the client always holds the
            full form anyway.
          * Moving a played match back to scheduled is REJECTED, not applied.
            It would have to throw away the result, minutes, rating and stats
            already logged, and silently deleting a player's own record of a
            match is worse than an error telling them to delete the match.

        Changing the sport is allowed while the entry has no stats; once it
        does, the new sport's catalog cannot describe them, so the client has
        to resend ``stats`` (an empty list is fine) in the same PATCH. The
        position is dropped when the sport moves and no new one was sent — the
        old one belongs to a sport this match is no longer about.
        """
        user = MatchService.require_player(actor)
        MatchService._reject_visibility(payload)

        entry = MatchService._get_owned_match(user, match_id)

        if not payload:
            raise ValidationError("Nothing to update.")

        was_played = entry.status == MatchEntry.Status.PLAYED
        previous_public_id = entry.photo_public_id

        # ── sport, and everything that hangs off it ───────────────────
        sport = entry.sport
        sport_changed = False

        if "sport" in payload:
            sport = MatchService._resolve_sport(payload["sport"])
            sport_changed = sport.id != entry.sport_id

        replace_stats = "stats" in payload
        stats = (
            MatchService._resolve_stats(sport, payload["stats"])
            if replace_stats
            else None
        )

        if sport_changed and not replace_stats and entry.stats.exists():
            raise ValidationError(
                "Changing the sport would leave stats that do not belong to "
                "it. Send stats again (or an empty list) with the new sport."
            )

        if "position" in payload:
            entry.position = MatchService._resolve_position(
                sport, payload["position"]
            )
        elif sport_changed:
            entry.position = None

        entry.sport = sport

        # ── status ───────────────────────────────────────────────────
        if "status" in payload:
            status = MatchService._clean_choice(
                payload["status"],
                MatchEntry.Status,
                "Status",
                entry.status,
            )

            if was_played and status == MatchEntry.Status.SCHEDULED:
                raise ValidationError(
                    "A match you have already played cannot be moved back to "
                    "scheduled — the result, minutes, rating and stats you "
                    "logged would have to be thrown away. Delete the match "
                    "instead, or change its date."
                )

            entry.status = status

        # ── plain fields ─────────────────────────────────────────────
        if "date" in payload:
            entry.date = MatchService._clean_date(payload["date"])

        if "kickoff_time" in payload:
            entry.kickoff_time = MatchService._clean_time(payload["kickoff_time"])

        if "opponent_name" in payload:
            entry.opponent_name = MatchService._clean_text(
                payload["opponent_name"], "opponent_name", "Opponent name"
            )

        if "match_type" in payload:
            entry.match_type = MatchService._clean_choice(
                payload["match_type"],
                MatchEntry.MatchType,
                "Match type",
                entry.match_type,
            )

        if "result" in payload:
            entry.result = MatchService._clean_choice(
                payload["result"],
                MatchEntry.Result,
                "Result",
                MatchEntry.Result.NA,
            )

        if "minutes_played" in payload:
            entry.minutes_played = MatchService._clean_minutes(
                payload["minutes_played"]
            )

        if "self_rating" in payload:
            entry.self_rating = MatchService._clean_self_rating(
                payload["self_rating"]
            )

        if "notes" in payload:
            entry.notes = (payload["notes"] or "").strip()

        if "career_entry" in payload:
            entry.career_entry = MatchService._resolve_career_entry(
                user, payload["career_entry"]
            )

        photo = MatchService._clean_photo(user, payload)
        if photo is not None:
            entry.photo_url, entry.photo_public_id = photo

        # The merged state, not the payload: a fixture stays clean whether the
        # offending value arrived in this PATCH or was somehow already there.
        MatchService._assert_scheduled_is_blank(
            entry.status,
            entry.result,
            entry.minutes_played,
            entry.self_rating,
            len(stats) if replace_stats else entry.stats.count(),
        )

        with transaction.atomic():
            # Full save: too many fields are conditionally touched for
            # update_fields to be worth the risk of forgetting one.
            entry.save()

            if replace_stats:
                MatchService._write_stats(entry, stats)

            # Covers every case the streak cares about: a fixture promoted to
            # played, a played entry's date moved, and a played entry edited at
            # all (cheap enough, and one query decides the whole thing).
            if was_played or entry.status == MatchEntry.Status.PLAYED:
                MatchService.recompute_streak(user)

        # The old image is now unreferenced. Deferred and best-effort, so a
        # storage outage cannot fail an edit that has already committed.
        if (
            previous_public_id
            and previous_public_id != entry.photo_public_id
        ):
            transaction.on_commit(
                lambda: MatchService._delete_orphaned_assets([previous_public_id])
            )

        return entry

    # =================================================================
    # DELETE
    # =================================================================

    @staticmethod
    def delete_match(actor, match_id) -> MatchEntry:
        """
        Soft delete one match, consistent with posts, messages and highlights.

        The row survives with ``is_deleted``; the photo does not. There is no
        undelete, so the object would otherwise sit in the bucket forever — the
        consequence is that a soft-deleted row keeps a ``photo_url`` pointing
        at nothing, which is correct exactly as long as nothing ever reads a
        deleted entry back.

        The cleanup is deferred to after commit and never allowed to raise: a
        storage failure must not fail — or roll back — the delete.
        """
        user = MatchService.require_player(actor)
        entry = MatchService._get_owned_match(user, match_id)

        public_id = entry.photo_public_id

        entry.is_deleted = True
        entry.save(update_fields=["is_deleted", "updated_at"])

        MatchService.recompute_streak(user)

        if public_id:
            transaction.on_commit(
                lambda: MatchService._delete_orphaned_assets([public_id])
            )

        return entry

    @staticmethod
    def _delete_orphaned_assets(public_ids) -> None:
        """
        Best-effort deletion of match photos nothing references any more. Never
        raises — a failed cleanup must not break the request. Scheduled via
        ``transaction.on_commit`` so nothing is destroyed on a rollback.
        """
        TAG = "MatchService._delete_orphaned_assets"

        try:
            storage = get_storage_service()
        except Exception as exc:
            logger.warning(f"{TAG} | Storage unavailable | {exc}")
            return

        for public_id in public_ids:
            try:
                storage.delete_file(public_id)
            except Exception as exc:
                logger.warning(
                    f"{TAG} | Failed to delete asset | "
                    f"public_id={public_id} | {exc}"
                )

    # =================================================================
    # STREAKS
    # =================================================================

    @staticmethod
    def recompute_streak(user) -> MatchDiarySettings:
        """
        Recompute the player's streak from scratch and write it to their diary
        settings.

        Streaks are counted in MATCH-WEEKS taken from the MATCH DATE, never
        from when the row was written. A player who sits down on Sunday and
        logs four weeks of backlog genuinely played four weeks running, and the
        counter is a record of playing, not of app usage. It also means a
        streak cannot be farmed by opening the app, and cannot be lost by
        forgetting to.

        A week counts once however many matches were in it, and only PLAYED,
        non-deleted entries count — a fixture is a plan, not a match.

        The current streak starts at this week if it has a match, and at LAST
        week if it does not: on a Tuesday, a player who has not played yet has
        not broken anything. If neither week has one, the streak is 0. From
        there it walks backwards while each preceding week is present, bounded
        at ``MAX_STREAK_WEEKS`` so a corrupt date can never spin.

        The longest streak is the longest run anywhere in the history, and is
        recomputed rather than accumulated — deleting matches has to be able to
        bring it down, or it stops being a fact about the player.

        One query for the weeks (grouped in the DB, not per row) and one write.
        """
        weeks = set(
            MatchEntry.objects
            .filter(
                user=user,
                status=MatchEntry.Status.PLAYED,
                is_deleted=False,
            )
            # Meta.ordering would otherwise be added to the SELECT and defeat
            # the DISTINCT.
            .order_by()
            .annotate(
                iso_year=ExtractIsoYear("date"),
                iso_week=ExtractWeek("date"),
            )
            .values_list("iso_year", "iso_week")
            .distinct()
        )

        current = MatchService._current_streak(weeks)
        longest = MatchService._longest_streak(weeks)

        settings, _ = MatchDiarySettings.objects.get_or_create(user=user)

        settings.current_streak_weeks = current
        settings.longest_streak_weeks = longest
        settings.last_logged_at = timezone.now()
        # auto_now only fires for fields listed in update_fields.
        settings.save(update_fields=[
            "current_streak_weeks",
            "longest_streak_weeks",
            "last_logged_at",
            "updated_at",
        ])

        return settings

    @staticmethod
    def _iso_week(day) -> tuple:
        """``date`` → the ``(iso_year, iso_week)`` pair the streak counts in."""
        calendar = day.isocalendar()
        return (calendar[0], calendar[1])

    @staticmethod
    def _monday_of(week):
        """``(iso_year, iso_week)`` → that week's Monday, for date arithmetic."""
        return datetime.date.fromisocalendar(week[0], week[1], 1)

    @staticmethod
    def _current_streak(weeks) -> int:
        """
        How many weeks back the run reaches from now. See
        :meth:`recompute_streak` for why it may start at last week.
        """
        if not weeks:
            return 0

        cursor = MatchService._iso_week(timezone.localdate())

        if cursor not in weeks:
            cursor = MatchService._iso_week(
                MatchService._monday_of(cursor) - datetime.timedelta(days=7)
            )

        if cursor not in weeks:
            return 0

        streak = 0
        while cursor in weeks and streak < MatchService.MAX_STREAK_WEEKS:
            streak += 1
            cursor = MatchService._iso_week(
                MatchService._monday_of(cursor) - datetime.timedelta(days=7)
            )

        return streak

    @staticmethod
    def _longest_streak(weeks) -> int:
        """The longest run of consecutive match-weeks anywhere in the history."""
        if not weeks:
            return 0

        mondays = sorted(MatchService._monday_of(week) for week in weeks)

        longest = 1
        run = 1

        for previous, current in zip(mondays, mondays[1:]):
            if current - previous == datetime.timedelta(days=7):
                run += 1
            else:
                run = 1
            longest = max(longest, run)

        return longest
