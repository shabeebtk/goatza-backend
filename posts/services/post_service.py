from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest
from services.storage.factory import get_storage_service
from posts.models import Comment, Post
    
class PostService:

    @staticmethod
    def delete_post(post_id, actor):
        storage = get_storage_service()

        # get post
        if actor.is_user:
            post = Post.objects.filter(
                id=post_id,
                author_user=actor.user
            ).first()

        elif actor.is_org:
            post = Post.objects.filter(
                id=post_id,
                author_org=actor.organization
            ).first()
        else:
            return False, None

        if not post:
            return False, None

        try:
            with transaction.atomic():

                #  get first media to extract folder
                first_media = post.media.first()

                if first_media and first_media.public_id:
                    folder_path = "/".join(first_media.public_id.split("/")[:-1])

                    try:
                        storage.delete_folder_data(folder_path)
                    except Exception as e:
                        print(f"Folder delete failed: {e}")

                #  delete DB
                deleted_count, _ = post.delete()

                return deleted_count > 0, None

        except Exception as e:
            print(f"Delete post failed: {e}")
            return False, None

    # =================================================================
    # MODERATION
    # =================================================================
    #
    # Additive: nothing above this line changes. These are the paths the
    # moderation admin calls, and they differ from the owner paths on purpose.

    @staticmethod
    def moderator_delete_post(post):
        """
        Take a post down as a moderator. Returns True if it moved.

        SOFT delete, unlike ``delete_post``, and the difference is deliberate.
        The owner path hard-deletes the row AND sweeps the post's R2 folder,
        which is right when somebody removes their own photo. It is wrong here
        twice over: the report's evidence would be destroyed along with the
        content, and a wrong call could never be undone.

        So this sets ``is_deleted`` — the flag every read path already filters
        on (feed, search, saved posts, the public profile) — and leaves the
        media objects in the bucket. Reversing a takedown is one boolean.
        """
        if post.is_deleted:
            return False

        post.is_deleted = True
        post.save(update_fields=["is_deleted"])

        return True

    @staticmethod
    def moderator_delete_comment(comment):
        """
        Take a comment down as a moderator, cascading to its replies and
        fixing the denormalized counters — the same mechanics the owner path in
        DeleteCommentAPIView runs, minus the author/post-owner permission gate.

        NOTE: those mechanics are implemented in the view, not in a service, so
        this is a second copy of them. The two must move together; extracting
        the view's block into this method is the obvious follow-up, kept out of
        this change so a moderation feature does not quietly alter the comment
        endpoint.
        """
        if comment.is_deleted:
            return False

        with transaction.atomic():
            if comment.parent_id is None:
                reply_ids = list(
                    Comment.objects
                    .filter(parent_id=comment.id, is_deleted=False)
                    .values_list("id", flat=True)
                )
                removed = 1 + len(reply_ids)

                if reply_ids:
                    Comment.objects.filter(id__in=reply_ids).update(is_deleted=True)

                comment.is_deleted = True
                comment.save(update_fields=["is_deleted"])
            else:
                removed = 1
                comment.is_deleted = True
                comment.save(update_fields=["is_deleted"])

                Comment.objects.filter(id=comment.parent_id).update(
                    reply_count=Greatest(F("reply_count") - 1, Value(0))
                )

            Post.objects.filter(id=comment.post_id).update(
                comments_count=Greatest(F("comments_count") - removed, Value(0))
            )

        return True
