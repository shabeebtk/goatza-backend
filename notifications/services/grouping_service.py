from collections import defaultdict
from posts.serializers.posts_serializers import PostMiniSerializer
from notifications.services.notification_service import (
    ACHIEVEMENT_DECISION_COPY,
    CAREER_DECISION_COPY,
    MESSAGE_SHARE_NOUN,
    RECRUITMENT_STATUS_COPY,
    RECRUITMENT_STATUS_COPY_DEFAULT,
)


class NotificationGroupingService:

    @staticmethod
    def group_notifications(notifications):
        grouped = defaultdict(list)

        # STEP 1: group
        for notif in notifications:
            key = notif.group_key or f"{notif.type}:{notif.id}"
            grouped[key].append(notif)

        result = []

        # STEP 2: build grouped response
        for _, items in grouped.items():
            items_sorted = sorted(items, key=lambda x: x.created_at, reverse=True)

            primary = items_sorted[0]

            actors = [
                NotificationGroupingService._get_actor_data(n)
                for n in items_sorted
            ]

            result.append(
                NotificationGroupingService._build_group_response(
                    primary,
                    actors,
                    items_sorted
                )
            )

        # STEP 3: sort groups
        result.sort(key=lambda x: x["created_at"], reverse=True)

        return result

    # ----------------------------------------

    @staticmethod
    def _get_actor_data(notification):
        if notification.actor_user:
            return {
                "id": str(notification.actor_user.id),
                "name": notification.actor_user.profile_name,
                "username": notification.actor_user.username,
                "avatar": getattr(notification.actor_user.profile, "profile_photo", None)
            }

        if notification.actor_org:
            return {
                "id": str(notification.actor_org.id),
                "name": notification.actor_org.name,
                "username": str(notification.actor_org.username), 
                "avatar": getattr(notification.actor_org.profile, "logo", None)
            }

        return None

    # ----------------------------------------

        
    @staticmethod
    def _build_group_response(primary, actors, items):
        total_count = len(items)

        # Distinct PEOPLE, newest first. Types that dedup per actor (like,
        # follow, recruitment_application) can never repeat one, but types that
        # legitimately can — a player sending two career verification requests
        # to the same club, someone commenting twice — would otherwise render
        # as "Alice, Alice listed you on their career".
        seen_actor_ids = set()
        distinct_actors = []
        for actor in actors:
            if not actor:
                continue
            actor_id = actor.get("id")
            if actor_id in seen_actor_ids:
                continue
            seen_actor_ids.add(actor_id)
            distinct_actors.append(actor)

        top_actors = distinct_actors[:2]
        # "and N others" counts PEOPLE, not rows — two comments from one person
        # is still one person. `message` is the exception and uses total_count
        # directly (see _build_text), because there it means "N more things".
        others_count = max(0, len(distinct_actors) - len(top_actors))

        # ----------------------------------------
        # RECRUITMENT DATA (applicants notifications)
        # Small inline dict — no heavy serializer. recruitment is select_related
        # on the list queryset, so this touches no extra query.
        # ----------------------------------------
        recruitment_data = None
        if primary.recruitment:
            recruitment_data = {
                "id": str(primary.recruitment.id),
                "title": primary.recruitment.title,
                "status": primary.recruitment.status,
            }

        recruitment_title = (
            recruitment_data["title"]
            if recruitment_data
            else primary.data.get("recruitment_title")
        )

        text = NotificationGroupingService._build_text(
            primary.type,
            top_actors,
            others_count,
            recruitment_title=recruitment_title,
            to_status=primary.data.get("to_status"),
            shared_kind=primary.data.get("shared_kind"),
            entry_title=primary.data.get("entry_title"),
            total_count=total_count,
        )

        # ----------------------------------------
        # POST DATA
        # ----------------------------------------
        post_data = None
        if primary.post:
            post_data = PostMiniSerializer(primary.post).data

        # ----------------------------------------
        # COMMENT DATA
        # ----------------------------------------
        comment_data = None
        if primary.comment:
            comment_data = {
                "id": str(primary.comment.id),
                "text": primary.comment.comment
            }

        # ----------------------------------------
        # CAREER ENTRY DATA
        # Small inline dict like recruitment_data — career_entry is
        # select_related on the list queryset, so this costs no extra query.
        # ----------------------------------------
        career_entry_data = None
        if primary.career_entry:
            career_entry_data = {
                "id": str(primary.career_entry.id),
                "title": primary.career_entry.title,
                "organization_name": primary.career_entry.organization_name,
                "verification_status": primary.career_entry.verification_status,
            }

        return {
            "id": str(primary.id),
            "type": primary.type,
            "text": text,
            "actors": top_actors,
            "others_count": others_count,
            "is_read": all(n.is_read for n in items),
            "created_at": primary.created_at,

            "post": post_data,
            "comment": comment_data,
            "recruitment": recruitment_data,
            "career_entry": career_entry_data,

            # The primary row's payload, passed through the way
            # NotificationSerializer already does for the ungrouped shape.
            # Some types carry the only copy of an id the client needs to act
            # on here — career_add_prompt's `application_id` is the reason this
            # exists: without it the in-app prompt has no way to call
            # /careers/from-application/<id>.
            "data": primary.data or {}
        }

    # ----------------------------------------

    @staticmethod
    def _build_text(
        notification_type, actors, others_count,
        recruitment_title=None, to_status=None, shared_kind=None,
        entry_title=None, total_count=1
    ):
        names = [a["name"] for a in actors if a]

        if notification_type == "like":
            if others_count > 0:
                return f"{', '.join(names)} and {others_count} others liked your post"
            return f"{', '.join(names)} liked your post"

        if notification_type == "comment":
            if others_count > 0:
                return f"{', '.join(names)} and {others_count} others commented on your post"
            return f"{', '.join(names)} commented on your post"

        if notification_type == "mention":
            # Never repeats: PostMention is unique per (post, target), so a
            # group holds exactly one row and there is no "and N others" form
            # to build — same family as follow / recruitment_application.
            return f"{names[0]} mentioned you in a post" if names else "You were mentioned in a post"

        if notification_type == "follow":
            return f"{names[0]} started following you" if names else "You have a new follower"

        if notification_type == "follow_back":
            return f"{names[0]} followed you back"

        if notification_type == "recruitment_application":
            title = recruitment_title or "your recruitment"
            if others_count > 0:
                return f"{', '.join(names)} and {others_count} others applied to {title}"
            return f"{', '.join(names)} applied to {title}"

        if notification_type == "message":
            who = names[0] if names else "Someone"
            # Grouped per conversation, and one sender can share many things —
            # so this counts ROWS, not people, unlike every other type here.
            if total_count > 1:
                return f"{who} shared {total_count} things with you"
            return f"{who} shared {MESSAGE_SHARE_NOUN.get(shared_kind, 'something')} with you"

        if notification_type == "recruitment_application_status":
            org = names[0] if names else "The organization"
            copy = RECRUITMENT_STATUS_COPY.get(
                to_status, RECRUITMENT_STATUS_COPY_DEFAULT
            )
            return f"{org} {copy['verb']}"

        if notification_type == "career_verification_request":
            # Grouped per org: others_count is how many MORE players are
            # waiting on this club, so the count is the point of the line.
            if others_count > 0:
                return (
                    f"{', '.join(names)} and {others_count} others "
                    f"listed you on their career"
                )
            return f"{', '.join(names)} listed you on their career"

        if notification_type == "achievement_verification_request":
            # Same shape as the career request — grouped per org, so
            # others_count is how many MORE people are waiting on this issuer.
            if others_count > 0:
                return (
                    f"{', '.join(names)} and {others_count} others "
                    f"credited you with an achievement"
                )
            return f"{', '.join(names)} credited you with an achievement"

        if notification_type in CAREER_DECISION_COPY:
            org = names[0] if names else "The organization"
            copy = CAREER_DECISION_COPY[notification_type]
            return f"{org} {copy['verb']}"

        if notification_type in ACHIEVEMENT_DECISION_COPY:
            org = names[0] if names else "The organization"
            copy = ACHIEVEMENT_DECISION_COPY[notification_type]
            return f"{org} {copy['verb']}"

        return "You have a new notification"