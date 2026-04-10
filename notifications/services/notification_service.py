from django.db import transaction
from notifications.models import Notification


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

        Notification.objects.create(
            type=Notification.Type.FOLLOW,
            dedup_key=dedup_key,
            group_key=dedup_key,
            **NotificationService._get_actor(actor_user, actor_org),
            **NotificationService._get_recipient(target_user, target_org),
        )

    
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

        Notification.objects.create(
            type=Notification.Type.FOLLOW_BACK,
            dedup_key=dedup_key,
            group_key=dedup_key,
            **NotificationService._get_actor(actor_user, actor_org),
            **NotificationService._get_recipient(target_user, target_org),
        )

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

        Notification.objects.create(
            type=Notification.Type.LIKE,
            group_key=f"like:post:{post.id}",
            post=post,
            data={
                "post_id": str(post.id)
            },
            **NotificationService._get_actor(actor_user, actor_org),
            **NotificationService._get_recipient(post.author_user, post.author_org),
        )

    # COMMENT
    @staticmethod
    def comment(actor_user=None, actor_org=None, comment=None):
        post = comment.post

        notified_users = set()

        # ----------------------------------------
        # POST OWNER
        # ----------------------------------------
        if post.author_user and post.author_user != actor_user:
            Notification.objects.create(
                type=Notification.Type.COMMENT,
                group_key=f"comment:post:{post.id}",
                post=post,
                comment=comment,
                **NotificationService._get_actor(actor_user, actor_org),
                **NotificationService._get_recipient(post.author_user, None),
            )
            notified_users.add(post.author_user.id)

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
                Notification.objects.create(
                    type=Notification.Type.COMMENT,
                    group_key=f"reply:comment:{comment.reply_to.id}",
                    post=post,
                    comment=comment,
                    **NotificationService._get_actor(actor_user, actor_org),
                    **NotificationService._get_recipient(target_user, None),
                )