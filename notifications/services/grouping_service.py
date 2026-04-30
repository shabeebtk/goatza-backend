from collections import defaultdict
from posts.serializers.posts_serializers import PostMiniSerializer


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

        top_actors = [a for a in actors if a][:2]
        others_count = max(0, total_count - len(top_actors))

        text = NotificationGroupingService._build_text(
            primary.type,
            top_actors,
            others_count
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

        return {
            "id": str(primary.id),
            "type": primary.type,
            "text": text,
            "actors": top_actors,
            "others_count": others_count,
            "is_read": all(n.is_read for n in items),
            "created_at": primary.created_at,

            # 🔥 NEW STRUCTURE
            "post": post_data,
            "comment": comment_data
        }

    # ----------------------------------------

    @staticmethod
    def _build_text(notification_type, actors, others_count):
        names = [a["name"] for a in actors if a]

        if notification_type == "like":
            if others_count > 0:
                return f"{', '.join(names)} and {others_count} others liked your post"
            return f"{', '.join(names)} liked your post"

        if notification_type == "comment":
            if others_count > 0:
                return f"{', '.join(names)} and {others_count} others commented on your post"
            return f"{', '.join(names)} commented on your post"

        if notification_type == "follow":
            return f"{names[0]} started following you" if names else "You have a new follower"
        
        if notification_type == "follow_back":
            return f"{names[0]} followed you back"

        return "You have a new notification"