from django.db import transaction
from notifications.models import Notification
from notifications.services.fcm_service import FCMService

def build_notification_payload(notification: Notification):
    actor = notification.actor_user
    profile = getattr(actor, "profile", None)

    actor_name = profile.name if profile else actor.username
    actor_avatar = profile.profile_photo if profile else ""

    # MESSAGE BUILDING 
    title = "Goatza"
    body = ""
    url = "/"

    if notification.type == Notification.Type.LIKE:
        title = f"{actor_name} liked your post"
        body = "Tap to view"
        url = f"/post/{notification.post_id}"

    elif notification.type == Notification.Type.COMMENT:
        comment_text = getattr(notification.comment, "content", "")
        short_comment = comment_text[:60] + "..." if len(comment_text) > 60 else comment_text

        # reply vs normal comment
        if notification.group_key.startswith("reply:"):
            title = f"{actor_name} replied to your comment"
        else:
            title = f"{actor_name} commented on your post"

        body = short_comment or "Tap to view"
        url = f"/post/{notification.post_id}"

    elif notification.type == Notification.Type.FOLLOW:
        title = f"{actor_name} started following you"
        body = "Tap to view profile"
        url = f"/profile/{actor.username}"

    elif notification.type == Notification.Type.FOLLOW_BACK:
        title = f"{actor_name} followed you back"
        body = "Tap to view profile"
        url = f"/profile/{actor.username}"

    # FINAL PAYLOAD
    return {
        "type": notification.type,
        "notification_id": str(notification.id),

        # actor
        "actor_name": actor_name,
        "actor_username": actor.username,
        "actor_avatar": actor_avatar,
        "actor_initials": (actor_name[:2]).upper(),

        # target
        "target_id": str(notification.post_id or ""),

        # PUSH CONTENT
        "title": title,
        "body": body,
        "url": url,

        # grouping
        "group_key": notification.group_key or "",
    }

class NotificationService:

    @staticmethod
    def _get_actor(actor_user=None, actor_org=None):
        return {
            "actor_user": actor_user,
            "actor_org": actor_org
        }

    @staticmethod
    def _get_recipient(recipient_user=None, recipient_org=None):
        return {
            "recipient_user": recipient_user,
            "recipient_org": recipient_org
        }

    # ----------------------------------------
    # FOLLOW
    # ----------------------------------------
    @staticmethod
    def follow(actor_user=None, actor_org=None, target_user=None, target_org=None):
        """
        Send follow notification (deduplicated)
        """

        if target_user:
            dedup_key = f"follow:{actor_user.id}:{target_user.id}"
        else:
            dedup_key = f"follow_org:{actor_org.id}:{target_org.id}"

        # prevent duplicates
        if Notification.objects.filter(dedup_key=dedup_key).exists():
            return

        notification = Notification.objects.create(
            type=Notification.Type.FOLLOW,
            dedup_key=dedup_key,
            group_key=dedup_key,
            **NotificationService._get_actor(actor_user, actor_org),
            **NotificationService._get_recipient(target_user, target_org),
        )
        payload = build_notification_payload(notification)
        FCMService.send_to_user(target_user, payload)

            
    @staticmethod
    def follow_back(actor_user=None, actor_org=None, target_user=None, target_org=None):
        """
        Follow back notification (deduplicated)
        """
        if target_user:
            dedup_key = f"follow_back:{actor_user.id}:{target_user.id}"
        else:
            dedup_key = f"follow_back_org:{actor_org.id}:{target_org.id}"

        # prevent duplicates
        if Notification.objects.filter(dedup_key=dedup_key).exists():
            return

        notification = Notification.objects.create(
            type=Notification.Type.FOLLOW_BACK,
            dedup_key=dedup_key,
            group_key=dedup_key,
            **NotificationService._get_actor(actor_user, actor_org),
            **NotificationService._get_recipient(target_user, target_org),
        )
        payload = build_notification_payload(notification)
        FCMService.send_to_user(target_user, payload)

    # ----------------------------------------
    # LIKE
    # ----------------------------------------
    @staticmethod
    def like(actor_user=None, actor_org=None, post=None):
        """
        Like notification (grouped, NOT deduped)
        """

        # don't notify self
        if post.author_user == actor_user:
            return

        notification = Notification.objects.create(
            type=Notification.Type.LIKE,
            group_key=f"like:post:{post.id}",
            post=post,
            data={
                "post_id": str(post.id)
            },
            **NotificationService._get_actor(actor_user, actor_org),
            **NotificationService._get_recipient(post.author_user, post.author_org),
        )
        payload = build_notification_payload(notification)
        FCMService.send_to_user(post.author_user, payload)

    # COMMENT
    @staticmethod
    def comment(actor_user=None, actor_org=None, comment=None):
        post = comment.post

        notified_users = set()

        # ----------------------------------------
        # POST OWNER
        # ----------------------------------------
        if post.author_user and post.author_user != actor_user:
            notification  = Notification.objects.create(
                type=Notification.Type.COMMENT,
                group_key=f"comment:post:{post.id}",
                post=post,
                comment=comment,
                **NotificationService._get_actor(actor_user, actor_org),
                **NotificationService._get_recipient(post.author_user, None),
            )
            notified_users.add(post.author_user.id)
            payload = build_notification_payload(notification)
            FCMService.send_to_user(post.author_user, payload)

        # ----------------------------------------
        # REPLY TARGET
        # ----------------------------------------
        if comment.reply_to:
            target_user = comment.reply_to.user

            if (
                target_user
                and target_user != actor_user
                and target_user.id not in notified_users
            ):
                notification = Notification.objects.create(
                    type=Notification.Type.COMMENT,
                    group_key=f"reply:comment:{comment.reply_to.id}",
                    post=post,
                    comment=comment,
                    **NotificationService._get_actor(actor_user, actor_org),
                    **NotificationService._get_recipient(target_user, None),
                )
                payload = build_notification_payload(notification)
                FCMService.send_to_user(target_user, payload)